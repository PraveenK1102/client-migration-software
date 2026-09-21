"""Mapping-graph execution context: the shared dependencies (db/adapter/schema/model/tracer) plus the
helper operations several nodes reuse. Nodes receive a ``MappingContext`` and are otherwise pure
functions of ``(ctx, state)`` — independently readable and unit-testable.

Behavior is identical to the pre-M3D single-file closures; only the plumbing (closure vars -> ctx
attributes/methods) changed.
"""
from __future__ import annotations

import json

from ...custom_fields import proposal_id, suggest_definition
from ...db import Database
from ...llm.base import ModelAdapter
from ...model_projection import redact_values
from ...policy import Decision, POLICY_VERSION
from ...profiling import ColumnProfile
from ...schema_loader import TargetSchema
from .._support import effective_schema_for_job, issue_id_for, profile_obj


class MappingContext:
    def __init__(self, db: Database, adapter: ModelAdapter | None, schema: TargetSchema,
                 model_id: str, tracer) -> None:
        self.db = db
        self.adapter = adapter
        self.schema = schema            # BASE schema (core + collections)
        self.model_id = model_id
        self.tracer = tracer

    # --- effective contract for a job's tenant ---------------------------
    def eff(self, job_id: str) -> TargetSchema:
        return effective_schema_for_job(self.db, self.schema, job_id)

    # --- bounded observed values (display) + all non-empty (type inference) ---
    def observed_values(self, table_id: str, col_index: int) -> tuple[list[str], list[str]]:
        from ...custom_fields import MAX_OBSERVED
        seen: list[str] = []
        allv: list[str] = []
        for r in self.db.get_rows_for_table(table_id):
            cells = json.loads(r["cells"])
            v = cells[col_index]["value"] if col_index < len(cells) else None
            if v is None or str(v).strip() == "":
                continue
            v = str(v).strip()
            allv.append(v)
            if v not in seen and len(seen) < MAX_OBSERVED:
                seen.append(v)
        return seen, allv

    # --- deterministic custom-field PROPOSALS for unresolved columns (never auto-created) ---
    def propose_custom_fields(self, job_id: str, eff: TargetSchema, table_id: str,
                              profiles: list[ColumnProfile], origin: str) -> None:
        db = self.db
        tenant = eff.tenant_id or db.job_tenant(job_id)
        taken = {json.loads(p["suggestion"]).get("key") for p in db.get_custom_field_proposals(job_id)
                 if p["status"] == "open"}
        for p in profiles:
            if p.non_empty_count == 0:
                continue      # an entirely empty column carries no business data to propose
            observed, all_values = self.observed_values(p.table_id, p.col_index)
            # Persist only REDACTED observed values in the proposal + audit (PII-safe): the reviewer
            # sees enum labels where those matter and a source deep-link for the raw values otherwise.
            observed = redact_values(p, observed)
            sug = suggest_definition(p.header, all_values or p.samples, eff, taken)
            taken.add(sug["key"])
            pid = proposal_id(job_id, p.profile_id)
            created = db.upsert_custom_field_proposal(
                job_id, proposal_id=pid, tenant_id=tenant, profile_id=p.profile_id, table_id=table_id,
                source_header=p.header, origin=origin, suggestion=sug, observed_values=observed,
                non_empty_count=p.non_empty_count)
            if created:
                db.add_audit(job_id, event_type="custom_field_proposed", actor="system", issue_id=pid,
                             source_ref={"profile_id": p.profile_id, "header": p.header, "table_id": table_id,
                                         "tenant_id": tenant},
                             after={"suggested": sug, "observed_values": observed, "origin": origin},
                             reason=("No core / collection / existing tenant custom destination for this "
                                     "column; a consultant must create a tenant custom field, map it, or "
                                     "ignore it. Nothing is created automatically."),
                             schema_version=eff.version)

    # --- a proposal is only meaningful while its column has no accepted/ignored decision ---
    def supersede_settled_proposals(self, job_id: str) -> None:
        db = self.db
        settled = {d["profile_id"] for d in db.get_decisions(job_id)
                   if d["status"] in ("auto_accepted", "approved", "corrected", "ignored", "rejected",
                                      "redundant", "reference_hold")}
        keep = {p["id"] for p in db.get_custom_field_proposals(job_id, status="open")
                if p["profile_id"] not in settled}
        db.supersede_open_proposals_not_in(job_id, keep)

    # --- persist one classified model-proposal result (auto-accept / unmapped / needs-review) ---
    def persist_assess_result(self, job_id, table_id, res, existing_dec, eff, profiles) -> None:
        db = self.db
        model_id = self.model_id
        prev = existing_dec.get(res.source_column_id)
        if prev and prev.get("actor") == "human" and prev["status"] in _HUMAN_FINAL:
            return  # a human overlay is never re-decided by the model path
        if prev and prev["status"] in ("redundant", "reference_hold"):
            return  # a structural disposition (M3C) is never overridden by a model proposal
        if res.decision is Decision.AUTO_ACCEPT:
            if prev and prev["status"] == "auto_accepted" and prev["target_field"] == res.target_field:
                return  # idempotent
            tf = eff.get(res.target_field)
            db.upsert_decision(job_id, profile_id=res.source_column_id, table_id=table_id,
                               source_header=res.header, target_field=res.target_field,
                               status="auto_accepted", actor="system", method="model", reason=res.reason,
                               destination_kind=eff.destination_kind(res.target_field),
                               custom_definition_id=(tf.custom_definition_id if tf else None))
            db.add_audit(job_id, event_type="mapping_auto_accepted", actor="system",
                         source_ref={"profile_id": res.source_column_id, "header": res.header, "table_id": table_id},
                         before={"target": None},
                         after={"target": res.target_field, "destination_kind": eff.destination_kind(res.target_field)},
                         reason=res.reason, schema_version=eff.version, policy_version=POLICY_VERSION,
                         model_version=model_id)
        elif res.decision is Decision.UNMAPPED:
            if not (prev and prev["status"] == "unmapped"):
                db.upsert_decision(job_id, profile_id=res.source_column_id, table_id=table_id,
                                   source_header=res.header, target_field=None, status="unmapped",
                                   actor="system", method="none", reason=res.reason, destination_kind="UNMAPPED")
                db.add_audit(job_id, event_type="mapping_unmapped", actor="system",
                             source_ref={"profile_id": res.source_column_id, "header": res.header, "table_id": table_id},
                             after={"target": None}, reason=res.reason, schema_version=eff.version,
                             policy_version=POLICY_VERSION)
            # The model found no destination: record a custom-field PROPOSAL so the column is never
            # silently discarded (a human decides: create custom field / map / ignore).
            prof_row = profiles.get(res.source_column_id)
            if prof_row:
                self.propose_custom_fields(job_id, eff, table_id, [profile_obj(prof_row)], origin="model_unmapped")
        else:
            iid = issue_id_for(res.source_column_id)
            existed = db.get_issue(iid) is not None
            db.upsert_issue(job_id, issue_id=iid, profile_id=res.source_column_id, table_id=table_id,
                            source_header=res.header, issue_type=res.reason.split(":")[0][:60],
                            proposed_target_field=res.target_field,
                            candidate_target_fields=res.candidate_target_fields,
                            evidence_summary=res.evidence_summary,
                            affected_non_empty_rows=res.evidence_summary.get("affected_non_empty_rows", 0))
            if not existed:
                db.add_audit(job_id, event_type="issue_created", actor="system", issue_id=iid,
                             source_ref={"profile_id": res.source_column_id, "header": res.header, "table_id": table_id},
                             after={"candidates": res.candidate_target_fields}, reason=res.reason,
                             schema_version=eff.version, policy_version=POLICY_VERSION)


_HUMAN_FINAL = ("approved", "corrected", "rejected", "ignored")
