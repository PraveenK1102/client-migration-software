"""Typed source-record representation.

This is the boundary between *file parsing* and the *migration workflow*. Parsers
(CSV, XLSX today) emit these types; the workflow never touches raw files again.

Design intent from the milestone brief:
- Preserve raw fields exactly (no coercion, no lost leading zeros).
- Carry provenance (``SourceRef``) precise enough to point a reviewer at the exact
  origin of any value.
- ``SourceRef`` already reserves ``page`` / ``region`` so future adapters (PDF,
  scanned images) can locate values on a page WITHOUT changing this schema. Those
  adapters are intentionally NOT implemented in Milestone 1.
- Record parsing problems as data (``ParsingIssue``) instead of raising/guessing.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class RawType(str, Enum):
    """Best-effort classification of a raw cell, without coercing its value."""

    TEXT = "text"
    NUMBER = "number"
    BOOL = "bool"
    DATE = "date"
    FORMULA = "formula"   # flagged as unsupported in M1; cached values are not trusted
    EMPTY = "empty"


class SourceRef(BaseModel):
    """Where a value came from. Precise enough for a reviewer to trace it.

    ``page`` and ``region`` are reserved for future page/region-based adapters
    (PDF, scanned docs). They are always ``None`` in Milestone 1.
    """

    file_id: str
    original_filename: str
    table_id: str
    sheet_name: str | None = None       # worksheet name for XLSX; None for CSV
    row_number: int | None = None       # 1-based data-row number within the table
    col_index: int | None = None        # 0-based column position (preserves duplicate headers)
    header: str | None = None
    # --- reserved for future adapters; not populated by CSV/XLSX parsers ---
    page: int | None = None
    region: dict | None = None


class RawCell(BaseModel):
    col_index: int
    header: str
    value: str | None                   # raw string exactly as read (None only when truly empty)
    raw_type: RawType
    ref: SourceRef


class SourceRecord(BaseModel):
    """One source row, with all of its raw cells and full provenance."""

    ref: SourceRef
    cells: list[RawCell] = Field(default_factory=list)


class SourceTable(BaseModel):
    """One logical table: a CSV file, or one worksheet of an XLSX file."""

    table_id: str
    file_id: str
    original_filename: str
    sheet_name: str | None = None
    headers: list[str] = Field(default_factory=list)
    n_rows: int = 0


class ParsingIssue(BaseModel):
    """A problem found while parsing, recorded as data (never a silent transform)."""

    ref: SourceRef
    kind: str                           # e.g. duplicate_header | formula_cell | possible_zero_loss
    detail: str
    severity: str = "warning"           # info | warning | error


class ParsedFile(BaseModel):
    """Everything produced from a single uploaded file."""

    file_id: str
    original_filename: str
    content_type: str | None = None
    stored_name: str
    size_bytes: int
    tables: list[SourceTable] = Field(default_factory=list)
    records: list[SourceRecord] = Field(default_factory=list)
    issues: list[ParsingIssue] = Field(default_factory=list)


class UnsupportedFormatError(ValueError):
    """Raised for a file whose format Milestone 1 does not support (only .csv/.xlsx)."""


class UploadTooLargeError(ValueError):
    """Raised when a file exceeds the configured byte limit."""


class RowLimitExceededError(ValueError):
    """Raised when a source table exceeds the configured hard row limit.

    The file is REJECTED, never silently truncated: a migration must ingest the
    complete accepted employee population or fail loudly. No partial rows are
    persisted from a file that raised this. Detected while reading (at limit+1),
    so an arbitrarily huge table is not fully materialised just to reject it.
    """
