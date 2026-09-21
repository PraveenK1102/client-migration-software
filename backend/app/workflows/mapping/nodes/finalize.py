"""finalize_mapping node: rebuild transform plans once all mappings are settled, then summarise."""
from __future__ import annotations

import json

from ....mapping_rules import RULE_VERSION
from ....policy import POLICY_VERSION
from ....source_intelligence import analyze_source, intelligence_metrics
from ..context import MappingContext
from ..state import GraphState


async def finalize_node(ctx: MappingContext, state: GraphState) -> GraphState:
    db, adapter, model_id, tracer = ctx.db, ctx.adapter, ctx.model_id, ctx.tracer
    job_id = state["job_id"]
    eff = ctx.eff(job_id)
    # M3C: rebuild transformation plans now that all mappings (rule + model + human) are settled,
    # so a model/human-accepted enum/date column also gets its declarative plan. Idempotent;
    # already auto-accepted plans are kept as-is.
    await analyze_source(db, job_id, eff, adapter, model_id, tracer=tracer)
    decisions = db.get_decisions(job_id)
    accepted = [d for d in decisions if d["target_field"] and d["status"] in
                ("auto_accepted", "approved", "corrected")]
    covered = {d["target_field"] for d in accepted}
    required = [f.name for f in eff.fields if f.required_in_final]
    tables = db.get_tables(job_id)
    proposals = db.get_proposals(job_id)
    tables_with_proposals = {p["table_id"] for p in proposals}
    audit = db.get_audit(job_id)
    api_attempts = sum((a.get("after") and json.loads(a["after"]).get("attempts", 0)) or 0
                       for a in audit if a["event_type"] == "proposed")

    metrics = {
        "source_tables": len(tables),
        "rows_examined": sum(t["n_rows"] for t in tables),
        "columns_resolved_by_rule": len([d for d in decisions if d.get("method") == "rule"]),
        "columns_resolved_by_model": len([d for d in decisions if d.get("method") == "model"]),
        "columns_resolved_by_human": len([d for d in decisions if d.get("method") == "human"]),
        "columns_unmapped": len([d for d in decisions if d["status"] == "unmapped"]),
        "columns_ignored": len([d for d in decisions if d["status"] == "ignored"]),
        "columns_to_core": len([d for d in accepted if (d.get("destination_kind") or "CORE_FIELD") == "CORE_FIELD"]),
        "columns_to_collections": len([d for d in accepted if d.get("destination_kind") == "COLLECTION_FIELD"]),
        "columns_to_custom": len([d for d in accepted if d.get("destination_kind") == "CUSTOM_FIELD"]),
        "open_custom_field_proposals": len(db.get_custom_field_proposals(job_id, status="open")),
        "tables_needing_no_model": len([t for t in tables if t["id"] not in tables_with_proposals]),
        "model_proposal_requests": len(tables_with_proposals),
        "model_api_attempts": api_attempts,
        "unresolved_issues": len(db.get_issues(job_id, status="open")),
    }
    metrics["source_intelligence"] = intelligence_metrics(db, job_id)
    summary = {
        "result": "mapping_complete", "schema_version": eff.version, "tenant_id": eff.tenant_id,
        "policy_version": POLICY_VERSION, "rules_version": RULE_VERSION, "model_id": model_id,
        "counts": {
            "accepted_mappings": len(accepted),
            "auto_accepted": len([d for d in decisions if d["status"] == "auto_accepted"]),
            "rule_accepted": metrics["columns_resolved_by_rule"],
            "model_accepted": metrics["columns_resolved_by_model"],
            "human_approved": len([d for d in decisions if d["status"] == "approved"]),
            "human_corrected": len([d for d in decisions if d["status"] == "corrected"]),
            "rejected": len([d for d in decisions if d["status"] == "rejected"]),
            "unmapped": metrics["columns_unmapped"],
            "ignored": metrics["columns_ignored"],
            "reviews_resolved": len(db.get_issues(job_id, status="resolved")),
        },
        "routing_metrics": metrics,
        "accepted_mappings": [{"source_header": d["source_header"], "target_field": d["target_field"],
                               "status": d["status"], "actor": d["actor"], "method": d.get("method"),
                               "destination_kind": d.get("destination_kind")}
                              for d in accepted],
        "uncovered_required_fields": [f for f in required if f not in covered],
    }
    db.set_job_summary(job_id, summary)
    db.set_job_stage(job_id, status="mapping_complete", stage="mapping_complete")
    db.add_audit(job_id, event_type="mapping_complete", actor="system", after=summary["counts"],
                 schema_version=eff.version, policy_version=POLICY_VERSION, model_version=model_id)
    return {"stage": "mapping_complete", "open_issue_count": 0}
