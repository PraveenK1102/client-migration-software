"""Conditional routing for the mapping graph (kept separate from node logic and graph wiring)."""
from __future__ import annotations

from .state import GraphState


def route_after_map(state: GraphState) -> str:
    """After source analysis: park if the provider is unconfigured, else assess proposals."""
    return "mapping_blocked" if state.get("provider_blocked") else "assess"


def route_after_assess(state: GraphState) -> str:
    """Open review issues -> human review; otherwise finalize the mapping."""
    return "prepare_review" if state.get("open_issue_count", 0) > 0 else "finalize_mapping"
