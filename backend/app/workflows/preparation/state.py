"""Preparation graph state."""
from __future__ import annotations

from typing import TypedDict


class PrepState(TypedDict, total=False):
    job_id: str
    stage: str
    open_record_issue_count: int
    resume_payload: dict
