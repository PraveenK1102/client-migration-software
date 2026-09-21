"""Conditional routing for the preparation graph."""
from __future__ import annotations

from .state import PrepState


def route_after_prepare(state: PrepState) -> str:
    """Open record issues -> human record review; otherwise finalize the prepared dataset."""
    return "prepare_record_review" if state.get("open_record_issue_count", 0) > 0 else "finalize_preparation"
