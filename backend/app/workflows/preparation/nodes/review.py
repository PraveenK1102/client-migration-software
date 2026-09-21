"""Record-review nodes: prepare the record-review queue and interrupt for a human decision.

``await_record_review`` is the only interrupt point in the preparation graph (side-effect-free).
"""
from __future__ import annotations

from langgraph.types import interrupt

from ..context import PrepContext
from ..state import PrepState


async def prepare_record_review_node(ctx: PrepContext, state: PrepState) -> PrepState:
    db = ctx.db
    job_id = state["job_id"]
    n = len(db.get_record_issues(job_id, status="open"))
    db.set_job_stage(job_id, status="awaiting_record_review", stage="awaiting_record_review")
    db.add_audit(job_id, event_type="record_review_prepared", actor="system", after={"open": n})
    return {"stage": "awaiting_record_review", "open_record_issue_count": n}


async def await_record_review_node(ctx: PrepContext, state: PrepState) -> PrepState:
    db = ctx.db
    job_id = state["job_id"]
    open_ids = [i["id"] for i in db.get_record_issues(job_id, status="open")]
    resume_value = interrupt({"job_id": job_id, "reason": "record_review_required",
                              "open_record_issue_ids": open_ids})
    return {"resume_payload": resume_value or {}, "stage": "resuming_records"}
