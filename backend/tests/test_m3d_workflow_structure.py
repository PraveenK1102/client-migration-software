"""M3D: the LangGraph layer is restructured (state/nodes/edges/graph separated) with IDENTICAL
node names & topology and a working compatibility facade — so persisted jobs resume unchanged.
"""
from __future__ import annotations

import importlib

import pytest

EXPECTED_MAPPING_NODES = {"profile", "map_columns", "analyze_source", "mapping_blocked", "assess",
                          "prepare_review", "await_review", "apply_decisions", "finalize_mapping"}
EXPECTED_PREP_NODES = {"prep_start", "prepare_records", "prepare_record_review",
                       "await_record_review", "finalize_preparation"}


def test_workflow_package_layout_exists():
    for mod in [
        "app.workflows", "app.workflows.deps", "app.workflows._support",
        "app.workflows.mapping.state", "app.workflows.mapping.context",
        "app.workflows.mapping.edges", "app.workflows.mapping.graph",
        "app.workflows.mapping.nodes",
        "app.workflows.preparation.state", "app.workflows.preparation.context",
        "app.workflows.preparation.edges", "app.workflows.preparation.graph",
        "app.workflows.preparation.nodes",
    ]:
        assert importlib.import_module(mod) is not None


def test_facade_reexports_public_symbols():
    import app.graph as facade
    for sym in ("build_graphs", "GraphDeps", "GraphState", "PrepState", "effective_schema_for_job"):
        assert hasattr(facade, sym), sym
    # State classes are defined in state.py, not in the facade module.
    from app.workflows.mapping.state import GraphState
    from app.workflows.preparation.state import PrepState
    assert facade.GraphState is GraphState and facade.PrepState is PrepState


def _node_names(compiled) -> set[str]:
    names = set(compiled.get_graph().nodes.keys())
    return {n for n in names if not n.startswith("__")}


async def test_compiled_graphs_have_identical_node_names(ctx):
    assert _node_names(ctx.mapping_graph) == EXPECTED_MAPPING_NODES
    assert _node_names(ctx.preparation_graph) == EXPECTED_PREP_NODES


def test_nodes_are_independent_callables():
    # Each node is an importable async function taking (ctx, state) — independently testable.
    from app.workflows.mapping.nodes import profile_node, map_columns_node, assess_node
    from app.workflows.preparation.nodes import prep_start_node
    import inspect
    for fn in (profile_node, map_columns_node, assess_node, prep_start_node):
        assert inspect.iscoroutinefunction(fn)
        params = list(inspect.signature(fn).parameters)
        assert params[:2] == ["ctx", "state"], (fn.__name__, params)
