"""M3A: durable SQLite work queue + bounded local worker pool.

Correctness comes from persisted work status + an atomic conditional-UPDATE claim + idempotent
stage transitions, NOT from in-memory task ownership. Covers: atomic claim (no double
processing), idempotent enqueue, retry->terminal, stale-lease reclaim, bounded concurrency
(<= MAX_FILE_WORKERS), a corrupt blob blocking parse, and a failed file blocking the next stage.
"""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.db import Database
from app.runtime import AppContext


# ----------------------------- pure queue mechanics -----------------------------
def _db(tmp_path) -> Database:
    return Database(tmp_path / "app.db")


def test_atomic_claim_no_double_processing(tmp_path):
    db = _db(tmp_path)
    try:
        job = db.create_job(schema_version="v", provider="fake", model_id="m", adapter_kind="fake")
        for i in range(5):
            db.enqueue_work(job_id=job, kind="INGEST_FILE", source_file_id=f"f{i}",
                            idempotency_key=f"{job}:ingest:f{i}")
        claimed = []
        for _ in range(5):
            item = db.claim_next_work("w1", lease_seconds=60)
            assert item is not None
            claimed.append(item["id"])
        assert len(set(claimed)) == 5              # each item claimed exactly once
        assert db.claim_next_work("w1", lease_seconds=60) is None   # nothing left
    finally:
        db.close()


def test_concurrent_claimers_never_share_an_item(tmp_path):
    db = _db(tmp_path)
    try:
        job = db.create_job(schema_version="v", provider="fake", model_id="m", adapter_kind="fake")
        n = 40
        for i in range(n):
            db.enqueue_work(job_id=job, kind="INGEST_FILE", source_file_id=f"f{i}",
                            idempotency_key=f"{job}:ingest:f{i}")
        seen: list[str] = []
        lock = threading.Lock()

        def drain(wid):
            while True:
                it = db.claim_next_work(wid, lease_seconds=60)
                if it is None:
                    return
                with lock:
                    seen.append(it["id"])

        threads = [threading.Thread(target=drain, args=(f"w{k}",)) for k in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(seen) == n and len(set(seen)) == n   # no item claimed twice
    finally:
        db.close()


def test_enqueue_is_idempotent(tmp_path):
    db = _db(tmp_path)
    try:
        job = db.create_job(schema_version="v", provider="fake", model_id="m", adapter_kind="fake")
        assert db.enqueue_work(job_id=job, kind="MAP", idempotency_key=f"{job}:map") is True
        assert db.enqueue_work(job_id=job, kind="MAP", idempotency_key=f"{job}:map") is False
        assert len([w for w in db.get_work_items(job) if w["kind"] == "MAP"]) == 1
    finally:
        db.close()


def test_fail_work_retries_then_goes_terminal(tmp_path):
    db = _db(tmp_path)
    try:
        job = db.create_job(schema_version="v", provider="fake", model_id="m", adapter_kind="fake")
        db.enqueue_work(job_id=job, kind="MAP", idempotency_key=f"{job}:map", max_attempts=3)
        statuses = []
        for _ in range(3):
            item = db.claim_next_work("w1", lease_seconds=60)   # attempt++ on claim
            assert item is not None
            statuses.append(db.fail_work(item["id"], category="err", error="boom", backoff_seconds=0))
        assert statuses == ["retryable", "retryable", "failed"]   # 3 attempts -> terminal
    finally:
        db.close()


def test_reclaim_stale_processing(tmp_path):
    db = _db(tmp_path)
    try:
        job = db.create_job(schema_version="v", provider="fake", model_id="m", adapter_kind="fake")
        db.enqueue_work(job_id=job, kind="MAP", idempotency_key=f"{job}:map")
        item = db.claim_next_work("w1", lease_seconds=60)
        # Force the lease into the past (simulate a crashed worker holding it).
        past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        with db._lock:
            db._conn.execute("UPDATE work_items SET lease_expires_at=? WHERE id=?", (past, item["id"]))
            db._conn.commit()
        assert db.reclaim_stale_work() == 1
        got = db.get_work_item(item["id"])
        assert got["status"] == "retryable" and got["last_error_category"] == "lease_expired"
        # A live (non-expired) lease is NOT reclaimed.
        db.claim_next_work("w2", lease_seconds=60)
        assert db.reclaim_stale_work() == 0
    finally:
        db.close()


def test_terminal_failure_no_retry(tmp_path):
    db = _db(tmp_path)
    try:
        job = db.create_job(schema_version="v", provider="fake", model_id="m", adapter_kind="fake")
        db.enqueue_work(job_id=job, kind="INGEST_FILE", idempotency_key=f"{job}:ingest:f1")
        item = db.claim_next_work("w1", lease_seconds=60)
        db.set_work_failed(item["id"], category="terminal", error="checksum mismatch")
        got = db.get_work_item(item["id"])
        assert got["status"] == "failed"
        assert db.claim_next_work("w1", lease_seconds=60) is None   # not retried
    finally:
        db.close()


# ----------------------------- worker-pool behaviour -----------------------------
def _enqueue_file(ctx, job_id: str, name: str, data: bytes, *, corrupt: bool = False) -> str:
    info = ctx.blobstore.put(data, suffix=".csv")
    fid = f"file_{info.key.split('.')[0][:12]}"
    ctx.db.add_source_file(job_id, file_id=fid, original_filename=name, stored_name=info.key,
                           content_type="text/csv", size_bytes=info.size_bytes,
                           blob_key=info.key, sha256=info.sha256, storage_status="stored",
                           parse_status="queued")
    if corrupt:  # tamper with the stored bytes AFTER the checksum was recorded
        ctx.blobstore._path(info.key).write_bytes(data + b"TAMPERED")
    ctx.db.enqueue_work(job_id=job_id, kind="INGEST_FILE", source_file_id=fid,
                        idempotency_key=f"{job_id}:ingest:{fid}")
    return fid


async def _await_for(pred, tries=300, delay=0.05):
    """Poll a predicate while YIELDING to the event loop, so the in-process worker tasks
    (which share this test's loop) can actually run. A blocking time.sleep would starve them."""
    for _ in range(tries):
        if pred():
            return True
        await asyncio.sleep(delay)
    return False


async def test_bounded_concurrency_never_exceeds_max_workers(temp_settings, monkeypatch):
    temp_settings.max_file_workers = 2
    import app.worker as worker_mod
    real_parse = worker_mod.parse_file
    state = {"cur": 0, "max": 0}
    lock = threading.Lock()

    def slow_parse(**kwargs):
        with lock:
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
        try:
            time.sleep(0.15)                       # force overlap between concurrent parses
            return real_parse(**kwargs)
        finally:
            with lock:
                state["cur"] -= 1

    monkeypatch.setattr(worker_mod, "parse_file", slow_parse)
    ctx = await AppContext.create(temp_settings)
    try:
        job = ctx.db.create_job(schema_version=ctx.schema.version, provider=ctx.provider,
                                model_id=ctx.model_id, adapter_kind=ctx.adapter_kind)
        header = b"employee_id,full_name,work_email,department,hire_date,contract_start_date\n"
        for i in range(6):
            _enqueue_file(ctx, job, f"f{i}.csv", header + f"00{i},N{i},n{i}@x.com,Sales,2020-01-01,\n".encode())
        assert await _await_for(lambda: len([f for f in ctx.db.get_source_files(job)
                                             if f["parse_status"] == "parsed"]) == 6, tries=400)
        assert state["max"] <= 2, f"observed {state['max']} concurrent parses > MAX_FILE_WORKERS=2"
        assert state["max"] >= 2, "expected genuine parallelism with 2 workers"
    finally:
        await ctx.aclose()


async def test_corrupt_blob_blocks_parse_and_next_stage(temp_settings):
    ctx = await AppContext.create(temp_settings)
    try:
        job = ctx.db.create_job(schema_version=ctx.schema.version, provider=ctx.provider,
                                model_id=ctx.model_id, adapter_kind=ctx.adapter_kind)
        header = b"employee_id,full_name,work_email,department,hire_date,contract_start_date\n"
        good = _enqueue_file(ctx, job, "good.csv", header + b"001,Alice,alice@x.com,Sales,2020-01-01,\n")
        bad = _enqueue_file(ctx, job, "bad.csv", header + b"002,Bob,bob@x.com,Sales,2020-02-02,\n",
                            corrupt=True)

        def settled():
            files = {f["id"]: f for f in ctx.db.get_source_files(job)}
            return files[good]["parse_status"] == "parsed" and files[bad]["parse_status"] == "failed"
        assert await _await_for(settled), "expected good->parsed and bad->failed"

        files = {f["id"]: f for f in ctx.db.get_source_files(job)}
        assert "checksum" in (files[bad]["parse_error"] or "").lower()
        bad_wi = next(w for w in ctx.db.get_work_items(job)
                      if w["source_file_id"] == bad and w["kind"] == "INGEST_FILE")
        assert bad_wi["status"] == "failed"                 # terminal, not retried forever
        # A failed file must BLOCK the next stage: MAP is never enqueued while an ingest failed.
        await asyncio.sleep(0.3)
        assert not any(w["kind"] == "MAP" for w in ctx.db.get_work_items(job))
        assert ctx.db.count_failed_ingest(job) == 1
        assert ctx.db.get_job(job)["status"] not in ("mapping_queued", "mapping_complete",
                                                     "preparation_complete")  # never advanced
    finally:
        await ctx.aclose()


async def test_all_files_parsed_enqueues_map_exactly_once(temp_settings):
    ctx = await AppContext.create(temp_settings)
    try:
        job = ctx.db.create_job(schema_version=ctx.schema.version, provider=ctx.provider,
                                model_id=ctx.model_id, adapter_kind=ctx.adapter_kind)
        header = b"employee_id,full_name,work_email,department,hire_date,contract_start_date\n"
        for i in range(3):
            _enqueue_file(ctx, job, f"f{i}.csv",
                          header + f"00{i},N{i},n{i}@x.com,Sales,2020-01-0{i+1},\n".encode())
        assert await _await_for(lambda: any(w["kind"] == "MAP" for w in ctx.db.get_work_items(job)))
        # Exactly one MAP work item despite three files finishing independently.
        assert len([w for w in ctx.db.get_work_items(job) if w["kind"] == "MAP"]) == 1
    finally:
        await ctx.aclose()
