"""finalize_preparation node: enforce the blocked==0 invariant and summarise the eligible dataset."""
from __future__ import annotations

from ....prepare import NORMALIZATION_VERSION, finalize_invariant_error
from ..context import PrepContext
from ..state import PrepState


async def finalize_preparation_node(ctx: PrepContext, state: PrepState) -> PrepState:
    db, schema = ctx.db, ctx.schema
    job_id = state["job_id"]
    candidates = db.get_candidates(job_id)
    eligible = [c for c in candidates if c["eligibility"] == "eligible"]
    blocked = [c for c in candidates if c["eligibility"] == "blocked"]
    excluded = [c for c in candidates if c["eligibility"] == "excluded"]
    rows_processed = sum(t["n_rows"] for t in db.get_tables(job_id))
    open_issues = db.get_record_issues(job_id, status="open")

    # INVARIANT: preparation_complete implies blocked == 0 (checked via a shared helper).
    msg = finalize_invariant_error(candidates, len(open_issues))
    if msg:
        db.set_job_stage(job_id, status="error", stage="preparation_invariant_failed", error=msg)
        db.add_audit(job_id, event_type="preparation_invariant_failed", actor="system",
                     after={"blocked": len(blocked), "open_record_issues": len(open_issues)}, reason=msg)
        return {"stage": "preparation_invariant_failed", "open_record_issue_count": len(open_issues)}

    result_msg = "preparation_complete" if eligible else "preparation_complete_no_eligible"
    summary = {
        "result": result_msg,
        "ready_for_target": len(eligible),
        "counts": {"source_rows_processed": rows_processed, "candidate_employees": len(candidates),
                   "eligible": len(eligible), "blocked": len(blocked), "excluded": len(excluded),
                   "open_record_issues": len(open_issues),
                   "resolved_record_issues": len(db.get_record_issues(job_id, status="resolved"))},
        "normalization_version": NORMALIZATION_VERSION, "schema_version": schema.version,
        "tenant_id": db.job_tenant(job_id),
    }
    db.set_prep_summary(job_id, summary)
    db.set_job_stage(job_id, status="preparation_complete", stage="preparation_complete")
    db.add_audit(job_id, event_type="preparation_complete", actor="system", after=summary["counts"],
                 schema_version=schema.version)
    return {"stage": "preparation_complete", "open_record_issue_count": 0}
