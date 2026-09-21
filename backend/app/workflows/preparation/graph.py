"""Preparation graph construction (concise). Node names, topology, the single
``await_record_review`` interrupt, and the checkpointer are IDENTICAL to the pre-M3D graph."""
from __future__ import annotations

from functools import partial

from langgraph.graph import END, START, StateGraph

from ..deps import GraphDeps
from .context import PrepContext
from .edges import route_after_prepare
from .nodes import (
    await_record_review_node,
    finalize_preparation_node,
    prep_start_node,
    prepare_record_review_node,
    prepare_records_node,
)
from .state import PrepState


def build_preparation_graph(deps: GraphDeps, checkpointer):
    ctx = PrepContext(db=deps.db, schema=deps.schema)

    def _n(fn):
        return partial(fn, ctx)

    pg = StateGraph(PrepState)
    pg.add_node("prep_start", _n(prep_start_node))
    pg.add_node("prepare_records", _n(prepare_records_node))
    pg.add_node("prepare_record_review", _n(prepare_record_review_node))
    pg.add_node("await_record_review", _n(await_record_review_node))
    pg.add_node("finalize_preparation", _n(finalize_preparation_node))
    pg.add_edge(START, "prep_start")
    pg.add_edge("prep_start", "prepare_records")
    pg.add_conditional_edges("prepare_records", route_after_prepare,
                             {"prepare_record_review": "prepare_record_review",
                              "finalize_preparation": "finalize_preparation"})
    pg.add_edge("prepare_record_review", "await_record_review")
    pg.add_edge("await_record_review", "prepare_records")
    pg.add_edge("finalize_preparation", END)
    return pg.compile(checkpointer=checkpointer)
