"""Preparation nodes: start + the deterministic record-preparation pass (normalization,
reconciliation, validation). NO model calls here."""
from __future__ import annotations

from ....prepare import prepare
from ..context import PrepContext
from ..state import PrepState


async def prep_start_node(ctx: PrepContext, state: PrepState) -> PrepState:
    db = ctx.db
    job_id = state["job_id"]
    db.set_job_stage(job_id, status="preparing_records", stage="preparing_records")
    db.add_audit(job_id, event_type="preparation_started", actor="system")
    return {"stage": "preparing_records"}


async def prepare_records_node(ctx: PrepContext, state: PrepState) -> PrepState:
    db = ctx.db
    job_id = state["job_id"]
    db.set_job_stage(job_id, status="preparing_records", stage="preparing_records")
    result = prepare(db, job_id, ctx.eff(job_id))
    db.replace_candidates(job_id, result["candidates"])
    keep_ids = set()
    for iss in result["issues"]:
        keep_ids.add(iss["id"])
        db.upsert_record_issue(job_id, issue_id=iss["id"], candidate_key=iss.get("candidate_key"),
                               field=iss.get("field"), issue_type=iss["issue_type"], reason=iss["reason"],
                               options=iss.get("options", []), affected=iss.get("affected", {}),
                               scope=iss.get("scope"))
    db.supersede_open_issues_not_in(job_id, keep_ids)
    open_blocking = [i for i in db.get_record_issues(job_id, status="open")]
    db.add_audit(job_id, event_type="records_prepared", actor="system",
                 after={"candidates": len(result["candidates"]),
                        "open_record_issues": len(open_blocking), **result["metrics"]})
    return {"stage": "preparing_records", "open_record_issue_count": len(open_blocking)}
