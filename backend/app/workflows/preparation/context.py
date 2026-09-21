"""Preparation-graph execution context (deterministic; NO model calls in M2/preparation)."""
from __future__ import annotations

from ...db import Database
from ...schema_loader import TargetSchema
from .._support import effective_schema_for_job


class PrepContext:
    def __init__(self, db: Database, schema: TargetSchema) -> None:
        self.db = db
        self.schema = schema            # BASE schema (used for schema.version in the summary)

    def eff(self, job_id: str) -> TargetSchema:
        return effective_schema_for_job(self.db, self.schema, job_id)
