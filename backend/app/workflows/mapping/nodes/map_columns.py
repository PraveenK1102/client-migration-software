"""map_columns node: deterministic-first mapping.

Rules resolve canonical/alias headers with ZERO model calls; only genuinely unresolved columns go to
the bounded, PII-safe model proposal path. With no provider, unresolved columns are BLOCKED (rule
decisions preserved) and each becomes a custom-field proposal — the fake adapter is never substituted.
"""
from __future__ import annotations

from ....llm.base import ModelError, ProposalRequest
from ....mapping_rules import map_table_deterministic, table_context_name
from ....model_projection import to_model_safe_column
from ....observability import record_model_call
from ....policy import POLICY_VERSION
from ....profiling import ColumnProfile
from ....source_intelligence import analyze_relationships_for_table
from ..._support import HUMAN_FINAL as _HUMAN_FINAL
from ..._support import profile_obj as _profile_obj
from ..context import MappingContext
from ..state import GraphState


async def map_columns_node(ctx: MappingContext, state: GraphState) -> GraphState:
    db, adapter, model_id, tracer = ctx.db, ctx.adapter, ctx.model_id, ctx.tracer
    _eff = ctx.eff
    _propose_custom_fields = ctx.propose_custom_fields
    _supersede_settled_proposals = ctx.supersede_settled_proposals

    job_id = state["job_id"]
    db.set_job_stage(job_id, status="processing", stage="mapping")
    eff = _eff(job_id)                     # effective contract for this job's tenant
    schema_public = eff.public_dict(for_model=True)   # lean, bounded model-visible schema (§22)
    profiles_by_table: dict[str, list[dict]] = {}
    for p in db.get_profiles(job_id):
        profiles_by_table.setdefault(p["table_id"], []).append(p)
    tables = {t["id"]: t for t in db.get_tables(job_id)}

    provider_blocked = False
    blocked_columns: list[str] = []
    existing_dec = {d["profile_id"]: d for d in db.get_decisions(job_id)}

    for table_id, prows in profiles_by_table.items():
        profiles = [_profile_obj(p) for p in sorted(prows, key=lambda x: x["col_index"])]
        t = tables.get(table_id, {})
        tname = table_context_name(t.get("original_filename"), t.get("sheet_name"))
        rule_results = map_table_deterministic(profiles, eff, table_name=tname)
        accepted_context: dict[str, str] = {}
        unresolved: list[ColumnProfile] = []

        for rm, prof in zip(rule_results, profiles):
            prev = existing_dec.get(rm.source_column_id)
            # A persisted HUMAN decision (approved/corrected/ignored/rejected) is an overlay that a
            # re-run never re-evaluates or overwrites.
            if prev and prev.get("actor") == "human" and prev["status"] in _HUMAN_FINAL:
                if prev["target_field"] and prev["status"] in ("approved", "corrected"):
                    accepted_context[prof.header] = prev["target_field"]
                continue
            if not rm.resolved and prof.non_empty_count == 0:
                # An entirely empty column carries no business data: record it as unmapped (visible,
                # never silently dropped) instead of blocking the job on model interpretation.
                if not (prev and prev["status"] == "unmapped"):
                    db.upsert_decision(job_id, profile_id=prof.profile_id, table_id=table_id,
                                       source_header=prof.header, target_field=None, status="unmapped",
                                       actor="system", method="none", destination_kind="UNMAPPED",
                                       reason="Column has no non-empty values; no destination inferred.")
                    db.add_audit(job_id, event_type="mapping_unmapped", actor="system",
                                 source_ref={"profile_id": prof.profile_id, "header": prof.header, "table_id": table_id},
                                 after={"target": None, "empty_column": True},
                                 reason="Column has no non-empty values; recorded as unmapped.",
                                 schema_version=eff.version, policy_version=POLICY_VERSION)
                continue
            if rm.resolved and rm.target:
                tf = eff.get(rm.target)
                already = (prev and prev["status"] == "auto_accepted"
                           and prev.get("method") == "rule" and prev["target_field"] == rm.target)
                if not already:  # idempotent: no duplicate decision/audit on re-run
                    db.upsert_decision(job_id, profile_id=rm.source_column_id, table_id=table_id,
                                       source_header=rm.header, target_field=rm.target,
                                       status="auto_accepted", actor="system", method="rule",
                                       reason=rm.reason + (f" data-quality: {rm.data_quality_flags}"
                                                           if rm.data_quality_flags else ""),
                                       destination_kind=rm.destination_kind, path_meta=rm.path_meta,
                                       custom_definition_id=(tf.custom_definition_id if tf else None))
                    db.add_audit(job_id, event_type="mapping_rule_accepted", actor="system",
                                 source_ref={"profile_id": rm.source_column_id, "header": rm.header,
                                             "table_id": table_id},
                                 after={"target": rm.target, "rule": rm.rule_id,
                                        "destination_kind": rm.destination_kind, "path_meta": rm.path_meta,
                                        "data_quality_flags": rm.data_quality_flags},
                                 reason=rm.reason, schema_version=eff.version, policy_version=POLICY_VERSION)
                accepted_context[rm.header] = rm.target
            else:
                unresolved.append(prof)

        # M3C structural pass (deterministic, BEFORE any model call): discover code/display
        # relationships and dispose a proven 1:1 code column as a redundant representation
        # (raw kept in provenance). Excluded from the model call and from custom-field proposals.
        redundant_ids = analyze_relationships_for_table(db, job_id, table_id, profiles, eff)
        if redundant_ids:
            unresolved = [p for p in unresolved if p.profile_id not in redundant_ids]

        if not unresolved:
            db.add_audit(job_id, event_type="table_rules_only", actor="system",
                         after={"table_id": table_id, "columns": len(profiles),
                                "table_role": (f"child:{eff.collection_for_table_name(tname).key}"
                                               if eff.collection_for_table_name(tname) else "employee")})
            continue

        if adapter is None:
            provider_blocked = True
            blocked_columns.extend(p.header for p in unresolved)
            # Deterministic escape hatch (no model): each unresolved non-empty column becomes a
            # custom-field PROPOSAL a consultant can approve / map / ignore. Never auto-created.
            _propose_custom_fields(job_id, eff, table_id, unresolved, origin="no_provider")
            db.add_audit(job_id, event_type="mapping_blocked_provider", actor="system",
                         after={"table_id": table_id,
                                "unresolved_columns": [p.header for p in unresolved]},
                         reason=("Groq is not configured; columns needing model interpretation are blocked. "
                                 "Each has a custom-field proposal a consultant can decide on instead."))
            continue

        # Bounded model proposal for unresolved columns only + accepted-context for conflicts.
        req = ProposalRequest(
            table_id=table_id,
            source_table_ref={"table_id": table_id,
                              "original_filename": tables.get(table_id, {}).get("original_filename"),
                              "sheet_name": tables.get(table_id, {}).get("sheet_name"),
                              "already_accepted_mappings": accepted_context},
            # PII-safe projection: the model receives header + safe indicators/shape + REDACTED
            # samples only. Raw high-cardinality values (names/emails/phones/ids/free text) never
            # leave the process; enum domains (business taxonomy) are sent because mapping needs
            # them and they are detached from any employee identity. See app/model_projection.py.
            columns=[to_model_safe_column(p) for p in unresolved])
        col_summary = [{"header": c.header,
                        "redaction_class": (c.format_indicators or {}).get("redaction_class")}
                       for c in req.columns]
        try:
            response, meta = await adapter.propose_mappings(schema_public=schema_public, request=req)
        except ModelError as e:
            msg = f"{type(e).__name__}: {e}"
            # Observe the failed call (sanitized): status + error category, no raw prompt.
            record_model_call(db, tracer, job_id=job_id, kind="mapping_proposal", table_id=table_id,
                              adapter_kind=adapter.kind, model_id=model_id,
                              columns_summary=col_summary, meta=None, status="error",
                              error_category=type(e).__name__)
            db.set_job_stage(job_id, status="error", stage="mapping", error=msg)
            db.add_audit(job_id, event_type="model_error", actor="system", reason=msg,
                         after={"table_id": table_id}, model_version=model_id)
            raise
        # Observe the successful call (sanitized): attempts, latency, tokens, counts, PLUS the
        # sanitized header->proposed-target results (a field name + a schema path, never a raw value)
        # so LangSmith/metrics show the actual semantic mapping the model produced (§8).
        proposal_summary = [{"header": it.source_header or "",
                             "proposed_target_field": it.proposed_target_field,
                             "is_ambiguous": bool(it.is_ambiguous)} for it in response.proposals]
        record_model_call(db, tracer, job_id=job_id, kind="mapping_proposal", table_id=table_id,
                          adapter_kind=meta.adapter_kind, model_id=meta.model_id,
                          columns_summary=col_summary, meta=meta, status="ok",
                          n_proposals=len(response.proposals),
                          input_extra={"n_unresolved": len(unresolved),
                                       "target_schema_version": eff.version,
                                       "n_target_paths": len(schema_public.get("target_paths", []))},
                          output_extra={"proposals": proposal_summary})
        unresolved_ids = {p.profile_id for p in unresolved}
        for item in response.proposals:
            if item.source_column_id in unresolved_ids:   # ignore stray/duplicate ids
                db.add_proposal(job_id, proposal=item, profile_id=item.source_column_id,
                                table_id=table_id, adapter_kind=meta.adapter_kind, model_id=meta.model_id)
        db.add_audit(job_id, event_type="proposed", actor="model",
                     after={"table_id": table_id, "n_unresolved": len(unresolved),
                            "n_proposals": len(response.proposals), "adapter_kind": meta.adapter_kind,
                            "attempts": meta.attempts}, model_version=meta.model_id)

    _supersede_settled_proposals(job_id)
    return {"stage": "mapping", "provider_blocked": provider_blocked,
            "blocked_columns": blocked_columns}
