"""Small, consistent SQLite access layer.

One connection per :class:`Database` instance (single-worker prototype), guarded by
a lock, with short transactions (a commit per write). No transaction is ever held
open across a model call or a human wait. Complex fields are stored as JSON text.

Two SQLite files are used by the app overall: this application database, and a
separate LangGraph checkpoint database (managed by the checkpointer). Both live
under the gitignored ``data/`` directory.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    adapter_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    error TEXT,
    summary TEXT,
    map_thread TEXT,
    prep_thread TEXT,
    prep_summary TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_files (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    content_type TEXT,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_tables (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    sheet_name TEXT,
    headers TEXT NOT NULL,
    n_rows INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS source_rows (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    table_id TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    cells TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS parsing_issues (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    table_id TEXT,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    severity TEXT NOT NULL,
    ref TEXT
);
CREATE TABLE IF NOT EXISTS column_profiles (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    table_id TEXT NOT NULL,
    col_index INTEGER NOT NULL,
    header TEXT NOT NULL,
    non_empty_count INTEGER NOT NULL,
    missing_count INTEGER NOT NULL,
    distinct_count INTEGER NOT NULL,
    observed_types TEXT NOT NULL,
    format_indicators TEXT NOT NULL,
    samples TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mapping_proposals (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    table_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    source_header TEXT NOT NULL,
    proposed_target_field TEXT,
    alternatives TEXT NOT NULL,
    is_ambiguous INTEGER NOT NULL,
    ambiguity_reason TEXT,
    evidence TEXT NOT NULL,
    confidence REAL,
    adapter_kind TEXT NOT NULL,
    model_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mapping_decisions (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    table_id TEXT NOT NULL,
    source_header TEXT NOT NULL,
    target_field TEXT,
    status TEXT NOT NULL,          -- auto_accepted | approved | corrected | rejected | unmapped
    actor TEXT NOT NULL,           -- system | model | human
    method TEXT,                   -- rule | model | human | none  (routing provenance)
    reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(job_id, profile_id)
);
CREATE TABLE IF NOT EXISTS review_issues (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    table_id TEXT NOT NULL,
    source_header TEXT NOT NULL,
    issue_type TEXT NOT NULL,
    proposed_target_field TEXT,
    candidate_target_fields TEXT NOT NULL,
    evidence_summary TEXT NOT NULL,
    affected_non_empty_rows INTEGER NOT NULL,
    status TEXT NOT NULL,          -- open | resolved
    version INTEGER NOT NULL,
    resolution TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    issue_id TEXT,
    work_item_id TEXT,
    source_ref TEXT,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    before TEXT,
    after TEXT,
    reason TEXT,
    schema_version TEXT,
    policy_version TEXT,
    model_version TEXT,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prepared_candidates (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    business_key TEXT,             -- normalized employee_id or NULL (untrusted/missing)
    eligibility TEXT NOT NULL,     -- eligible | blocked | excluded
    record TEXT NOT NULL,          -- JSON: per-field normalized value + status + provenance
    source_refs TEXT NOT NULL,     -- JSON list of contributing source refs
    issue_ids TEXT,                -- JSON list of blocking record-issue ids
    exclude_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS record_issues (
    id TEXT PRIMARY KEY,           -- deterministic (idempotent across recompute)
    job_id TEXT NOT NULL,
    candidate_key TEXT,            -- business key / candidate association
    field TEXT,
    issue_type TEXT NOT NULL,      -- ambiguous_date|value_conflict|missing_required|unknown_enum|shared_email|invalid_value|unresolved_flag
    reason TEXT NOT NULL,
    options TEXT NOT NULL,         -- JSON supported alternatives
    affected TEXT NOT NULL,        -- JSON affected rows/candidates
    scope TEXT,                    -- JSON scope (table/column/value) for bulk resolution
    status TEXT NOT NULL,          -- open | resolved | superseded
    version INTEGER NOT NULL,
    resolution TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tables_job ON source_tables(job_id);
CREATE INDEX IF NOT EXISTS idx_rows_table ON source_rows(table_id);
CREATE INDEX IF NOT EXISTS idx_profiles_job ON column_profiles(job_id);
CREATE INDEX IF NOT EXISTS idx_proposals_job ON mapping_proposals(job_id);
CREATE INDEX IF NOT EXISTS idx_decisions_job ON mapping_decisions(job_id);
CREATE INDEX IF NOT EXISTS idx_issues_job ON review_issues(job_id);
CREATE INDEX IF NOT EXISTS idx_audit_job ON audit_events(job_id);
CREATE INDEX IF NOT EXISTS idx_cand_job ON prepared_candidates(job_id);
CREATE INDEX IF NOT EXISTS idx_recissue_job ON record_issues(job_id);

-- M3A: durable work queue (single-node; claimed via an atomic conditional UPDATE).
CREATE TABLE IF NOT EXISTS work_items (
    id                  TEXT PRIMARY KEY,
    job_id              TEXT NOT NULL,
    source_file_id      TEXT,
    kind                TEXT NOT NULL,
    payload             TEXT NOT NULL DEFAULT '{}',
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending|processing|retryable|succeeded|failed|cancelled
    attempt             INTEGER NOT NULL DEFAULT 0,
    max_attempts        INTEGER NOT NULL DEFAULT 5,
    available_at        TEXT NOT NULL,
    started_at          TEXT,
    completed_at        TEXT,
    worker_id           TEXT,
    lease_expires_at    TEXT,
    last_error_category TEXT,
    last_error          TEXT,
    idempotency_key     TEXT NOT NULL UNIQUE,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_work_claim ON work_items(status, available_at);
CREATE INDEX IF NOT EXISTS idx_work_job ON work_items(job_id);

-- M3A: snapshot of the confirmed target system seen at reconciliation time.
CREATE TABLE IF NOT EXISTS target_snapshots (
    id               TEXT PRIMARY KEY,
    job_id           TEXT NOT NULL,
    candidate_id     TEXT NOT NULL,
    business_key     TEXT,
    target_record_id TEXT,
    match_basis      TEXT NOT NULL,
    target_revision  INTEGER,
    target_payload   TEXT,
    fetched_at       TEXT NOT NULL,
    UNIQUE(job_id, candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_target_snap_job ON target_snapshots(job_id);

-- M3A: deterministic reconciliation outcome per candidate (replaced each run).
CREATE TABLE IF NOT EXISTS target_reconciliation (
    id               TEXT PRIMARY KEY,
    job_id           TEXT NOT NULL,
    candidate_id     TEXT NOT NULL,
    business_key     TEXT,
    outcome          TEXT NOT NULL,   -- READY_CREATE|READY_UPDATE|NO_CHANGE|REVIEW_REQUIRED|EXCLUDED
    target_record_id TEXT,
    target_revision  INTEGER,
    match_basis      TEXT NOT NULL,
    diff             TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE(job_id, candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_target_recon_job ON target_reconciliation(job_id);

-- M3A: human-review issues for unsafe incoming-vs-target conflicts (separate from record_issues).
CREATE TABLE IF NOT EXISTS target_review_issues (
    id             TEXT PRIMARY KEY,
    job_id         TEXT NOT NULL,
    candidate_id   TEXT NOT NULL,
    business_key   TEXT,
    field          TEXT,
    issue_type     TEXT NOT NULL,
    reason         TEXT NOT NULL,
    incoming_value TEXT,
    target_value   TEXT,
    match_basis    TEXT NOT NULL,
    options        TEXT NOT NULL,
    affected       TEXT NOT NULL,
    status         TEXT NOT NULL,
    version        INTEGER NOT NULL,
    resolution     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_target_review_job ON target_review_issues(job_id, status);

-- M3A.1: immutable, append-only employee record versions (distinct from the audit stream).
-- A version is the complete effective desired employee snapshot at a decision point. Previous
-- versions are NEVER mutated; a material change appends a new version. Idempotent via record_hash.
CREATE TABLE IF NOT EXISTS employee_versions (
    id                 TEXT PRIMARY KEY,
    job_id             TEXT NOT NULL,
    candidate_id       TEXT NOT NULL,
    business_key       TEXT,
    version_no         INTEGER NOT NULL,
    parent_version_id  TEXT,
    origin             TEXT NOT NULL,   -- existing_target | migration | human | rollback
    snapshot           TEXT NOT NULL,   -- JSON {field: value} complete effective record
    record_hash        TEXT NOT NULL,   -- sha256 of the canonical snapshot (dedup/integrity)
    change_reason      TEXT,
    decision_note      TEXT,
    decision_id        TEXT,            -- record/target review issue id or audit id
    target_revision    INTEGER,         -- set when the version is a confirmed-target baseline
    restores_version_id TEXT,           -- set for a future M3B rollback (linkage groundwork)
    field_changes      TEXT,            -- JSON [{field, from, to, provenance, method}]
    created_by         TEXT NOT NULL,   -- system | human
    created_at         TEXT NOT NULL,
    UNIQUE(job_id, candidate_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_empver ON employee_versions(job_id, candidate_id, version_no);

-- M3A.2: tenants (customer scope). A custom field belongs to exactly one tenant's configuration.
CREATE TABLE IF NOT EXISTS tenants (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- M3A.2: tenant-scoped custom-field definitions (core schema + these = effective schema).
-- Persisted separately from the versioned core schema file; never shared across tenants.
CREATE TABLE IF NOT EXISTS custom_field_definitions (
    id                 TEXT PRIMARY KEY,
    tenant_id          TEXT NOT NULL,
    key                TEXT NOT NULL,          -- stable machine key within the tenant
    label              TEXT NOT NULL,
    type               TEXT NOT NULL,          -- string|number|boolean|date|enum|multiselect
    required           INTEGER NOT NULL DEFAULT 0,
    options            TEXT,                   -- JSON list for enum/multiselect
    multi_value        INTEGER NOT NULL DEFAULT 0,
    description        TEXT,
    aliases            TEXT,                   -- JSON list of explicitly-declared header synonyms
    origin             TEXT NOT NULL,          -- seed | proposal | api
    origin_proposal_id TEXT,
    origin_job_id      TEXT,
    created_by         TEXT NOT NULL,          -- system | human
    created_at         TEXT NOT NULL,
    UNIQUE(tenant_id, key)
);
CREATE INDEX IF NOT EXISTS idx_cfd_tenant ON custom_field_definitions(tenant_id);

-- M3A.2: custom-field PROPOSALS for unmapped source columns. A proposal never mutates the target
-- schema by itself; a human must approve (create definition), map to an existing custom field,
-- map to a target path, or explicitly ignore the source field. Deterministic id per (job, column).
CREATE TABLE IF NOT EXISTS custom_field_proposals (
    id              TEXT PRIMARY KEY,
    job_id          TEXT NOT NULL,
    tenant_id       TEXT NOT NULL,
    profile_id      TEXT NOT NULL,
    table_id        TEXT NOT NULL,
    source_header   TEXT NOT NULL,
    origin          TEXT NOT NULL,             -- no_provider | model_unmapped | human
    suggestion      TEXT NOT NULL,             -- JSON {key,label,type,options,multi_value,required}
    observed_values TEXT NOT NULL,             -- JSON bounded distinct sample values
    non_empty_count INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,             -- open | approved | mapped_existing | mapped_target | ignored | superseded
    version         INTEGER NOT NULL,
    resolution      TEXT,
    definition_id   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cfp_job ON custom_field_proposals(job_id, status);
CREATE INDEX IF NOT EXISTS idx_rows_table_rownum ON source_rows(table_id, row_number);

-- M3B: durable delivery plan — one operation per READY_CREATE / READY_UPDATE candidate.
-- Persisted BEFORE any target write. Statuses track the full lifecycle including retry + rollback.
CREATE TABLE IF NOT EXISTS delivery_operations (
    id                      TEXT PRIMARY KEY,
    job_id                  TEXT NOT NULL,
    candidate_id            TEXT NOT NULL,
    employee_id             TEXT,                          -- target employee id (from recon or creation)
    op_type                 TEXT NOT NULL,                 -- CREATE | UPDATE
    payload                 TEXT NOT NULL,                 -- JSON: full record (CREATE) or patch (UPDATE)
    expected_target_revision INTEGER,                      -- NULL for CREATE; required for UPDATE
    target_record_id        TEXT,                          -- if known (UPDATE always has it)
    before_snapshot         TEXT,                          -- JSON: target state before write (UPDATE only)
    desired_version_id      TEXT,                          -- employee_versions.id this operation delivers
    idempotency_key         TEXT NOT NULL UNIQUE,
    status                  TEXT NOT NULL DEFAULT 'PLANNED', -- PLANNED|PROCESSING|SUCCEEDED|RETRYABLE|FAILED|STALE_TARGET|ROLLBACK_PLANNED|ROLLED_BACK|ROLLBACK_FAILED
    attempt_count           INTEGER NOT NULL DEFAULT 0,
    last_error              TEXT,
    target_revision_after   INTEGER,                       -- confirmed revision AFTER a successful write
    target_request_id       TEXT,                          -- target's request-id from the last accepted call
    work_item_id            TEXT,                          -- current delivery work-item
    rollback_work_item_id   TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    UNIQUE(job_id, candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_delop_job ON delivery_operations(job_id, status);

-- M3B: append-only attempt ledger — every HTTP request (deliver or rollback) logged here.
CREATE TABLE IF NOT EXISTS delivery_attempts (
    id              TEXT PRIMARY KEY,
    operation_id    TEXT NOT NULL,
    attempt_no      INTEGER NOT NULL,
    action          TEXT NOT NULL,                          -- DELIVER | ROLLBACK
    started_at      TEXT NOT NULL,
    completed_at    TEXT,
    http_status     INTEGER,
    retryable       INTEGER,                               -- 0 or 1 (NULL if not yet completed)
    error_category  TEXT,
    retry_after     REAL,
    target_request_id TEXT,
    response_meta   TEXT,                                  -- JSON: sanitized response metadata
    result          TEXT NOT NULL DEFAULT 'pending',        -- pending|success|retryable|terminal|conflict
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_delattempt_op ON delivery_attempts(operation_id, attempt_no);

-- M3C: declarative transformation plans (transform.v1) — the persisted, versioned, auditable plan
-- that deterministic preparation executes. One plan per (job, source column, target).
CREATE TABLE IF NOT EXISTS transformation_plans (
    id              TEXT PRIMARY KEY,
    job_id          TEXT NOT NULL,
    table_id        TEXT NOT NULL,
    profile_id      TEXT NOT NULL,
    source_header   TEXT NOT NULL,
    target_field    TEXT,
    kind            TEXT NOT NULL,          -- date|enum|boolean|reference|redundant|number|string
    operations      TEXT NOT NULL,          -- JSON list of whitelisted ops (never code)
    origin          TEXT NOT NULL,          -- deterministic|model|human
    status          TEXT NOT NULL,          -- auto_accepted|needs_review|approved|rejected
    evidence        TEXT,                   -- JSON evidence supporting the plan
    affected_rows   INTEGER NOT NULL DEFAULT 0,
    review_prompt   TEXT,
    review_options  TEXT,                   -- JSON choices for a scoped human review
    resolution      TEXT,                   -- JSON human decision
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
-- M3C: proven code/display column relationships (rel.v1).
CREATE TABLE IF NOT EXISTS column_relationships (
    id                TEXT PRIMARY KEY,
    job_id            TEXT NOT NULL,
    table_id          TEXT NOT NULL,
    relationship      TEXT NOT NULL,        -- one_to_one|code_display|inconsistent
    code_profile_id   TEXT,
    label_profile_id  TEXT,
    evidence          TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
-- M3D: per model-call observability metrics (one row per proposal/transform call). SANITIZED only:
-- counts + timing + token usage + status. NEVER raw prompt/values (see model_projection).
CREATE TABLE IF NOT EXISTS model_calls (
    id                 TEXT PRIMARY KEY,
    job_id             TEXT NOT NULL,
    kind               TEXT NOT NULL,        -- mapping_proposal | transform_proposal
    table_id           TEXT,
    adapter_kind       TEXT NOT NULL,        -- groq | fake
    model_id           TEXT NOT NULL,
    attempts           INTEGER NOT NULL DEFAULT 0,
    latency_ms         REAL NOT NULL DEFAULT 0,
    prompt_tokens      INTEGER,
    completion_tokens  INTEGER,
    total_tokens       INTEGER,
    status             TEXT NOT NULL,        -- ok | error
    error_category     TEXT,
    n_columns          INTEGER NOT NULL DEFAULT 0,
    n_proposals        INTEGER,
    created_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stage_timings (
    id          TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL,
    stage       TEXT NOT NULL,        -- parse_stage | mapping | preparation | reconcile | delivery
    duration_ms REAL NOT NULL DEFAULT 0,
    detail      TEXT,                 -- optional JSON (e.g. resume vs first run)
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tplans_job ON transformation_plans(job_id);
CREATE INDEX IF NOT EXISTS idx_colrel_job ON column_relationships(job_id);
CREATE INDEX IF NOT EXISTS idx_modelcalls_job ON model_calls(job_id);
CREATE INDEX IF NOT EXISTS idx_stagetimings_job ON stage_timings(job_id);
"""

# Columns added after the initial M1 schema; migrated onto existing job databases.
_JOBS_MIGRATION_COLUMNS = {"map_thread": "TEXT", "prep_thread": "TEXT", "prep_summary": "TEXT",
                           "recon_thread": "TEXT", "recon_summary": "TEXT",
                           "delivery_summary": "TEXT"}
_SOURCE_FILE_MIGRATION_COLUMNS = {"blob_key": "TEXT", "sha256": "TEXT",
                                  "storage_status": "TEXT", "parse_status": "TEXT",
                                  "parse_error": "TEXT"}
# M3A.2 additive columns (older databases are migrated in place; defaults keep old rows valid).
_DECISION_MIGRATION_COLUMNS = {"destination_kind": "TEXT", "path_meta": "TEXT",
                               "custom_definition_id": "TEXT", "note": "TEXT"}
_CANDIDATE_MIGRATION_COLUMNS = {"collections": "TEXT", "custom_attributes": "TEXT"}

# M3I: job-scoped DERIVED state whose correctness depends on the COMPLETE source set. Cleared on a
# source-set rebuild (removing a source file) and on migration delete. NOT included: tenants and
# custom_field_definitions (tenant/organization schema is preserved), audit_events (immutable trail),
# jobs (tombstoned, not dropped), and the raw source_files/tables/rows (handled explicitly so the
# REMAINING files survive a rebuild). delivery_attempts is keyed by operation_id, so it is cleared via
# its parent delivery_operations separately.
_DERIVED_TABLES_BY_JOB = (
    "mapping_proposals", "mapping_decisions", "review_issues", "prepared_candidates", "record_issues",
    "transformation_plans", "column_relationships", "custom_field_proposals", "target_snapshots",
    "target_reconciliation", "target_review_issues", "employee_versions", "model_calls",
    "stage_timings", "column_profiles",
)

# EVERY table that stores rows for a single migration (job). A COMPLETE migration delete purges each of
# these (all carry a job_id column), then delivery_attempts (linked via its parent operation), then the
# jobs row itself. Organization-scoped tables — tenants and custom_field_definitions — are NEVER listed
# here: they are shared across a client's migrations and must survive. This is the full source set +
# the derived set + the raw staging tables + work_items + the audit trail.
_ALL_JOB_TABLES = _DERIVED_TABLES_BY_JOB + (
    "delivery_operations", "source_rows", "parsing_issues", "source_tables", "source_files",
    "work_items", "audit_events",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _nid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def _def_row(row) -> dict:
    """Decode a custom_field_definitions row (JSON columns -> lists, ints -> bools)."""
    d = dict(row)
    d["options"] = json.loads(d["options"]) if d.get("options") else None
    d["aliases"] = json.loads(d["aliases"]) if d.get("aliases") else []
    d["required"] = bool(d.get("required"))
    d["multi_value"] = bool(d.get("multi_value"))
    return d


class Database:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")           # concurrent readers + one writer
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)};")  # wait, don't fail, on a lock
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")         # safe with WAL, less fsync stall
        self._lock = threading.Lock()
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after M1 to pre-existing job databases."""
        def add_missing(table: str, cols: dict[str, str]) -> None:
            existing = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for col, coltype in cols.items():
                if col not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")

        add_missing("jobs", _JOBS_MIGRATION_COLUMNS)
        add_missing("jobs", {"tenant_id": "TEXT"})
        # M3I: soft-delete tombstone. A deleted migration is hidden from ordinary listings and can
        # never be resurrected by the worker, but its jobs row + audit trail are retained.
        add_missing("jobs", {"deleted_at": "TEXT"})
        add_missing("mapping_decisions", {"method": "TEXT"})
        add_missing("mapping_decisions", _DECISION_MIGRATION_COLUMNS)
        add_missing("source_files", _SOURCE_FILE_MIGRATION_COLUMNS)
        add_missing("audit_events", {"work_item_id": "TEXT"})
        add_missing("prepared_candidates", _CANDIDATE_MIGRATION_COLUMNS)
        # M3C: enriched profile.v2 payload (stats + complete low-cardinality value domain).
        add_missing("column_profiles", {"profile_ext": "TEXT"})
        # M3B.2: operation-level execution fence columns (pre-M3B.2 delivery DBs lack them).
        add_missing("delivery_operations", {"work_item_id": "TEXT", "rollback_work_item_id": "TEXT"})
        # M3B.2 (item 6): DB-level attempt-number uniqueness per operation. Dedupe any accidental
        # duplicates (keep the first-inserted row) BEFORE creating the unique index so an existing DB
        # with a prior duplicate cannot block startup.
        try:
            self._conn.execute(
                "DELETE FROM delivery_attempts WHERE rowid NOT IN "
                "(SELECT MIN(rowid) FROM delivery_attempts GROUP BY operation_id, attempt_no)")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_delattempt_op "
                "ON delivery_attempts(operation_id, attempt_no)")
        except sqlite3.Error:  # pragma: no cover - defensive; leave the non-unique index in place
            pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- locked read helpers (single connection, multi-threaded sync routes) ---
    def _fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def _fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        with self._lock:
            cur = self._conn.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None

    # --- jobs -------------------------------------------------------------
    def create_job(self, *, schema_version: str, provider: str, model_id: str, adapter_kind: str,
                   tenant_id: str = "default") -> str:
        job_id = _nid("job")
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO tenants (id, name, created_at) VALUES (?,?,?)",
                (tenant_id, tenant_id, _now()))
            self._conn.execute(
                "INSERT INTO jobs (id, thread_id, schema_version, provider, model_id, adapter_kind, "
                "status, stage, tenant_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, job_id, schema_version, provider, model_id, adapter_kind,
                 "created", "created", tenant_id, _now(), _now()),
            )
            self._conn.commit()
        return job_id

    def job_tenant(self, job_id: str) -> str:
        job = self.get_job(job_id)
        return (job or {}).get("tenant_id") or "default"

    def set_job_stage(self, job_id: str, *, status: str, stage: str, error: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, stage=?, error=?, updated_at=? WHERE id=?",
                (status, stage, error, _now(), job_id),
            )
            self._conn.commit()

    def set_job_summary(self, job_id: str, summary: dict) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET summary=?, updated_at=? WHERE id=?", (_j(summary), _now(), job_id)
            )
            self._conn.commit()

    def get_job(self, job_id: str) -> dict | None:
        return self._fetchone("SELECT * FROM jobs WHERE id=?", (job_id,))

    def list_jobs(self, limit: int = 50) -> list[dict]:
        # Tombstoned (soft-deleted) migrations are excluded from the ordinary list. get_job() still
        # returns them by id (delete endpoint / worker tombstone guard need to read them).
        return self._fetchall(
            "SELECT * FROM jobs WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT ?", (limit,))

    def job_is_deleted(self, job_id: str) -> bool:
        """True if the migration was deleted — either the jobs row is gone (a complete delete) or a
        legacy soft-tombstone (deleted_at set). The worker uses this to never resurrect a removed job,
        so a missing row must read as deleted, not as 'not yet deleted'."""
        r = self._fetchone("SELECT deleted_at FROM jobs WHERE id=?", (job_id,))
        return r is None or bool(r.get("deleted_at"))

    # --- source files/tables/rows ----------------------------------------
    def add_source_file(self, job_id: str, *, file_id: str, original_filename: str,
                        stored_name: str, content_type: str | None, size_bytes: int,
                        blob_key: str | None = None, sha256: str | None = None,
                        storage_status: str = "stored", parse_status: str = "uploaded") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO source_files (id, job_id, original_filename, stored_name, content_type, "
                "size_bytes, blob_key, sha256, storage_status, parse_status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (file_id, job_id, original_filename, stored_name, content_type, size_bytes,
                 blob_key, sha256, storage_status, parse_status, _now()),
            )
            self._conn.commit()

    def set_source_file_status(self, file_id: str, *, parse_status: str,
                               parse_error: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE source_files SET parse_status=?, parse_error=? WHERE id=?",
                (parse_status, parse_error, file_id))
            self._conn.commit()

    def get_source_files(self, job_id: str) -> list[dict]:
        return self._fetchall("SELECT * FROM source_files WHERE job_id=? ORDER BY created_at",
                              (job_id,))

    def delete_source_data_for_file(self, job_id: str, file_id: str) -> None:
        """Remove staging rows/tables/parsing-issues for a file so re-ingestion is idempotent."""
        with self._lock:
            tbl_ids = [r["id"] for r in self._conn.execute(
                "SELECT id FROM source_tables WHERE job_id=? AND file_id=?", (job_id, file_id)).fetchall()]
            for tid in tbl_ids:
                self._conn.execute("DELETE FROM source_rows WHERE table_id=?", (tid,))
                self._conn.execute("DELETE FROM parsing_issues WHERE table_id=?", (tid,))
            self._conn.execute("DELETE FROM source_tables WHERE job_id=? AND file_id=?", (job_id, file_id))
            self._conn.commit()

    def get_source_file(self, file_id: str) -> dict | None:
        return self._fetchone("SELECT * FROM source_files WHERE id=?", (file_id,))

    # === M3I: safe source-set mutation (file removal) + migration delete =====================
    @staticmethod
    def _audit_conn(conn: sqlite3.Connection, job_id: str, *, event_type: str, actor: str,
                    after: Any = None, reason: str | None = None,
                    source_ref: dict | None = None) -> None:
        """Append an audit event ON AN EXISTING transaction/connection (the instance lock is already
        held; add_audit() would deadlock re-acquiring the non-reentrant lock)."""
        conn.execute(
            "INSERT INTO audit_events (id, job_id, issue_id, work_item_id, source_ref, event_type, "
            "actor, before, after, reason, schema_version, policy_version, model_version, ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_nid("aud"), job_id, None, None, _j(source_ref) if source_ref else None, event_type, actor,
             None, _j(after) if after is not None else None, reason, None, None, None, _now()))

    def _clear_derived_state_conn(self, conn: sqlite3.Connection, job_id: str) -> None:
        """Delete ALL job-scoped derived state (mapping/preparation/reconciliation/versions/metrics)
        for a job on an existing transaction. Delivery rows are cleared via their parent operation.
        Tenant/organization schema (custom_field_definitions, tenants) and the audit trail are kept."""
        op_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM delivery_operations WHERE job_id=?", (job_id,)).fetchall()]
        for oid in op_ids:
            conn.execute("DELETE FROM delivery_attempts WHERE operation_id=?", (oid,))
        conn.execute("DELETE FROM delivery_operations WHERE job_id=?", (job_id,))
        for table in _DERIVED_TABLES_BY_JOB:
            conn.execute(f"DELETE FROM {table} WHERE job_id=?", (job_id,))

    def _delete_source_tables_conn(self, conn: sqlite3.Connection, job_id: str,
                                   file_ids: list[str]) -> None:
        """Delete the raw staged tables/rows/parsing-issues for the given files (their source_files
        rows too), on an existing transaction."""
        if not file_ids:
            return
        ph = ",".join("?" * len(file_ids))
        tbl_ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM source_tables WHERE job_id=? AND file_id IN ({ph})",
            (job_id, *file_ids)).fetchall()]
        for tid in tbl_ids:
            conn.execute("DELETE FROM source_rows WHERE table_id=?", (tid,))
            conn.execute("DELETE FROM parsing_issues WHERE table_id=?", (tid,))
        conn.execute(f"DELETE FROM source_tables WHERE job_id=? AND file_id IN ({ph})",
                     (job_id, *file_ids))
        conn.execute(f"DELETE FROM source_files WHERE job_id=? AND id IN ({ph})", (job_id, *file_ids))

    def has_active_work(self, job_id: str) -> bool:
        """True while a worker currently OWNS an in-flight work item for the job (status='processing').
        A destructive source-set mutation must not race the worker."""
        r = self._fetchone(
            "SELECT COUNT(*) AS n FROM work_items WHERE job_id=? AND status='processing'", (job_id,))
        return bool(r and r["n"])

    def has_target_delivery_activity(self, job_id: str) -> bool:
        """True once target sync has started or left ANY delivery-attempt / non-PLANNED evidence. Used
        to BLOCK source-file removal: after a target write attempt the source set can no longer be
        rebuilt without hiding an external side effect."""
        job = self.get_job(job_id)
        if job and job.get("status") in ("delivering", "rollback_in_progress", "migration_complete",
                                         "delivery_partial_failure", "rollback_complete",
                                         "rollback_partial_failure", "stale_target_review_required"):
            return True
        r = self._fetchone(
            "SELECT COUNT(*) AS n FROM delivery_operations WHERE job_id=? AND status<>'PLANNED'", (job_id,))
        if r and r["n"]:
            return True
        r = self._fetchone(
            "SELECT COUNT(*) AS n FROM delivery_attempts a JOIN delivery_operations o "
            "ON a.operation_id=o.id WHERE o.job_id=?", (job_id,))
        return bool(r and r["n"])

    def has_unreverted_target_writes(self, job_id: str) -> bool:
        """True while the target still holds (or may hold) migration changes that were NOT rolled back.
        Used to BLOCK migration delete. A write that succeeded then rolled back (ROLLED_BACK) is safe;
        a live SUCCEEDED / in-flight PROCESSING / failed-rollback (ROLLBACK_FAILED) op is not."""
        r = self._fetchone(
            "SELECT COUNT(*) AS n FROM delivery_operations WHERE job_id=? "
            "AND status IN ('SUCCEEDED','PROCESSING','ROLLBACK_FAILED')", (job_id,))
        return bool(r and r["n"])

    def count_prepared_candidates(self, job_id: str) -> int:
        r = self._fetchone("SELECT COUNT(*) AS n FROM prepared_candidates WHERE job_id=?", (job_id,))
        return r["n"] if r else 0

    def remove_source_files(self, job_id: str, file_ids: list[str], *, actor: str = "human",
                            note: str | None = None, map_max_attempts: int = 5) -> dict:
        """Atomically remove the selected source files AND rebuild the migration from the remaining
        files. One transaction: drop the removed files' blobs-metadata/tables/rows/parsing-issues,
        clear ALL job-scoped derived state, cancel non-terminal queued work, audit the removal
        (preserving tenant custom-field definitions), enqueue ONE fresh generation-keyed MAP work item,
        and reset the job stage to rebuilding. Caller deletes the returned blob keys after commit.

        Returns {"status": "ok"|"active_work", ...}. Callers MUST have validated file ownership,
        non-empty/dedup, at-least-one-remaining, and no target delivery activity BEFORE calling.
        """
        file_ids = list(dict.fromkeys(file_ids))
        with self._lock:
            try:
                # Race guard (inside the same lock the worker claims under): never mutate while a
                # worker owns an in-flight item for this job.
                if self._conn.execute(
                        "SELECT COUNT(*) AS n FROM work_items WHERE job_id=? AND status='processing'",
                        (job_id,)).fetchone()["n"]:
                    return {"status": "active_work"}
                ph = ",".join("?" * len(file_ids))
                removed = [dict(r) for r in self._conn.execute(
                    f"SELECT id, blob_key, original_filename FROM source_files "
                    f"WHERE job_id=? AND id IN ({ph})", (job_id, *file_ids)).fetchall()]
                self._delete_source_tables_conn(self._conn, job_id, file_ids)
                self._clear_derived_state_conn(self._conn, job_id)
                self._conn.execute(
                    "UPDATE work_items SET status='cancelled', last_error_category='source_set_rebuild', "
                    "lease_expires_at=NULL, updated_at=? WHERE job_id=? AND status IN ('pending','retryable')",
                    (_now(), job_id))
                remaining = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM source_files WHERE job_id=?", (job_id,)).fetchone()["n"]
                preserved = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM custom_field_definitions WHERE origin_job_id=?",
                    (job_id,)).fetchone()["n"]
                self._audit_conn(
                    self._conn, job_id, event_type="source_files_removed", actor=actor,
                    after={"removed_files": [{"id": r["id"], "filename": r["original_filename"]}
                                             for r in removed],
                           "removed_count": len(removed), "remaining_files": remaining,
                           "preserved_custom_field_definitions": preserved},
                    reason=note)
                map_work_id = _nid("wk")
                map_key = f"{job_id}:map:rebuild:{uuid.uuid4().hex[:8]}"
                self._conn.execute(
                    "INSERT INTO work_items (id, job_id, kind, payload, status, attempt, max_attempts, "
                    "available_at, idempotency_key, created_at, updated_at) "
                    "VALUES (?,?,?,?, 'pending', 0, ?, ?, ?, ?, ?)",
                    (map_work_id, job_id, "MAP", "{}", int(map_max_attempts), _now(), map_key,
                     _now(), _now()))
                self._conn.execute(
                    "UPDATE jobs SET status='mapping_queued', stage='rebuilding', error=NULL, "
                    "updated_at=? WHERE id=?", (_now(), job_id))
                self._conn.commit()
                return {"status": "ok", "removed": removed, "remaining": remaining,
                        "blob_keys": [r["blob_key"] for r in removed if r.get("blob_key")],
                        "map_work_id": map_work_id, "preserved_custom_field_definitions": preserved}
            except Exception:
                self._conn.rollback()
                raise

    def delete_job(self, job_id: str, *, actor: str = "human", note: str | None = None) -> dict:
        """COMPLETELY and permanently delete a migration in one atomic transaction: purge every
        job-scoped row — source files/tables/rows, all mapping/preparation/reconciliation/version/
        metrics state, delivery operations + attempts, work items, AND the audit trail — then drop the
        jobs row itself. Nothing about this migration remains. The organization (tenant + its
        custom_field_definitions) is ALSO deleted when this was its LAST migration, so a fully removed
        company leaves no orphaned org config; an org still used by another migration, and the internal
        "default" org, are kept. Returns {"status": "ok"|"active_work", "blob_keys": [...],
        "org_deleted": <tenant_id|None>}: the caller deletes the returned blobs after commit and MUST
        have validated the no-unreverted-target-writes rule first."""
        with self._lock:
            try:
                if self._conn.execute(
                        "SELECT COUNT(*) AS n FROM work_items WHERE job_id=? AND status='processing'",
                        (job_id,)).fetchone()["n"]:
                    return {"status": "active_work"}
                jrow = self._conn.execute("SELECT tenant_id FROM jobs WHERE id=?", (job_id,)).fetchone()
                tenant_id = jrow["tenant_id"] if jrow else None
                blob_keys = [r["blob_key"] for r in self._conn.execute(
                    "SELECT blob_key FROM source_files WHERE job_id=?", (job_id,)).fetchall()
                    if r["blob_key"]]
                # delivery_attempts has no job_id; remove it via its parent operations first.
                self._conn.execute(
                    "DELETE FROM delivery_attempts WHERE operation_id IN "
                    "(SELECT id FROM delivery_operations WHERE job_id=?)", (job_id,))
                for table in _ALL_JOB_TABLES:
                    self._conn.execute(f"DELETE FROM {table} WHERE job_id=?", (job_id,))
                self._conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
                # Cascade the organization when this migration was its last one (never the internal
                # "default", never an org still referenced by another migration).
                org_deleted = None
                if tenant_id and tenant_id != self.default_tenant_id_value():
                    remaining = self._conn.execute(
                        "SELECT COUNT(*) AS n FROM jobs WHERE tenant_id=?", (tenant_id,)).fetchone()["n"]
                    if remaining == 0:
                        self._conn.execute("DELETE FROM custom_field_definitions WHERE tenant_id=?", (tenant_id,))
                        self._conn.execute("DELETE FROM tenants WHERE id=?", (tenant_id,))
                        org_deleted = tenant_id
                self._conn.commit()
                return {"status": "ok", "blob_keys": blob_keys, "org_deleted": org_deleted}
            except Exception:
                self._conn.rollback()
                raise

    def default_tenant_id_value(self) -> str:
        """The internal default tenant id — never auto-deleted by the org cascade."""
        return "default"

    def add_source_table(self, job_id: str, table) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO source_tables (id, job_id, file_id, original_filename, "
                "sheet_name, headers, n_rows) VALUES (?,?,?,?,?,?,?)",
                (table.table_id, job_id, table.file_id, table.original_filename,
                 table.sheet_name, _j(table.headers), table.n_rows),
            )
            self._conn.commit()

    def add_source_rows(self, job_id: str, records) -> None:
        with self._lock:
            for rec in records:
                self._conn.execute(
                    "INSERT INTO source_rows (id, job_id, table_id, row_number, cells) VALUES (?,?,?,?,?)",
                    (_nid("row"), job_id, rec.ref.table_id, rec.ref.row_number or 0,
                     _j([c.model_dump() for c in rec.cells])),
                )
            self._conn.commit()

    def add_parsing_issues(self, job_id: str, issues) -> None:
        with self._lock:
            for iss in issues:
                self._conn.execute(
                    "INSERT INTO parsing_issues (id, job_id, table_id, kind, detail, severity, ref) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (_nid("pi"), job_id, iss.ref.table_id, iss.kind, iss.detail, iss.severity,
                     _j(iss.ref.model_dump())),
                )
            self._conn.commit()

    def get_tables(self, job_id: str) -> list[dict]:
        return self._fetchall("SELECT * FROM source_tables WHERE job_id=?", (job_id,))

    def get_rows_for_table(self, table_id: str) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM source_rows WHERE table_id=? ORDER BY row_number", (table_id,)
        )

    def count_source_rows(self, table_id: str) -> int:
        r = self._fetchone("SELECT COUNT(*) AS n FROM source_rows WHERE table_id=?", (table_id,))
        return r["n"] if r else 0

    def get_source_rows_page(self, table_id: str, *, offset: int, limit: int) -> list[dict]:
        """Bounded page of raw staged rows for a table (M3A.1 staged-data inspector)."""
        return self._fetchall(
            "SELECT * FROM source_rows WHERE table_id=? ORDER BY row_number LIMIT ? OFFSET ?",
            (table_id, int(limit), int(offset)))

    def get_source_row_context(self, table_id: str, row_number: int, *, context: int = 2) -> list[dict]:
        """The exact staged row plus up to ``context`` rows on each side (source deep-link)."""
        lo, hi = int(row_number) - max(0, int(context)), int(row_number) + max(0, int(context))
        return self._fetchall(
            "SELECT * FROM source_rows WHERE table_id=? AND row_number BETWEEN ? AND ? ORDER BY row_number",
            (table_id, lo, hi))

    def get_parsing_issues(self, job_id: str) -> list[dict]:
        return self._fetchall("SELECT * FROM parsing_issues WHERE job_id=?", (job_id,))

    # --- profiles ---------------------------------------------------------
    def add_profiles(self, job_id: str, profiles) -> None:
        with self._lock:
            for p in profiles:
                ext = _j({"profile_version": getattr(p, "profile_version", "profile.v2"),
                          "stats": getattr(p, "stats", {}) or {},
                          "value_domain": getattr(p, "value_domain", []) or []})
                self._conn.execute(
                    "INSERT OR IGNORE INTO column_profiles (id, job_id, table_id, col_index, header, "
                    "non_empty_count, missing_count, distinct_count, observed_types, format_indicators, "
                    "samples, profile_ext) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (p.profile_id, job_id, p.table_id, p.col_index, p.header, p.non_empty_count,
                     p.missing_count, p.distinct_count, _j(p.observed_types), _j(p.format_indicators),
                     _j(p.samples), ext),
                )
            self._conn.commit()

    def get_profiles(self, job_id: str) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM column_profiles WHERE job_id=? ORDER BY table_id, col_index", (job_id,)
        )

    def get_profile(self, profile_id: str) -> dict | None:
        return self._fetchone("SELECT * FROM column_profiles WHERE id=?", (profile_id,))

    # --- M3C transformation plans -----------------------------------------
    def upsert_transformation_plan(self, plan) -> bool:
        """Insert/refresh a plan. A human-settled plan (approved/rejected) is NEVER clobbered by a
        deterministic/model re-run: its status, operations and resolution are preserved."""
        r = plan.to_row()
        with self._lock:
            existing = self._conn.execute(
                "SELECT status FROM transformation_plans WHERE id=?", (r["id"],)).fetchone()
            if existing and existing["status"] in ("approved", "rejected") and plan.origin != "human":
                return False
            now = _now()
            self._conn.execute(
                "INSERT INTO transformation_plans (id, job_id, table_id, profile_id, source_header, "
                "target_field, kind, operations, origin, status, evidence, affected_rows, review_prompt, "
                "review_options, resolution, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET table_id=excluded.table_id, source_header=excluded.source_header, "
                "target_field=excluded.target_field, kind=excluded.kind, operations=excluded.operations, "
                "origin=excluded.origin, status=excluded.status, evidence=excluded.evidence, "
                "affected_rows=excluded.affected_rows, review_prompt=excluded.review_prompt, "
                "review_options=excluded.review_options, resolution=excluded.resolution, updated_at=excluded.updated_at",
                (r["id"], r["job_id"], r["table_id"], r["profile_id"], r["source_header"],
                 r["target_field"], r["kind"], _j(r["operations"]), r["origin"], r["status"],
                 _j(r["evidence"]), r["affected_rows"], r["review_prompt"], _j(r["review_options"]),
                 _j(r["resolution"]) if r["resolution"] is not None else None, now, now))
            self._conn.commit()
            return existing is None

    def get_transformation_plans(self, job_id: str, *, status: str | None = None) -> list[dict]:
        if status:
            return self._fetchall(
                "SELECT * FROM transformation_plans WHERE job_id=? AND status=? ORDER BY table_id, source_header",
                (job_id, status))
        return self._fetchall(
            "SELECT * FROM transformation_plans WHERE job_id=? ORDER BY table_id, source_header", (job_id,))

    def get_transformation_plan(self, plan_id: str) -> dict | None:
        return self._fetchone("SELECT * FROM transformation_plans WHERE id=?", (plan_id,))

    def resolve_transformation_plan(self, plan_id: str, *, status: str, resolution: dict | None,
                                    operations: list | None = None) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT operations FROM transformation_plans WHERE id=?",
                                     (plan_id,)).fetchone()
            if row is None:
                return False
            ops = operations if operations is not None else json.loads(row["operations"])
            self._conn.execute(
                "UPDATE transformation_plans SET status=?, resolution=?, operations=?, origin='human', "
                "updated_at=? WHERE id=?",
                (status, _j(resolution) if resolution is not None else None, _j(ops), _now(), plan_id))
            self._conn.commit()
            return True

    def delete_transformation_plans_not_in(self, job_id: str, keep_ids: set[str]) -> None:
        """Remove auto/deterministic plans no longer produced by a re-run (keep human-settled ones)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, status FROM transformation_plans WHERE job_id=?", (job_id,)).fetchall()
            for row in rows:
                if row["id"] not in keep_ids and row["status"] not in ("approved", "rejected"):
                    self._conn.execute("DELETE FROM transformation_plans WHERE id=?", (row["id"],))
            self._conn.commit()

    def add_column_relationship(self, job_id: str, *, table_id: str, relationship: str,
                                code_profile_id: str | None, label_profile_id: str | None,
                                evidence: dict) -> None:
        rid = f"rel_{hashlib.sha1(f'{job_id}|{table_id}|{code_profile_id}|{label_profile_id}'.encode()).hexdigest()[:12]}"
        with self._lock:
            self._conn.execute(
                "INSERT INTO column_relationships (id, job_id, table_id, relationship, code_profile_id, "
                "label_profile_id, evidence, created_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET relationship=excluded.relationship, evidence=excluded.evidence",
                (rid, job_id, table_id, relationship, code_profile_id, label_profile_id, _j(evidence), _now()))
            self._conn.commit()

    def get_column_relationships(self, job_id: str) -> list[dict]:
        return self._fetchall("SELECT * FROM column_relationships WHERE job_id=?", (job_id,))

    # --- proposals --------------------------------------------------------
    def add_proposal(self, job_id: str, *, proposal, profile_id: str, table_id: str,
                     adapter_kind: str, model_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO mapping_proposals (id, job_id, table_id, profile_id, source_header, "
                "proposed_target_field, alternatives, is_ambiguous, ambiguity_reason, evidence, confidence, "
                "adapter_kind, model_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"prop_{profile_id}", job_id, table_id, profile_id, proposal.source_header,
                 proposal.proposed_target_field, _j(proposal.alternative_target_fields),
                 1 if proposal.is_ambiguous else 0, proposal.ambiguity_reason, _j(proposal.evidence),
                 proposal.confidence, adapter_kind, model_id, _now()),
            )
            self._conn.commit()

    def get_proposals(self, job_id: str) -> list[dict]:
        return self._fetchall("SELECT * FROM mapping_proposals WHERE job_id=?", (job_id,))

    # --- decisions --------------------------------------------------------
    def upsert_decision(self, job_id: str, *, profile_id: str, table_id: str, source_header: str,
                        target_field: str | None, status: str, actor: str, reason: str | None,
                        method: str | None = None, destination_kind: str | None = None,
                        path_meta: dict | None = None, custom_definition_id: str | None = None,
                        note: str | None = None, conn: sqlite3.Connection | None = None) -> None:
        sql = ("INSERT INTO mapping_decisions (id, job_id, profile_id, table_id, source_header, "
               "target_field, status, actor, method, reason, destination_kind, path_meta, "
               "custom_definition_id, note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
               "ON CONFLICT(job_id, profile_id) DO UPDATE SET target_field=excluded.target_field, "
               "status=excluded.status, actor=excluded.actor, method=excluded.method, "
               "reason=excluded.reason, destination_kind=excluded.destination_kind, "
               "path_meta=excluded.path_meta, custom_definition_id=excluded.custom_definition_id, "
               "note=excluded.note")
        args = (_nid("dec"), job_id, profile_id, table_id, source_header, target_field, status,
                actor, method, reason, destination_kind, _j(path_meta) if path_meta else None,
                custom_definition_id, note, _now())
        if conn is not None:
            conn.execute(sql, args)
            return
        with self._lock:
            self._conn.execute(sql, args)
            self._conn.commit()

    def get_decisions(self, job_id: str) -> list[dict]:
        return self._fetchall("SELECT * FROM mapping_decisions WHERE job_id=?", (job_id,))

    # --- review issues ----------------------------------------------------
    def upsert_issue(self, job_id: str, *, issue_id: str, profile_id: str, table_id: str,
                     source_header: str, issue_type: str, proposed_target_field: str | None,
                     candidate_target_fields: list[str], evidence_summary: dict,
                     affected_non_empty_rows: int) -> None:
        """Idempotent: creating the same issue twice (same deterministic id) is a no-op."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO review_issues (id, job_id, profile_id, table_id, source_header, "
                "issue_type, proposed_target_field, candidate_target_fields, evidence_summary, "
                "affected_non_empty_rows, status, version, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (issue_id, job_id, profile_id, table_id, source_header, issue_type,
                 proposed_target_field, _j(candidate_target_fields), _j(evidence_summary),
                 affected_non_empty_rows, "open", 1, _now(), _now()),
            )
            self._conn.commit()

    def get_issue(self, issue_id: str) -> dict | None:
        return self._fetchone("SELECT * FROM review_issues WHERE id=?", (issue_id,))

    def get_issues(self, job_id: str, status: str | None = None) -> list[dict]:
        if status:
            return self._fetchall(
                "SELECT * FROM review_issues WHERE job_id=? AND status=? ORDER BY created_at",
                (job_id, status),
            )
        return self._fetchall(
            "SELECT * FROM review_issues WHERE job_id=? ORDER BY created_at", (job_id,)
        )

    def resolve_issue_if_current(self, issue_id: str, *, expected_version: int, resolution: dict) -> str:
        """Atomically resolve an OPEN issue at ``expected_version``.

        Returns: 'resolved' on success, 'stale' on version/status mismatch,
        'noop' if already resolved with an identical resolution (idempotent).
        """
        with self._lock:
            cur = self._conn.execute("SELECT * FROM review_issues WHERE id=?", (issue_id,))
            row = cur.fetchone()
            if row is None:
                return "not_found"
            issue = dict(row)
            if issue["status"] == "resolved":
                existing = json.loads(issue["resolution"]) if issue["resolution"] else None
                if existing == resolution:
                    return "noop"
                return "stale"
            if issue["version"] != expected_version:
                return "stale"
            cur2 = self._conn.execute(
                "UPDATE review_issues SET status='resolved', resolution=?, version=version+1, updated_at=? "
                "WHERE id=? AND version=? AND status='open'",
                (_j(resolution), _now(), issue_id, expected_version),
            )
            changed = cur2.rowcount
            self._conn.commit()
            return "resolved" if changed else "stale"

    # --- audit ------------------------------------------------------------
    def add_audit(self, job_id: str, *, event_type: str, actor: str, issue_id: str | None = None,
                  work_item_id: str | None = None, source_ref: dict | None = None,
                  before: Any = None, after: Any = None,
                  reason: str | None = None, schema_version: str | None = None,
                  policy_version: str | None = None, model_version: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_events (id, job_id, issue_id, work_item_id, source_ref, event_type, "
                "actor, before, after, reason, schema_version, policy_version, model_version, ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_nid("aud"), job_id, issue_id, work_item_id, _j(source_ref) if source_ref else None,
                 event_type, actor, _j(before) if before is not None else None,
                 _j(after) if after is not None else None,
                 reason, schema_version, policy_version, model_version, _now()),
            )
            self._conn.commit()

    def get_audit(self, job_id: str) -> list[dict]:
        return self._fetchall("SELECT * FROM audit_events WHERE job_id=? ORDER BY ts", (job_id,))

    # --- M3D: model-call observability metrics (sanitized) ----------------
    def add_model_call(self, job_id: str, *, kind: str, table_id: str | None, adapter_kind: str,
                       model_id: str, attempts: int, latency_ms: float,
                       prompt_tokens: int | None, completion_tokens: int | None,
                       total_tokens: int | None, status: str, error_category: str | None,
                       n_columns: int, n_proposals: int | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO model_calls (id, job_id, kind, table_id, adapter_kind, model_id, attempts, "
                "latency_ms, prompt_tokens, completion_tokens, total_tokens, status, error_category, "
                "n_columns, n_proposals, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_nid("mc"), job_id, kind, table_id, adapter_kind, model_id, int(attempts),
                 float(latency_ms), prompt_tokens, completion_tokens, total_tokens, status,
                 error_category, int(n_columns), n_proposals, _now()))
            self._conn.commit()

    def get_model_calls(self, job_id: str) -> list[dict]:
        return self._fetchall("SELECT * FROM model_calls WHERE job_id=? ORDER BY created_at", (job_id,))

    # --- M3F §I: per-stage compute timing (best-effort; never gates the pipeline) --------
    def add_stage_timing(self, job_id: str, *, stage: str, duration_ms: float,
                         detail: dict | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO stage_timings (id, job_id, stage, duration_ms, detail, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (_nid("st"), job_id, stage, float(duration_ms),
                 json.dumps(detail) if detail else None, _now()))
            self._conn.commit()

    def get_stage_timings(self, job_id: str) -> list[dict]:
        return self._fetchall("SELECT * FROM stage_timings WHERE job_id=? ORDER BY created_at", (job_id,))

    # --- M2: threads + prep summary --------------------------------------
    def set_job_threads(self, job_id: str, *, map_thread: str | None = None,
                        prep_thread: str | None = None, recon_thread: str | None = None) -> None:
        with self._lock:
            for col, val in (("map_thread", map_thread), ("prep_thread", prep_thread),
                             ("recon_thread", recon_thread)):
                if val is not None:
                    self._conn.execute(f"UPDATE jobs SET {col}=?, updated_at=? WHERE id=?",
                                       (val, _now(), job_id))
            self._conn.commit()

    def set_prep_summary(self, job_id: str, summary: dict) -> None:
        with self._lock:
            self._conn.execute("UPDATE jobs SET prep_summary=?, updated_at=? WHERE id=?",
                               (_j(summary), _now(), job_id))
            self._conn.commit()

    def set_recon_summary(self, job_id: str, summary: dict) -> None:
        with self._lock:
            self._conn.execute("UPDATE jobs SET recon_summary=?, updated_at=? WHERE id=?",
                               (_j(summary), _now(), job_id))
            self._conn.commit()

    # --- M2: prepared candidates (derived; replaced wholesale each run) ----
    def replace_candidates(self, job_id: str, candidates: list[dict]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM prepared_candidates WHERE job_id=?", (job_id,))
            for c in candidates:
                self._conn.execute(
                    "INSERT INTO prepared_candidates (id, job_id, business_key, eligibility, record, "
                    "source_refs, issue_ids, exclude_reason, collections, custom_attributes, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (c["id"], job_id, c.get("business_key"), c["eligibility"], _j(c["record"]),
                     _j(c.get("source_refs", [])), _j(c.get("issue_ids", [])), c.get("exclude_reason"),
                     _j(c.get("collections") or {}), _j(c.get("custom_attributes") or []),
                     _now(), _now()),
                )
            self._conn.commit()

    def get_candidates(self, job_id: str) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM prepared_candidates WHERE job_id=? ORDER BY business_key IS NULL, business_key",
            (job_id,))

    def get_candidate(self, job_id: str, candidate_id: str) -> dict | None:
        return self._fetchone(
            "SELECT * FROM prepared_candidates WHERE job_id=? AND id=?", (job_id, candidate_id))

    def update_candidate_scalar_fields(self, job_id: str, candidate_id: str, *,
                                       core: dict[str, Any] | None = None,
                                       custom: dict[str, Any] | None = None,
                                       note: str | None = None) -> bool:
        """Apply a migration-admin edit to a candidate's SCALAR fields (core record fields and existing
        scalar organization/custom attributes). Values must already be normalized by the caller. Each
        edited field is marked resolved with rule 'admin.edit' so it flows into the effective snapshot
        exactly like any other resolved value. Returns True if the row was found and written."""
        core = core or {}
        custom = custom or {}
        with self._lock:
            row = self._conn.execute(
                "SELECT record, custom_attributes FROM prepared_candidates WHERE job_id=? AND id=?",
                (job_id, candidate_id)).fetchone()
            if row is None:
                return False
            rec = json.loads(row["record"]) if row["record"] else {}
            for f, v in core.items():
                rec[f] = {"value": v, "status": "resolved", "rule": "admin.edit",
                          "reason": note or "Migration-admin edit", "provenance": []}
            ca = json.loads(row["custom_attributes"]) if row["custom_attributes"] else []
            by_key = {c.get("key"): c for c in ca if isinstance(c, dict)}
            for k, v in custom.items():
                if k in by_key:
                    by_key[k]["value"] = v
                    by_key[k]["status"] = "resolved"
                    by_key[k]["rule"] = "admin.edit"
            self._conn.execute(
                "UPDATE prepared_candidates SET record=?, custom_attributes=?, updated_at=? "
                "WHERE job_id=? AND id=?",
                (_j(rec), _j(ca), _now(), job_id, candidate_id))
            self._conn.commit()
            return True

    def latest_synced_target(self, job_id: str, candidate_id: str) -> dict | None:
        """The live target coordinates for an already-synced employee: the most recent delivery
        operation for this candidate that reached the target (SUCCEEDED write), with the target
        employee id and the revision it left behind. None when the employee was never synced — used to
        decide whether an admin edit must push a target UPDATE and whether a delete is possible."""
        row = self._fetchone(
            "SELECT * FROM delivery_operations WHERE job_id=? AND candidate_id=? AND status='SUCCEEDED' "
            "AND op_type IN ('CREATE','UPDATE') ORDER BY updated_at DESC, created_at DESC LIMIT 1",
            (job_id, candidate_id))
        if not row:
            return None
        return {"employee_id": row.get("employee_id") or row.get("target_record_id"),
                "revision": row.get("target_revision_after"), "op": row}

    # --- M2: record issues (deterministic ids -> idempotent) --------------
    def upsert_record_issue(self, job_id: str, *, issue_id: str, candidate_key: str | None,
                            field: str | None, issue_type: str, reason: str, options: list,
                            affected: dict, scope: dict | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO record_issues (id, job_id, candidate_key, field, issue_type, reason, "
                "options, affected, scope, status, version, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET reason=excluded.reason, options=excluded.options, "
                "affected=excluded.affected, scope=excluded.scope, "
                "status=CASE WHEN record_issues.status='superseded' THEN 'open' ELSE record_issues.status END, "
                "updated_at=excluded.updated_at",
                (issue_id, job_id, candidate_key, field, issue_type, reason, _j(options), _j(affected),
                 _j(scope) if scope else None, "open", 1, _now(), _now()),
            )
            self._conn.commit()

    def get_record_issue(self, issue_id: str) -> dict | None:
        return self._fetchone("SELECT * FROM record_issues WHERE id=?", (issue_id,))

    def get_record_issues(self, job_id: str, status: str | None = None) -> list[dict]:
        if status:
            return self._fetchall(
                "SELECT * FROM record_issues WHERE job_id=? AND status=? ORDER BY created_at",
                (job_id, status))
        return self._fetchall(
            "SELECT * FROM record_issues WHERE job_id=? ORDER BY created_at", (job_id,))

    def resolve_record_issue_if_current(self, issue_id: str, *, expected_version: int,
                                        resolution: dict) -> str:
        with self._lock:
            row = self._conn.execute("SELECT * FROM record_issues WHERE id=?", (issue_id,)).fetchone()
            if row is None:
                return "not_found"
            issue = dict(row)
            if issue["status"] == "resolved":
                existing = json.loads(issue["resolution"]) if issue["resolution"] else None
                return "noop" if existing == resolution else "stale"
            if issue["status"] == "superseded":
                return "stale"
            if issue["version"] != expected_version:
                return "stale"
            cur = self._conn.execute(
                "UPDATE record_issues SET status='resolved', resolution=?, version=version+1, "
                "updated_at=? WHERE id=? AND version=? AND status='open'",
                (_j(resolution), _now(), issue_id, expected_version),
            )
            changed = cur.rowcount
            self._conn.commit()
            return "resolved" if changed else "stale"

    def supersede_open_issues_not_in(self, job_id: str, keep_ids: set[str]) -> None:
        """Mark open record issues that were not recomputed this pass as superseded."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM record_issues WHERE job_id=? AND status='open'", (job_id,)).fetchall()
            for r in rows:
                if r["id"] not in keep_ids:
                    self._conn.execute(
                        "UPDATE record_issues SET status='superseded', updated_at=? WHERE id=?",
                        (_now(), r["id"]))
            self._conn.commit()

    def get_resolved_record_decisions(self, job_id: str) -> dict[str, dict]:
        """issue_id -> resolution for resolved record issues (overlays for recompute)."""
        out: dict[str, dict] = {}
        for r in self._fetchall(
                "SELECT id, resolution FROM record_issues WHERE job_id=? AND status='resolved'", (job_id,)):
            if r["resolution"]:
                out[r["id"]] = json.loads(r["resolution"])
        return out

    # ===================== M3A: durable work queue =====================
    def enqueue_work(self, *, job_id: str, kind: str, idempotency_key: str,
                     source_file_id: str | None = None, payload: dict | None = None,
                     max_attempts: int = 5, available_at: str | None = None,
                     conn: sqlite3.Connection | None = None) -> bool:
        """Insert a work item. Idempotent via idempotency_key (returns False if it already exists).
        Pass ``conn`` to enlist in an existing transaction (atomic decision+enqueue)."""
        sql = ("INSERT OR IGNORE INTO work_items (id, job_id, source_file_id, kind, payload, status, "
               "attempt, max_attempts, available_at, idempotency_key, created_at, updated_at) "
               "VALUES (?,?,?,?,?, 'pending', 0, ?, ?, ?, ?, ?)")
        args = (_nid("wk"), job_id, source_file_id, kind, _j(payload or {}), max_attempts,
                available_at or _now(), idempotency_key, _now(), _now())
        if conn is not None:
            return conn.execute(sql, args).rowcount > 0
        with self._lock:
            rc = self._conn.execute(sql, args).rowcount
            self._conn.commit()
            return rc > 0

    def claim_next_work(self, worker_id: str, *, lease_seconds: int) -> dict | None:
        """Atomically claim one due work item. Correctness comes from the persisted status +
        the atomic conditional UPDATE (not the in-process lock, which only serializes SQLite)."""
        now = _now()
        expires = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM work_items WHERE status IN ('pending','retryable') AND available_at<=? "
                "ORDER BY available_at, created_at LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            cur = self._conn.execute(
                "UPDATE work_items SET status='processing', worker_id=?, started_at=?, "
                "lease_expires_at=?, attempt=attempt+1, updated_at=? "
                "WHERE id=? AND status IN ('pending','retryable')",
                (worker_id, now, expires, now, row["id"]))
            self._conn.commit()
            if cur.rowcount == 0:
                return None  # lost the race to another claimer
            got = self._conn.execute("SELECT * FROM work_items WHERE id=?", (row["id"],)).fetchone()
            return dict(got)

    def heartbeat_work(self, work_id: str, worker_id: str, *, lease_seconds: int) -> None:
        expires = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE work_items SET lease_expires_at=?, updated_at=? "
                "WHERE id=? AND worker_id=? AND status='processing'", (expires, _now(), work_id, worker_id))
            self._conn.commit()

    # --- lease ownership (M3B preflight): every completion / failure must PROVE the claim ---
    def _owner_clause(self, worker_id: str | None) -> tuple[str, tuple]:
        """SQL fragment restricting a mutation to the CURRENT lease holder. Without a worker id the
        mutation is only allowed while the item is still processing (legacy/admin paths)."""
        if worker_id is None:
            return " AND status='processing'", ()
        return " AND worker_id=? AND status='processing'", (worker_id,)

    def work_owned_by(self, work_id: str, worker_id: str) -> bool:
        """True iff ``worker_id`` still holds the live lease on ``work_id`` (call before any external
        side effect)."""
        r = self._fetchone("SELECT 1 AS ok FROM work_items WHERE id=? AND worker_id=? AND status='processing' "
                           "AND (lease_expires_at IS NULL OR lease_expires_at>?)", (work_id, worker_id, _now()))
        return r is not None

    def complete_work(self, work_id: str, worker_id: str | None = None) -> bool:
        """Mark succeeded ONLY if this worker still owns the claim. A stale worker (lease expired and
        the item re-claimed) cannot complete work it no longer owns. Returns True if applied."""
        clause, extra = self._owner_clause(worker_id)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE work_items SET status='succeeded', completed_at=?, lease_expires_at=NULL, "
                f"updated_at=? WHERE id=?{clause}", (_now(), _now(), work_id, *extra))
            self._conn.commit()
            return cur.rowcount > 0

    def fail_work(self, work_id: str, worker_id: str | None = None, *, category: str, error: str,
                  backoff_seconds: float = 2.0) -> str:
        """Mark retryable (if attempts remain) or failed — only for the current lease holder.
        Returns the new status, or 'not_owned' / 'not_found'."""
        avail = (datetime.now(timezone.utc) + timedelta(seconds=max(0.0, backoff_seconds))).isoformat()
        clause, extra = self._owner_clause(worker_id)
        with self._lock:
            row = self._conn.execute("SELECT attempt, max_attempts FROM work_items WHERE id=?",
                                     (work_id,)).fetchone()
            if row is None:
                return "not_found"
            status = "retryable" if row["attempt"] < row["max_attempts"] else "failed"
            cur = self._conn.execute(
                "UPDATE work_items SET status=?, last_error_category=?, last_error=?, available_at=?, "
                f"lease_expires_at=NULL, updated_at=? WHERE id=?{clause}",
                (status, category, (error or "")[:2000], avail, _now(), work_id, *extra))
            self._conn.commit()
            return status if cur.rowcount else "not_owned"

    def set_work_failed(self, work_id: str, worker_id: str | None = None, *, category: str, error: str) -> bool:
        """Force a work item terminal (no retry) — used for non-retryable stage errors. Ownership-checked."""
        clause, extra = self._owner_clause(worker_id)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE work_items SET status='failed', last_error_category=?, last_error=?, "
                f"lease_expires_at=NULL, updated_at=? WHERE id=?{clause}",
                (category, (error or "")[:2000], _now(), work_id, *extra))
            self._conn.commit()
            return cur.rowcount > 0

    def cancel_work(self, work_id: str, worker_id: str | None = None, *, reason: str = "cancelled") -> bool:
        """Mark an owned work item terminal as CANCELLED (never retried). Used by the tombstone guard so
        a deleted migration's already-claimed item is dropped instead of executed. Ownership-checked."""
        clause, extra = self._owner_clause(worker_id)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE work_items SET status='cancelled', last_error_category=?, "
                f"lease_expires_at=NULL, updated_at=? WHERE id=?{clause}",
                (reason, _now(), work_id, *extra))
            self._conn.commit()
            return cur.rowcount > 0

    def defer_work(self, work_id: str, worker_id: str, *, delay_seconds: float, reason: str) -> bool:
        """Put an owned item back to 'pending' for later WITHOUT consuming an attempt (e.g. a stage
        that must wait for in-flight delivery work). Ownership-checked."""
        avail = (datetime.now(timezone.utc) + timedelta(seconds=max(0.0, delay_seconds))).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE work_items SET status='pending', attempt=CASE WHEN attempt>0 THEN attempt-1 ELSE 0 END, "
                "available_at=?, lease_expires_at=NULL, worker_id=NULL, last_error_category='deferred', "
                "last_error=?, updated_at=? WHERE id=? AND worker_id=? AND status='processing'",
                (avail, reason[:500], _now(), work_id, worker_id))
            self._conn.commit()
            return cur.rowcount > 0

    def reclaim_stale_work(self, *, force_all: bool = False) -> int:
        """Return stale 'processing' items to the queue: 'retryable' while attempts remain, else
        'failed' (the attempt budget can never be exceeded through reclaims). By default only expired
        leases are reclaimed (live items renew via heartbeat). ``force_all`` reclaims EVERY processing
        item — used once at process startup, where the single-node invariant guarantees no other
        process can hold a live lease (a crashed process left them behind)."""
        now = _now()
        cond = "status='processing'" if force_all else \
            "status='processing' AND lease_expires_at IS NOT NULL AND lease_expires_at<?"
        args = () if force_all else (now,)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE work_items SET status=CASE WHEN attempt>=max_attempts THEN 'failed' ELSE 'retryable' END, "
                f"worker_id=NULL, lease_expires_at=NULL, available_at=?, "
                f"last_error_category=CASE WHEN attempt>=max_attempts THEN 'lease_expired_exhausted' ELSE 'lease_expired' END, "
                f"updated_at=? WHERE {cond}", (now, now, *args))
            self._conn.commit()
            return cur.rowcount

    def get_work_item(self, work_id: str) -> dict | None:
        return self._fetchone("SELECT * FROM work_items WHERE id=?", (work_id,))

    def get_work_items(self, job_id: str) -> list[dict]:
        return self._fetchall("SELECT * FROM work_items WHERE job_id=? ORDER BY created_at", (job_id,))

    def work_counts(self) -> dict[str, int]:
        rows = self._fetchall("SELECT status, COUNT(*) AS n FROM work_items GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def count_incomplete_ingest(self, job_id: str) -> int:
        """INGEST_FILE work items for a job that are not yet succeeded (pending/processing/retryable)."""
        r = self._fetchone(
            "SELECT COUNT(*) AS n FROM work_items WHERE job_id=? AND kind='INGEST_FILE' "
            "AND status IN ('pending','processing','retryable')", (job_id,))
        return r["n"] if r else 0

    def count_failed_ingest(self, job_id: str) -> int:
        r = self._fetchone(
            "SELECT COUNT(*) AS n FROM work_items WHERE job_id=? AND kind='INGEST_FILE' AND status='failed'",
            (job_id,))
        return r["n"] if r else 0

    def complete_ingest_and_maybe_enqueue_map(self, work_id: str, job_id: str, worker_id: str | None = None) -> bool:
        """Mark an INGEST_FILE item succeeded (ownership-checked) and, in the SAME transaction, enqueue
        MAP iff it was the last incomplete ingest and no ingest failed. Atomic + idempotent (unique MAP
        key), so the next stage is enqueued exactly once even with several files finishing concurrently
        or a crash between steps. Returns True if MAP was enqueued by this call."""
        clause, extra = self._owner_clause(worker_id)
        with self._lock:
            try:
                owned = self._conn.execute(
                    "UPDATE work_items SET status='succeeded', completed_at=?, lease_expires_at=NULL, "
                    f"updated_at=? WHERE id=?{clause}", (_now(), _now(), work_id, *extra)).rowcount
                if not owned:
                    self._conn.rollback()
                    return False
                inc = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM work_items WHERE job_id=? AND kind='INGEST_FILE' "
                    "AND status IN ('pending','processing','retryable')", (job_id,)).fetchone()["n"]
                failed = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM work_items WHERE job_id=? AND kind='INGEST_FILE' "
                    "AND status='failed'", (job_id,)).fetchone()["n"]
                enqueued = False
                if inc == 0 and failed == 0:
                    rc = self._conn.execute(
                        "INSERT OR IGNORE INTO work_items (id, job_id, kind, payload, status, attempt, "
                        "max_attempts, available_at, idempotency_key, created_at, updated_at) "
                        "VALUES (?,?,?,?, 'pending', 0, 5, ?, ?, ?, ?)",
                        (_nid("wk"), job_id, "MAP", "{}", _now(), f"{job_id}:map", _now(), _now())).rowcount
                    enqueued = rc > 0
                self._conn.commit()
                return enqueued
            except Exception:
                self._conn.rollback()
                raise

    # --- atomic: resolve a review issue AND enqueue continuation in one transaction ---
    def resolve_mapping_issue_and_enqueue(self, job_id: str, issue_id: str, *, expected_version: int,
                                          resolution: dict) -> tuple[str, bool]:
        """Resolve a mapping review issue; if it was the last open one, enqueue RESUME_MAPPING in the
        SAME transaction. Returns (outcome, enqueued)."""
        return self._resolve_and_enqueue("review_issues", job_id, issue_id, expected_version, resolution,
                                         kind="RESUME_MAPPING")

    def resolve_record_issue_and_enqueue(self, job_id: str, issue_id: str, *, expected_version: int,
                                         resolution: dict) -> tuple[str, bool]:
        return self._resolve_and_enqueue("record_issues", job_id, issue_id, expected_version, resolution,
                                         kind="RESUME_PREPARATION", audit_event="record_decision")

    def resolve_target_issue_and_enqueue(self, job_id: str, issue_id: str, *, expected_version: int,
                                         resolution: dict) -> tuple[str, bool]:
        return self._resolve_and_enqueue("target_review_issues", job_id, issue_id, expected_version,
                                         resolution, kind="RESUME_TARGET_REVIEW",
                                         audit_event="target_decision")

    def _resolve_and_enqueue(self, table: str, job_id: str, issue_id: str, expected_version: int,
                             resolution: dict, *, kind: str, audit_event: str | None = None) -> tuple[str, bool]:
        open_col = "status"
        with self._lock:
            try:
                row = self._conn.execute(f"SELECT * FROM {table} WHERE id=?", (issue_id,)).fetchone()
                if row is None:
                    self._conn.rollback()
                    return "not_found", False
                if row[open_col] == "resolved":
                    existing = json.loads(row["resolution"]) if row["resolution"] else None
                    outcome = "noop" if existing == resolution else "stale"
                elif row[open_col] == "superseded" or row["version"] != expected_version:
                    outcome = "stale"
                else:
                    cur = self._conn.execute(
                        f"UPDATE {table} SET status='resolved', resolution=?, version=version+1, "
                        f"updated_at=? WHERE id=? AND version=? AND status='open'",
                        (_j(resolution), _now(), issue_id, expected_version))
                    outcome = "resolved" if cur.rowcount else "stale"
                # Record the human decision in the SAME transaction (record/target decisions
                # otherwise have no audit event; mapping keeps its graph-emitted issue_resolved).
                if outcome == "resolved" and audit_event:
                    after = {k: v for k, v in resolution.items() if k != "actor"}
                    rowd = dict(row)
                    field = rowd.get("field")
                    # Link the decision to the affected employee (+ the source evidence of the
                    # chosen option when the issue carries per-option provenance) so the audit
                    # drawer can deep-link without inferring from JSON.
                    src = {"field": field, "candidate_key": rowd.get("candidate_key"),
                           "business_key": rowd.get("business_key") or rowd.get("candidate_key"),
                           "candidate_id": rowd.get("candidate_id")}
                    try:
                        aff = json.loads(rowd["affected"]) if rowd.get("affected") else {}
                    except Exception:  # noqa: BLE001
                        aff = {}
                    if not src["candidate_id"]:
                        src["candidate_id"] = aff.get("candidate_id") or resolution.get("candidate_id")
                    chosen = resolution.get("value")
                    if chosen is not None and isinstance(aff.get("options_detail"), list):
                        for od in aff["options_detail"]:
                            if str(od.get("value")) == str(chosen) and od.get("sources"):
                                src["provenance"] = od["sources"]
                                break
                    if "provenance" not in src and isinstance(aff.get("provenance"), list) and aff["provenance"]:
                        src["provenance"] = aff["provenance"]
                    if audit_event == "target_decision":
                        after["target_value"] = rowd.get("target_value")
                        after["incoming_value"] = rowd.get("incoming_value")
                        after["target_record_id"] = (aff.get("target") or {}).get("employee_id") \
                            or aff.get("target_record_id")
                        after["target_revision"] = (aff.get("target") or {}).get("revision")
                    self._conn.execute(
                        "INSERT INTO audit_events (id, job_id, issue_id, work_item_id, source_ref, "
                        "event_type, actor, before, after, reason, schema_version, policy_version, "
                        "model_version, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (_nid("aud"), job_id, issue_id, None, _j(src),
                         audit_event, "human", None, _j(after), resolution.get("reason") or resolution.get("note"),
                         None, None, None, _now()))
                enqueued = False
                if outcome in ("resolved", "noop"):
                    remaining = self._conn.execute(
                        f"SELECT COUNT(*) AS n FROM {table} WHERE job_id=? AND status='open'",
                        (job_id,)).fetchone()["n"]
                    if remaining == 0:
                        idem = f"{job_id}:{kind}:{issue_id}:{expected_version}"
                        rc = self._conn.execute(
                            "INSERT OR IGNORE INTO work_items (id, job_id, kind, payload, status, attempt, "
                            "max_attempts, available_at, idempotency_key, created_at, updated_at) "
                            "VALUES (?,?,?,?, 'pending', 0, 5, ?, ?, ?, ?)",
                            (_nid("wk"), job_id, kind, _j({"issue_id": issue_id}), _now(), idem,
                             _now(), _now())).rowcount
                        enqueued = rc > 0
                self._conn.commit()
                return outcome, enqueued
            except Exception:
                self._conn.rollback()
                raise

    # ===================== M3A: target reconciliation =====================
    def replace_target_snapshots(self, job_id: str, snapshots: list[dict]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM target_snapshots WHERE job_id=?", (job_id,))
            for s in snapshots:
                self._conn.execute(
                    "INSERT INTO target_snapshots (id, job_id, candidate_id, business_key, "
                    "target_record_id, match_basis, target_revision, target_payload, fetched_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (_nid("snap"), job_id, s["candidate_id"], s.get("business_key"),
                     s.get("target_record_id"), s["match_basis"], s.get("target_revision"),
                     _j(s.get("target_payload")) if s.get("target_payload") is not None else None, _now()))
            self._conn.commit()

    def get_target_snapshots(self, job_id: str) -> list[dict]:
        return self._fetchall("SELECT * FROM target_snapshots WHERE job_id=?", (job_id,))

    def replace_target_reconciliation(self, job_id: str, results: list[dict]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM target_reconciliation WHERE job_id=?", (job_id,))
            for r in results:
                self._conn.execute(
                    "INSERT INTO target_reconciliation (id, job_id, candidate_id, business_key, outcome, "
                    "target_record_id, target_revision, match_basis, diff, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (_nid("rec"), job_id, r["candidate_id"], r.get("business_key"), r["outcome"],
                     r.get("target_record_id"), r.get("target_revision"), r["match_basis"],
                     _j(r.get("diff")) if r.get("diff") is not None else None, _now()))
            self._conn.commit()

    def get_target_reconciliation(self, job_id: str) -> list[dict]:
        return self._fetchall("SELECT * FROM target_reconciliation WHERE job_id=?", (job_id,))

    def upsert_target_review_issue(self, job_id: str, *, issue_id: str, candidate_id: str,
                                   business_key: str | None, field: str | None, issue_type: str,
                                   reason: str, incoming_value: str | None, target_value: str | None,
                                   match_basis: str, options: list, affected: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO target_review_issues (id, job_id, candidate_id, business_key, field, "
                "issue_type, reason, incoming_value, target_value, match_basis, options, affected, "
                "status, version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'open',1,?,?) "
                "ON CONFLICT(id) DO UPDATE SET reason=excluded.reason, options=excluded.options, "
                "affected=excluded.affected, incoming_value=excluded.incoming_value, "
                "target_value=excluded.target_value, "
                "status=CASE WHEN target_review_issues.status='superseded' THEN 'open' "
                "ELSE target_review_issues.status END, updated_at=excluded.updated_at",
                (issue_id, job_id, candidate_id, business_key, field, issue_type, reason, incoming_value,
                 target_value, match_basis, _j(options), _j(affected), _now(), _now()))
            self._conn.commit()

    def get_target_review_issue(self, issue_id: str) -> dict | None:
        return self._fetchone("SELECT * FROM target_review_issues WHERE id=?", (issue_id,))

    def get_target_review_issues(self, job_id: str, status: str | None = None) -> list[dict]:
        if status:
            return self._fetchall("SELECT * FROM target_review_issues WHERE job_id=? AND status=? "
                                  "ORDER BY created_at", (job_id, status))
        return self._fetchall("SELECT * FROM target_review_issues WHERE job_id=? ORDER BY created_at",
                              (job_id,))

    def supersede_target_issues_not_in(self, job_id: str, keep_ids: set[str]) -> None:
        rows = self._fetchall("SELECT id FROM target_review_issues WHERE job_id=? AND status='open'",
                              (job_id,))
        stale = [r["id"] for r in rows if r["id"] not in keep_ids]
        with self._lock:
            for sid in stale:
                self._conn.execute("UPDATE target_review_issues SET status='superseded', updated_at=? "
                                   "WHERE id=?", (_now(), sid))
            self._conn.commit()

    def get_resolved_target_decisions(self, job_id: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in self._fetchall("SELECT id, resolution FROM target_review_issues WHERE job_id=? "
                                "AND status='resolved'", (job_id,)):
            if r["resolution"]:
                out[r["id"]] = json.loads(r["resolution"])
        return out

    # ===================== M3A.2: tenants + custom fields + proposals =====================
    def upsert_tenant(self, tenant_id: str, name: str | None = None) -> None:
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO tenants (id, name, created_at) VALUES (?,?,?)",
                               (tenant_id, name or tenant_id, _now()))
            if name:
                self._conn.execute("UPDATE tenants SET name=? WHERE id=?", (name, tenant_id))
            self._conn.commit()

    def list_tenants(self) -> list[dict]:
        return self._fetchall("SELECT * FROM tenants ORDER BY id")

    def get_tenant(self, tenant_id: str) -> dict | None:
        return self._fetchone("SELECT * FROM tenants WHERE id=?", (tenant_id,))

    def add_custom_field_definition(self, *, tenant_id: str, key: str, label: str, type: str,
                                    required: bool = False, options: list | None = None,
                                    multi_value: bool = False, description: str | None = None,
                                    aliases: list | None = None, origin: str = "api",
                                    origin_proposal_id: str | None = None, origin_job_id: str | None = None,
                                    created_by: str = "human",
                                    conn: sqlite3.Connection | None = None) -> dict | None:
        """Create a tenant custom-field definition. Returns the row, or None if (tenant, key)
        already exists (idempotent seeds / replay never create a duplicate definition)."""
        did = _nid("cfd")
        sql = ("INSERT OR IGNORE INTO custom_field_definitions (id, tenant_id, key, label, type, required, "
               "options, multi_value, description, aliases, origin, origin_proposal_id, origin_job_id, "
               "created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
        args = (did, tenant_id, key, label, type, 1 if required else 0,
                _j(options) if options is not None else None, 1 if multi_value else 0, description,
                _j(aliases or []), origin, origin_proposal_id, origin_job_id, created_by, _now())
        if conn is not None:
            conn.execute("INSERT OR IGNORE INTO tenants (id, name, created_at) VALUES (?,?,?)",
                         (tenant_id, tenant_id, _now()))
            rc = conn.execute(sql, args).rowcount
            if rc == 0:
                return None
            row = conn.execute("SELECT * FROM custom_field_definitions WHERE id=?", (did,)).fetchone()
            return _def_row(row)
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO tenants (id, name, created_at) VALUES (?,?,?)",
                               (tenant_id, tenant_id, _now()))
            rc = self._conn.execute(sql, args).rowcount
            self._conn.commit()
            if rc == 0:
                return None
            row = self._conn.execute("SELECT * FROM custom_field_definitions WHERE id=?", (did,)).fetchone()
            return _def_row(row)

    def get_custom_field_definitions(self, tenant_id: str) -> list[dict]:
        return [_def_row(r) for r in self._fetchall(
            "SELECT * FROM custom_field_definitions WHERE tenant_id=? ORDER BY created_at, key", (tenant_id,))]

    def get_custom_field_definition(self, definition_id: str) -> dict | None:
        r = self._fetchone("SELECT * FROM custom_field_definitions WHERE id=?", (definition_id,))
        return _def_row(r) if r else None

    def find_custom_field_definition(self, tenant_id: str, key: str) -> dict | None:
        r = self._fetchone("SELECT * FROM custom_field_definitions WHERE tenant_id=? AND key=?", (tenant_id, key))
        return _def_row(r) if r else None

    def upsert_custom_field_proposal(self, job_id: str, *, proposal_id: str, tenant_id: str, profile_id: str,
                                     table_id: str, source_header: str, origin: str, suggestion: dict,
                                     observed_values: list, non_empty_count: int) -> bool:
        """Idempotent: an existing proposal (same deterministic id) keeps its status/decision; only
        its observed evidence is refreshed. Returns True if newly created."""
        with self._lock:
            exists = self._conn.execute("SELECT id FROM custom_field_proposals WHERE id=?", (proposal_id,)).fetchone()
            if exists:
                self._conn.execute(
                    "UPDATE custom_field_proposals SET observed_values=?, non_empty_count=?, updated_at=? WHERE id=?",
                    (_j(observed_values), non_empty_count, _now(), proposal_id))
                self._conn.commit()
                return False
            self._conn.execute(
                "INSERT INTO custom_field_proposals (id, job_id, tenant_id, profile_id, table_id, source_header, "
                "origin, suggestion, observed_values, non_empty_count, status, version, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,'open',1,?,?)",
                (proposal_id, job_id, tenant_id, profile_id, table_id, source_header, origin, _j(suggestion),
                 _j(observed_values), non_empty_count, _now(), _now()))
            self._conn.commit()
            return True

    def get_custom_field_proposal(self, proposal_id: str) -> dict | None:
        return self._fetchone("SELECT * FROM custom_field_proposals WHERE id=?", (proposal_id,))

    def get_custom_field_proposals(self, job_id: str, status: str | None = None) -> list[dict]:
        if status:
            return self._fetchall("SELECT * FROM custom_field_proposals WHERE job_id=? AND status=? "
                                  "ORDER BY created_at", (job_id, status))
        return self._fetchall("SELECT * FROM custom_field_proposals WHERE job_id=? ORDER BY created_at", (job_id,))

    def supersede_open_proposals_not_in(self, job_id: str, keep_ids: set[str]) -> None:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM custom_field_proposals WHERE job_id=? AND status='open'", (job_id,)).fetchall()
            for r in rows:
                if r["id"] not in keep_ids:
                    self._conn.execute("UPDATE custom_field_proposals SET status='superseded', updated_at=? "
                                       "WHERE id=?", (_now(), r["id"]))
            self._conn.commit()

    def resolve_custom_field_proposal(self, job_id: str, proposal_id: str, *, expected_version: int,
                                      resolution: dict, new_status: str, definition: dict | None,
                                      decision: dict, audit_event: str, audit_after: dict,
                                      rerun_kind: str | None) -> tuple[str, bool, dict | None]:
        """Atomically: resolve the proposal (version-checked), optionally create the tenant custom-field
        definition, upsert the human mapping decision for the source column, write the audit event, and
        (if no open proposals remain and a rerun is requested) enqueue a MAP re-run with a fresh key.
        Returns (outcome, enqueued, definition_row). outcome: resolved | noop | stale | not_found |
        key_exists (definition key already exists for this tenant; nothing written)."""
        with self._lock:
            try:
                row = self._conn.execute("SELECT * FROM custom_field_proposals WHERE id=? AND job_id=?",
                                         (proposal_id, job_id)).fetchone()
                if row is None:
                    return "not_found", False, None
                if row["status"] != "open":
                    existing = json.loads(row["resolution"]) if row["resolution"] else None
                    return ("noop" if existing == resolution else "stale"), False, None
                if row["version"] != expected_version:
                    return "stale", False, None
                created_def: dict | None = None
                if definition is not None:
                    created_def = self.add_custom_field_definition(conn=self._conn, **definition)
                    if created_def is None:
                        self._conn.rollback()
                        return "key_exists", False, None
                    decision = dict(decision, custom_definition_id=created_def["id"])
                self._conn.execute(
                    "UPDATE custom_field_proposals SET status=?, resolution=?, version=version+1, "
                    "definition_id=?, updated_at=? WHERE id=? AND version=? AND status='open'",
                    (new_status, _j(resolution), (created_def or {}).get("id") or decision.get("custom_definition_id"),
                     _now(), proposal_id, expected_version))
                self.upsert_decision(job_id, conn=self._conn, **decision)
                after = dict(audit_after)
                if created_def:
                    after["definition_id"] = created_def["id"]
                self._conn.execute(
                    "INSERT INTO audit_events (id, job_id, issue_id, work_item_id, source_ref, event_type, actor, "
                    "before, after, reason, schema_version, policy_version, model_version, ts) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (_nid("aud"), job_id, proposal_id, None,
                     _j({"profile_id": row["profile_id"], "header": row["source_header"], "table_id": row["table_id"],
                         "tenant_id": row["tenant_id"]}),
                     audit_event, "human", None, _j(after), resolution.get("note"), None, None, None, _now()))
                enqueued = False
                remaining = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM custom_field_proposals WHERE job_id=? AND status='open'",
                    (job_id,)).fetchone()["n"]
                self._last_rerun_status = None
                if remaining == 0 and rerun_kind:
                    st = self._enqueue_stage_rerun_conn(self._conn, job_id, rerun_kind, f"{job_id}:{rerun_kind.lower()}")
                    self._last_rerun_status = st          # queued | already_active (worker chains it) | noop
                    enqueued = st == "queued"
                self._conn.commit()
                return "resolved", enqueued, created_def
            except Exception:
                self._conn.rollback()
                raise

    # --- stage re-runs (shared by routes + worker) ---------------------------------------
    def _enqueue_stage_rerun_conn(self, conn: sqlite3.Connection, job_id: str, kind: str, base_key: str,
                                  payload: dict | None = None) -> str:
        items = conn.execute("SELECT status, idempotency_key FROM work_items WHERE job_id=? AND kind=?",
                             (job_id, kind)).fetchall()
        if any(w["status"] in ("pending", "processing", "retryable") for w in items):
            return "already_active"
        key = base_key if not any(w["idempotency_key"] == base_key for w in items) \
            else f"{base_key}:rerun:{len(items)}"
        rc = conn.execute(
            "INSERT OR IGNORE INTO work_items (id, job_id, kind, payload, status, attempt, max_attempts, "
            "available_at, idempotency_key, created_at, updated_at) VALUES (?,?,?,?, 'pending', 0, 5, ?, ?, ?, ?)",
            (_nid("wk"), job_id, kind, _j(payload or {}), _now(), key, _now(), _now())).rowcount
        return "queued" if rc > 0 else "noop"

    def enqueue_stage_rerun(self, job_id: str, kind: str, base_key: str, payload: dict | None = None) -> str:
        """Enqueue a job-level stage. Avoids stacking while one is active; a re-trigger after the base key
        already exists gets a fresh (rerun) key so it actually runs. Returns queued|already_active|noop."""
        with self._lock:
            try:
                r = self._enqueue_stage_rerun_conn(self._conn, job_id, kind, base_key, payload)
                self._conn.commit()
                return r
            except Exception:
                self._conn.rollback()
                raise

    # ===================== M3A.1: immutable employee versions =====================
    @staticmethod
    def snapshot_hash(snapshot: dict) -> str:
        """Deterministic hash of a complete effective employee snapshot (idempotent dedup)."""
        return hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()

    def add_employee_version(self, job_id: str, candidate_id: str, *, snapshot: dict, origin: str,
                             created_by: str, business_key: str | None = None,
                             change_reason: str | None = None, decision_note: str | None = None,
                             decision_id: str | None = None, target_revision: int | None = None,
                             restores_version_id: str | None = None,
                             field_changes: list | None = None, dedup: bool = True) -> dict | None:
        """Append an immutable employee version. Idempotent: if ``dedup`` and the latest version's
        record_hash already equals this snapshot's hash, no version is created (returns None).
        version_no is assigned transactionally; previous versions are never mutated."""
        new_hash = self.snapshot_hash(snapshot)
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT * FROM employee_versions WHERE job_id=? AND candidate_id=? ORDER BY version_no",
                    (job_id, candidate_id)).fetchall()
                latest = dict(rows[-1]) if rows else None
                if dedup and latest and latest["record_hash"] == new_hash:
                    return None
                version_no = (latest["version_no"] + 1) if latest else 1
                vid = _nid("ver")
                self._conn.execute(
                    "INSERT INTO employee_versions (id, job_id, candidate_id, business_key, version_no, "
                    "parent_version_id, origin, snapshot, record_hash, change_reason, decision_note, "
                    "decision_id, target_revision, restores_version_id, field_changes, created_by, "
                    "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (vid, job_id, candidate_id, business_key, version_no,
                     latest["id"] if latest else None, origin, _j(snapshot), new_hash, change_reason,
                     decision_note, decision_id, target_revision, restores_version_id,
                     _j(field_changes) if field_changes is not None else None, created_by, _now()))
                self._conn.commit()
                row = self._conn.execute(
                    "SELECT * FROM employee_versions WHERE id=?", (vid,)).fetchone()  # same held lock
                return dict(row) if row else None
            except Exception:
                self._conn.rollback()
                raise

    def get_employee_versions(self, job_id: str, candidate_id: str) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM employee_versions WHERE job_id=? AND candidate_id=? ORDER BY version_no",
            (job_id, candidate_id))

    def get_employee_version(self, job_id: str, candidate_id: str, version_no: int) -> dict | None:
        return self._fetchone(
            "SELECT * FROM employee_versions WHERE job_id=? AND candidate_id=? AND version_no=?",
            (job_id, candidate_id, int(version_no)))


    # ======================= M3B: delivery operations + attempts ============================

    def create_delivery_operation(self, *, job_id: str, candidate_id: str, employee_id: str | None,
                                  op_type: str, payload: dict, expected_target_revision: int | None,
                                  target_record_id: str | None, before_snapshot: dict | None,
                                  desired_version_id: str | None, idempotency_key: str) -> dict:
        oid = _nid("dop")
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO delivery_operations (id, job_id, candidate_id, employee_id, op_type, "
                "payload, expected_target_revision, target_record_id, before_snapshot, desired_version_id, "
                "idempotency_key, status, attempt_count, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,'PLANNED',0,?,?)",
                (oid, job_id, candidate_id, employee_id, op_type, _j(payload),
                 expected_target_revision, target_record_id,
                 _j(before_snapshot) if before_snapshot else None,
                 desired_version_id, idempotency_key, now, now))
            self._conn.commit()
        return dict(self._conn.execute("SELECT * FROM delivery_operations WHERE id=?", (oid,)).fetchone())

    def get_delivery_operation(self, op_id: str) -> dict | None:
        return self._fetchone("SELECT * FROM delivery_operations WHERE id=?", (op_id,))

    def get_delivery_operations(self, job_id: str, *, status: str | None = None) -> list[dict]:
        if status:
            return self._fetchall(
                "SELECT * FROM delivery_operations WHERE job_id=? AND status=? ORDER BY created_at",
                (job_id, status))
        return self._fetchall(
            "SELECT * FROM delivery_operations WHERE job_id=? ORDER BY created_at", (job_id,))

    def get_delivery_operation_for_candidate(self, job_id: str, candidate_id: str) -> dict | None:
        return self._fetchone(
            "SELECT * FROM delivery_operations WHERE job_id=? AND candidate_id=?",
            (job_id, candidate_id))

    def claim_operation_work(self, op_id: str, work_item_id: str, *, rollback: bool = False) -> bool:
        """M3B.2 operation-level execution fence. Atomically bind the operation to ONE work item
        before any target side effect. Succeeds when the slot is free OR already held by the SAME
        work item (so a durable reclaim after lease expiry / restart still works). A DIFFERENT work
        item for the same op gets rowcount 0 and must skip. Defense-in-depth over target idempotency."""
        col = "rollback_work_item_id" if rollback else "work_item_id"
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE delivery_operations SET {col}=?, updated_at=? "
                f"WHERE id=? AND ({col} IS NULL OR {col}=?)",
                (work_item_id, _now(), op_id, work_item_id))
            self._conn.commit()
            return cur.rowcount > 0

    def release_operation_work(self, op_id: str, *, rollback: bool = False) -> None:
        """Clear the operation's execution claim so a NEW work generation (manual retry, stale
        replan) can claim it. Called whenever the op is reset to a claimable state."""
        col = "rollback_work_item_id" if rollback else "work_item_id"
        with self._lock:
            self._conn.execute(
                f"UPDATE delivery_operations SET {col}=NULL, updated_at=? WHERE id=?",
                (_now(), op_id))
            self._conn.commit()

    def update_delivery_operation(self, op_id: str, **fields) -> bool:
        """Update specified fields on a delivery operation. Returns True if row found."""
        allowed = {"status", "attempt_count", "last_error", "target_revision_after",
                   "target_request_id", "work_item_id", "rollback_work_item_id",
                   "employee_id", "payload", "expected_target_revision", "before_snapshot",
                   "target_record_id", "idempotency_key", "desired_version_id", "op_type"}
        sets = []
        vals = []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"disallowed field: {k}")
            if k in ("payload", "before_snapshot") and isinstance(v, dict):
                v = _j(v)
            sets.append(f"{k}=?")
            vals.append(v)
        if not sets:
            return True
        sets.append("updated_at=?")
        vals.append(_now())
        vals.append(op_id)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE delivery_operations SET {','.join(sets)} WHERE id=?", tuple(vals))
            self._conn.commit()
            return cur.rowcount > 0

    def transition_delivery_op(self, op_id: str, from_status: str, to_status: str, **extra) -> bool:
        """Conditional status transition (optimistic — refuses if current status != from_status)."""
        sets = ["status=?", "updated_at=?"]
        vals: list = [to_status, _now()]
        allowed = {"attempt_count", "last_error", "target_revision_after", "target_request_id",
                   "work_item_id", "rollback_work_item_id", "employee_id", "payload",
                   "expected_target_revision", "before_snapshot", "target_record_id",
                   "idempotency_key", "desired_version_id", "op_type"}
        for k, v in extra.items():
            if k not in allowed:
                raise ValueError(f"disallowed field: {k}")
            if k in ("payload", "before_snapshot") and isinstance(v, dict):
                v = _j(v)
            sets.append(f"{k}=?")
            vals.append(v)
        vals.extend([op_id, from_status])
        with self._lock:
            set_clause = ",".join(sets)
            cur = self._conn.execute(
                f"UPDATE delivery_operations SET {set_clause} WHERE id=? AND status=?", tuple(vals))
            self._conn.commit()
            return cur.rowcount > 0

    def delivery_counts(self, job_id: str) -> dict[str, int]:
        rows = self._fetchall(
            "SELECT status, COUNT(*) AS c FROM delivery_operations WHERE job_id=? GROUP BY status",
            (job_id,))
        return {r["status"]: r["c"] for r in rows}

    def set_delivery_summary(self, job_id: str, summary: dict) -> None:
        with self._lock:
            self._conn.execute("UPDATE jobs SET delivery_summary=?, updated_at=? WHERE id=?",
                               (_j(summary), _now(), job_id))
            self._conn.commit()

    # --- attempt ledger ----------------------------------------------------------------
    def add_delivery_attempt(self, *, operation_id: str, attempt_no: int, action: str) -> str:
        aid = _nid("datm")
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO delivery_attempts (id, operation_id, attempt_no, action, started_at, result, created_at) "
                "VALUES (?,?,?,?,?,'pending',?)", (aid, operation_id, attempt_no, action, now, now))
            self._conn.commit()
        return aid

    def complete_delivery_attempt(self, attempt_id: str, *, http_status: int | None, retryable: bool | None,
                                  error_category: str | None, retry_after: float | None,
                                  target_request_id: str | None, response_meta: dict | None,
                                  result: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE delivery_attempts SET completed_at=?, http_status=?, retryable=?, error_category=?, "
                "retry_after=?, target_request_id=?, response_meta=?, result=? WHERE id=?",
                (_now(), http_status, 1 if retryable else (0 if retryable is not None else None),
                 error_category, retry_after, target_request_id,
                 _j(response_meta) if response_meta else None, result, attempt_id))
            self._conn.commit()

    def get_delivery_attempts(self, operation_id: str) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM delivery_attempts WHERE operation_id=? ORDER BY attempt_no", (operation_id,))

    def delivery_attempt_count(self, operation_id: str) -> int:
        r = self._fetchone("SELECT COUNT(*) AS c FROM delivery_attempts WHERE operation_id=?", (operation_id,))
        return r["c"] if r else 0

    # --- repository methods for delivery business logic (no raw _conn/_lock from callers) ---

    def update_target_snapshot(self, job_id: str, candidate_id: str, *,
                               target_payload: dict, target_revision: int | None,
                               fetched_at: str | None = None) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE target_snapshots SET target_payload=?, target_revision=?, fetched_at=? "
                "WHERE job_id=? AND candidate_id=?",
                (_j(target_payload), target_revision, fetched_at or _now(), job_id, candidate_id))
            self._conn.commit()
            return cur.rowcount > 0

    def update_target_reconciliation_row(self, job_id: str, candidate_id: str, *,
                                         outcome: str, target_revision: int | None,
                                         diff: dict | None) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE target_reconciliation SET outcome=?, target_revision=?, diff=?, created_at=? "
                "WHERE job_id=? AND candidate_id=?",
                (outcome, target_revision, _j(diff) if diff is not None else None,
                 _now(), job_id, candidate_id))
            self._conn.commit()
            return cur.rowcount > 0

    def supersede_target_review_issue(self, issue_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE target_review_issues SET status='superseded', updated_at=? "
                "WHERE id=? AND status='resolved'",
                (_now(), issue_id))
            self._conn.commit()
            return cur.rowcount > 0
