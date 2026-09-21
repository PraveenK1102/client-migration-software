"""Mapping graph construction (concise): wire named nodes + edges over a MappingContext.

Node NAMES, topology, the single ``await_review`` interrupt, and the checkpointer are IDENTICAL to
the pre-M3D single-file graph, so existing checkpoints/threads resume unchanged. Nodes are bound to
the context with ``functools.partial`` (LangGraph awaits the resulting coroutine function).
"""
from __future__ import annotations

from functools import partial

from langgraph.graph import END, START, StateGraph

from ...observability import ModelTracer
from ..deps import GraphDeps
from .context import MappingContext
from .edges import route_after_assess, route_after_map
from .nodes import (
    analyze_source_node,
    apply_decisions_node,
    assess_node,
    await_review_node,
    finalize_node,
    map_columns_node,
    mapping_blocked_node,
    prepare_review_node,
    profile_node,
)
from .state import GraphState


def build_mapping_graph(deps: GraphDeps, checkpointer):
    ctx = MappingContext(db=deps.db, adapter=deps.adapter, schema=deps.schema,
                         model_id=(deps.adapter.model_id if deps.adapter is not None else deps.model_id),
                         tracer=deps.tracer or ModelTracer())

    def _n(fn):
        return partial(fn, ctx)

    mg = StateGraph(GraphState)
    mg.add_node("profile", _n(profile_node))
    mg.add_node("map_columns", _n(map_columns_node))
    mg.add_node("analyze_source", _n(analyze_source_node))
    mg.add_node("mapping_blocked", _n(mapping_blocked_node))
    mg.add_node("assess", _n(assess_node))
    mg.add_node("prepare_review", _n(prepare_review_node))
    mg.add_node("await_review", _n(await_review_node))
    mg.add_node("apply_decisions", _n(apply_decisions_node))
    mg.add_node("finalize_mapping", _n(finalize_node))
    mg.add_edge(START, "profile")
    mg.add_edge("profile", "map_columns")
    mg.add_edge("map_columns", "analyze_source")
    mg.add_conditional_edges("analyze_source", route_after_map,
                             {"mapping_blocked": "mapping_blocked", "assess": "assess"})
    mg.add_edge("mapping_blocked", END)
    mg.add_conditional_edges("assess", route_after_assess,
                             {"prepare_review": "prepare_review", "finalize_mapping": "finalize_mapping"})
    mg.add_edge("prepare_review", "await_review")
    mg.add_edge("await_review", "apply_decisions")
    mg.add_conditional_edges("apply_decisions", route_after_assess,
                             {"prepare_review": "prepare_review", "finalize_mapping": "finalize_mapping"})
    mg.add_edge("finalize_mapping", END)
    return mg.compile(checkpointer=checkpointer)
