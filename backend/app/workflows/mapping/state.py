"""Mapping graph state (kept out of the graph builder on purpose)."""
from __future__ import annotations

from typing import TypedDict


class GraphState(TypedDict, total=False):
    job_id: str
    schema_version: str
    stage: str
    table_ids: list[str]
    open_issue_count: int
    resume_payload: dict
    provider_blocked: bool
    blocked_columns: list[str]
