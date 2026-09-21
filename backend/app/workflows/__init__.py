"""LangGraph workflows, restructured (M3D) into a clear, navigable shape.

    workflows/
      deps.py                     GraphDeps (explicit shared dependency bundle)
      _support.py                 shared helpers/types (effective schema, table reconstruction)
      mapping/{state,context,edges,graph}.py + nodes/*.py
      preparation/{state,context,edges,graph}.py + nodes/*.py

State lives in ``state.py`` (not the graph builder); node implementations are independent
``node(ctx, state)`` functions; edges/routing are separate; graph construction is concise.
Checkpointer, thread ids, node NAMES, topology, and interrupt/resume semantics are IDENTICAL to the
pre-M3D single-file graph, so persisted jobs resume unchanged. ``app.graph`` remains a thin facade.
"""
from __future__ import annotations

from .deps import GraphDeps
from .mapping import build_mapping_graph
from .preparation import build_preparation_graph


def build_graphs(deps: GraphDeps, checkpointer):
    """Return (mapping_graph, preparation_graph) — same signature/behavior as before the restructure."""
    mapping_graph = build_mapping_graph(deps, checkpointer)
    preparation_graph = build_preparation_graph(deps, checkpointer)
    return mapping_graph, preparation_graph


__all__ = ["GraphDeps", "build_graphs", "build_mapping_graph", "build_preparation_graph"]
