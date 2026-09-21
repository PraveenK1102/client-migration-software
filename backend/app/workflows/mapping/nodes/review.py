"""Human-in-the-loop mapping review nodes: prepare the queue, interrupt for a decision, apply it.

``await_review`` is the ONLY interrupt point in the mapping graph and is side-effect-free; all
persistence happens in the idempotent surrounding nodes. Interrupt/resume semantics are unchanged.
"""
from __future__ import annotations

import json

from langgraph.types import interrupt

from ....policy import POLICY_VERSION
from ..context import MappingContext
from ..state import GraphState


async def prepare_review_node(ctx: MappingContext, state: GraphState) -> GraphState:
    db = ctx.db
    job_id = state["job_id"]
    open_issues = db.get_issues(job_id, status="open")
    db.set_job_stage(job_id, status="awaiting_review", stage="awaiting_review")
    db.add_audit(job_id, event_type="review_prepared", actor="system",
                 after={"open_issues": len(open_issues)})
    return {"stage": "awaiting_review", "open_issue_count": len(open_issues)}


async def await_review_node(ctx: MappingContext, state: GraphState) -> GraphState:
    db = ctx.db
    job_id = state["job_id"]
    open_issues = db.get_issues(job_id, status="open")
    resume_value = interrupt({"job_id": job_id, "reason": "mapping_review_required",
                              "open_issue_ids": [i["id"] for i in open_issues]})
    return {"resume_payload": resume_value or {}, "stage": "resuming"}


async def apply_decisions_node(ctx: MappingContext, state: GraphState) -> GraphState:
    db = ctx.db
    job_id = state["job_id"]
    db.set_job_stage(job_id, status="processing", stage="applying_decisions")
    eff = ctx.eff(job_id)
    existing = {d["profile_id"]: d for d in db.get_decisions(job_id)}
    for issue in db.get_issues(job_id, status="resolved"):
        resolution = json.loads(issue["resolution"]) if issue["resolution"] else None
        if not resolution:
            continue
        action = resolution.get("action")
        profile_id = issue["profile_id"]
        if action == "approve":
            status, target = "approved", issue["proposed_target_field"]
        elif action == "correct":
            status, target = "corrected", resolution.get("corrected_target")
        elif action == "ignore":
            status, target = "ignored", None          # explicit, audited "leave this source field out"
        else:
            status, target = "rejected", None
        prev = existing.get(profile_id)
        if prev and prev["status"] == status and (prev["target_field"] or None) == (target or None):
            continue
        tf = eff.get(target) if target else None
        dest = eff.destination_kind(target) if target else ("IGNORED" if status == "ignored" else "UNMAPPED")
        db.upsert_decision(job_id, profile_id=profile_id, table_id=issue["table_id"],
                           source_header=issue["source_header"], target_field=target,
                           status=status, actor="human", method="human", reason=resolution.get("reason"),
                           destination_kind=dest, custom_definition_id=(tf.custom_definition_id if tf else None),
                           note=resolution.get("note"))
        db.add_audit(job_id, event_type=("source_field_ignored" if status == "ignored" else "issue_resolved"),
                     actor="human", issue_id=issue["id"],
                     source_ref={"profile_id": profile_id, "header": issue["source_header"],
                                 "table_id": issue["table_id"]},
                     before={"target": issue["proposed_target_field"], "status": "needs_review"},
                     after={"target": target, "status": status, "destination_kind": dest,
                            "note": resolution.get("note")},
                     reason=resolution.get("reason"), schema_version=eff.version, policy_version=POLICY_VERSION)
    ctx.supersede_settled_proposals(job_id)
    open_count = len(db.get_issues(job_id, status="open"))
    return {"stage": "applying_decisions", "open_issue_count": open_count}
