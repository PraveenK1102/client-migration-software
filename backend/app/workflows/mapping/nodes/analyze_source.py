"""analyze_source node: M3C adaptive source intelligence (declarative transform plans)."""
from __future__ import annotations

from ....source_intelligence import analyze_source
from ..context import MappingContext
from ..state import GraphState


async def analyze_source_node(ctx: MappingContext, state: GraphState) -> GraphState:
    """Build declarative transformation plans (date convention + two-digit-year century, enum/boolean
    value maps, referential integrity, manager-name derivation) from full-column evidence. Runs for
    both the blocked and proceeding paths so the intelligence is available even when the job is later
    blocked on provider/proposals."""
    db, adapter, model_id, tracer = ctx.db, ctx.adapter, ctx.model_id, ctx.tracer
    job_id = state["job_id"]
    db.set_job_stage(job_id, status="processing", stage="analyzing_source")
    eff = ctx.eff(job_id)
    await analyze_source(db, job_id, eff, adapter, model_id, tracer=tracer)
    return {"stage": "analyzing_source",
            "provider_blocked": state.get("provider_blocked", False),
            "blocked_columns": state.get("blocked_columns", [])}
