"""mapping_blocked node: park the job when unresolved columns need a model but none is configured."""
from __future__ import annotations

from ..context import MappingContext
from ..state import GraphState


async def mapping_blocked_node(ctx: MappingContext, state: GraphState) -> GraphState:
    db = ctx.db
    job_id = state["job_id"]
    blocked = state.get("blocked_columns", [])
    msg = (f"Groq is not configured. Deterministic processing is available; "
           f"{len(blocked)} column(s) require model interpretation and cannot proceed until "
           f"GROQ_API_KEY is set in backend/.env. Rule-accepted mappings are preserved; "
           f"restart the backend after configuring, then retry mapping on this job — or decide each "
           f"blocked column under Reviews (create a tenant custom field, map it to a target field, or "
           f"ignore the source field); mapping re-runs automatically once every column is decided.")
    db.set_job_stage(job_id, status="blocked_provider", stage="blocked_provider", error=msg)
    db.add_audit(job_id, event_type="mapping_blocked", actor="system",
                 after={"blocked_columns": blocked}, reason=msg)
    return {"stage": "blocked_provider"}
