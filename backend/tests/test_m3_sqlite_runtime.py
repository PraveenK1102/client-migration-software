"""M3A: SQLite is hardened for a single-node concurrent runtime.

WAL mode (concurrent readers + one writer), a busy timeout (wait, don't fail, on a briefly
locked write), synchronous=NORMAL, and short transactions (a commit per write — no transaction
is held open across a model call, file parse, target HTTP call, or human wait).
"""
from __future__ import annotations

from app.db import Database


def test_wal_busy_timeout_and_synchronous(tmp_path):
    db = Database(tmp_path / "app.db", busy_timeout_ms=1234)
    try:
        assert db._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert db._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1234
        assert db._conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    finally:
        db.close()


def test_writes_commit_immediately_no_open_transaction(tmp_path):
    """Each write commits; the connection is never left mid-transaction between calls."""
    db = Database(tmp_path / "app.db")
    try:
        job_id = db.create_job(schema_version="employee.v1", provider="fake",
                               model_id="m", adapter_kind="fake")
        assert db._conn.in_transaction is False
        db.enqueue_work(job_id=job_id, kind="INGEST_FILE", idempotency_key=f"{job_id}:ingest:f1",
                        source_file_id="f1")
        assert db._conn.in_transaction is False
        # A second connection can immediately read the committed row (WAL: readers don't block).
        db2 = Database(tmp_path / "app.db")
        try:
            assert len(db2.get_work_items(job_id)) == 1
        finally:
            db2.close()
    finally:
        db.close()


def test_wal_reader_sees_committed_writes_from_other_connection(tmp_path):
    writer = Database(tmp_path / "app.db")
    reader = Database(tmp_path / "app.db")
    try:
        job_id = writer.create_job(schema_version="employee.v1", provider="fake",
                                   model_id="m", adapter_kind="fake")
        writer.set_job_stage(job_id, status="queued", stage="queued")
        assert reader.get_job(job_id)["status"] == "queued"
    finally:
        writer.close()
        reader.close()
