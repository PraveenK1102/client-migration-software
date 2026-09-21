"""Compatibility facade for the LangGraph workflows.

The graph layer was restructured (M3D) into :mod:`app.workflows` (``mapping`` / ``preparation``
sub-packages, each with ``state.py`` / ``context.py`` / ``nodes/`` / ``edges.py`` / ``graph.py``).
This module is a thin, stable re-export so existing imports (``from app.graph import build_graphs,
GraphDeps``) keep working unchanged. There is no logic here; see ``app/workflows/``.
"""
from __future__ import annotations

from .workflows import GraphDeps, build_graphs, build_mapping_graph, build_preparation_graph
from .workflows._support import effective_schema_for_job
from .workflows.mapping.state import GraphState
from .workflows.preparation.state import PrepState

__all__ = [
    "GraphDeps", "build_graphs", "build_mapping_graph", "build_preparation_graph",
    "effective_schema_for_job", "GraphState", "PrepState",
]
