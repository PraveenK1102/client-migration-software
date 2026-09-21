"""profile node: build per-column profile.v2 for every source table."""
from __future__ import annotations

from ....profiling import profile_table
from ..._support import reconstruct_table_and_records
from ..context import MappingContext
from ..state import GraphState


async def profile_node(ctx: MappingContext, state: GraphState) -> GraphState:
    db, schema = ctx.db, ctx.schema
    job_id = state["job_id"]
    db.set_job_stage(job_id, status="processing", stage="profiling")
    table_ids: list[str] = []
    for trow in db.get_tables(job_id):
        table, records = reconstruct_table_and_records(db, trow)
        profiles = profile_table(table, records, max_samples=5)
        db.add_profiles(job_id, profiles)
        table_ids.append(table.table_id)
    db.add_audit(job_id, event_type="profiled", actor="system",
                 after={"tables": len(table_ids)}, schema_version=schema.version)
    return {"stage": "profiling", "table_ids": table_ids}
