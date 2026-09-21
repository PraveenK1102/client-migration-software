"""assess node: classify model proposals into auto-accept / unmapped / needs-review (deterministic
policy — never the model's self-reported confidence), resolving intra-table conflicts."""
from __future__ import annotations

import json

from ....llm.schema import ProposalItem
from ....policy import Decision, classify_proposal, resolve_table_conflicts
from ..._support import profile_obj as _profile_obj
from ..context import MappingContext
from ..state import GraphState


async def assess_node(ctx: MappingContext, state: GraphState) -> GraphState:
    db = ctx.db
    job_id = state["job_id"]
    db.set_job_stage(job_id, status="processing", stage="assessing")
    eff = ctx.eff(job_id)
    profiles = {p["id"]: p for p in db.get_profiles(job_id)}
    proposals = db.get_proposals(job_id)
    existing_dec = {d["profile_id"]: d for d in db.get_decisions(job_id)}
    # Targets already taken by rule/human decisions per table (conflict detection).
    rule_targets: dict[str, set[str]] = {}
    for d in db.get_decisions(job_id):
        if d["target_field"] and d["status"] in ("auto_accepted", "approved", "corrected"):
            rule_targets.setdefault(d["table_id"], set()).add(d["target_field"])

    by_table: dict[str, list[tuple[dict, dict]]] = {}
    for pr in proposals:
        prof = profiles.get(pr["profile_id"])
        if prof:
            by_table.setdefault(pr["table_id"], []).append((pr, prof))

    for table_id, pairs in by_table.items():
        results = []
        for pr, prof_row in pairs:
            item = ProposalItem(
                source_column_id=pr["profile_id"], source_header=pr["source_header"],
                proposed_target_field=pr["proposed_target_field"],
                alternative_target_fields=json.loads(pr["alternatives"]),
                is_ambiguous=bool(pr["is_ambiguous"]), ambiguity_reason=pr["ambiguity_reason"],
                evidence=json.loads(pr["evidence"]), confidence=pr["confidence"])
            results.append(classify_proposal(item, _profile_obj(prof_row), eff))
        results = resolve_table_conflicts(results)

        for res in results:
            # A model proposal cannot overwrite a rule-accepted target in the same table.
            if (res.decision is Decision.AUTO_ACCEPT and res.target_field
                    and res.target_field in rule_targets.get(table_id, set())):
                res.decision = Decision.NEEDS_REVIEW
                res.reason = (f"Conflict: model proposed '{res.target_field}' which a deterministic "
                              f"rule already accepted in this table; escalating rather than overwriting.")
                res.candidate_target_fields = [res.target_field]
            ctx.persist_assess_result(job_id, table_id, res, existing_dec, eff, profiles)

    ctx.supersede_settled_proposals(job_id)
    open_count = len(db.get_issues(job_id, status="open"))
    return {"stage": "assessing", "open_issue_count": open_count}
