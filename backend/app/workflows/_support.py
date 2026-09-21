"""Shared dependencies/types for the workflow graphs (explicit, not buried in a graph builder).

These helpers are used by both the mapping and preparation graphs' nodes and by the compatibility
facade ``app.graph``. Behavior is identical to the pre-M3D single-file implementation.
"""
from __future__ import annotations

import json

from ..db import Database
from ..profiling import ColumnProfile, profile_from_row
from ..schema_loader import TargetSchema
from ..source_records import RawCell, SourceRecord, SourceRef, SourceTable

# A persisted HUMAN decision in one of these states is an overlay a re-run never re-evaluates.
HUMAN_FINAL = ("approved", "corrected", "rejected", "ignored")


def effective_schema_for_job(db: Database, base: TargetSchema, job_id: str) -> TargetSchema:
    """core + collections (base file) + the job tenant's persisted custom-field definitions."""
    tenant = db.job_tenant(job_id)
    return base.with_custom_definitions(db.get_custom_field_definitions(tenant), tenant)


def issue_id_for(profile_id: str) -> str:
    return f"iss_{profile_id}"


def profile_obj(row: dict) -> ColumnProfile:
    return profile_from_row(row)


def reconstruct_table_and_records(db: Database, table_row: dict):
    table = SourceTable(table_id=table_row["id"], file_id=table_row["file_id"],
                        original_filename=table_row["original_filename"], sheet_name=table_row["sheet_name"],
                        headers=json.loads(table_row["headers"]), n_rows=table_row["n_rows"])
    records: list[SourceRecord] = []
    for r in db.get_rows_for_table(table.table_id):
        cells = [RawCell.model_validate(c) for c in json.loads(r["cells"])]
        ref = SourceRef(file_id=table.file_id, original_filename=table.original_filename,
                        table_id=table.table_id, sheet_name=table.sheet_name, row_number=r["row_number"])
        records.append(SourceRecord(ref=ref, cells=cells))
    return table, records
