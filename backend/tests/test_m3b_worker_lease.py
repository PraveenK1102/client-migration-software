"""M3B preflight: worker lease ownership must be PROVEN before completing/failing work, the attempt
budget can never be exceeded through lease reclaims, an owned item can be deferred without consuming
an attempt, and a crashed process's items are reclaimed at startup. External side effects depend on
these guarantees."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from app.db import Database


def _db(tmp_path) -> Database:
    return Database(tmp_path / "app.db")


def _job(db) -> str:
    return db.create_job(schema_version="v", provider="fake", model_id="m", adapter_kind="fake")


def _expire_lease(db, work_id):
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    with db._lock:
        db._conn.execute("UPDATE work_items SET lease_expires_at=? WHERE id=?", (past, work_id))
        db._conn.commit()


def test_stale_worker_cannot_complete_or_fail_reclaimed_work(tmp_path):
    db = _db(tmp_path)
    try:
        job = _job(db)
        db.enqueue_work(job_id=job, kind="MAP", idempotency_key=f"{job}:map", max_attempts=5)
        item = db.claim_next_work("w1", lease_seconds=1)
        assert item and item["worker_id"] == "w1"
        _expire_lease(db, item["id"])
        assert db.reclaim_stale_work() == 1                      # lease expired -> back to retryable
        item2 = db.claim_next_work("w2", lease_seconds=60)      # a NEWER claim by another worker
        assert item2 and item2["id"] == item["id"] and item2["worker_id"] == "w2"
        # The stale worker w1 tries to finish "its" work: every mutation is refused.
        assert db.complete_work(item["id"], "w1") is False
        assert db.fail_work(item["id"], "w1", category="x", error="stale") == "not_owned"
        assert db.set_work_failed(item["id"], "w1", category="x", error="stale") is False
        assert db.work_owned_by(item["id"], "w1") is False
        assert db.work_owned_by(item["id"], "w2") is True
        cur = db.get_work_item(item["id"])
        assert cur["status"] == "processing" and cur["worker_id"] == "w2"    # untouched by w1
        # The live owner can complete it.
        assert db.complete_work(item["id"], "w2") is True
        assert db.get_work_item(item["id"])["status"] == "succeeded"
    finally:
        db.close()


def test_reclaim_never_exceeds_max_attempts(tmp_path):
    db = _db(tmp_path)
    try:
        job = _job(db)
        db.enqueue_work(job_id=job, kind="DELIVER_OP", idempotency_key=f"{job}:op1", max_attempts=2)
        for expected in ("retryable", "failed"):
            item = db.claim_next_work("w1", lease_seconds=1)
            assert item is not None
            _expire_lease(db, item["id"])
            assert db.reclaim_stale_work() == 1
            assert db.get_work_item(item["id"])["status"] == expected
        # attempt count equals max_attempts and nothing is claimable any more
        w = db.get_work_item(item["id"])
        assert w["attempt"] == 2 and w["max_attempts"] == 2 and w["last_error_category"] == "lease_expired_exhausted"
        assert db.claim_next_work("w1", lease_seconds=1) is None
    finally:
        db.close()


def test_fail_work_respects_budget_and_backoff(tmp_path):
    db = _db(tmp_path)
    try:
        job = _job(db)
        db.enqueue_work(job_id=job, kind="DELIVER_OP", idempotency_key=f"{job}:op", max_attempts=3)
        item = db.claim_next_work("w1", lease_seconds=60)
        assert db.fail_work(item["id"], "w1", category="HTTP_503", error="x", backoff_seconds=30) == "retryable"
        w = db.get_work_item(item["id"])
        assert w["available_at"] > datetime.now(timezone.utc).isoformat()   # backoff honoured
        assert db.claim_next_work("w1", lease_seconds=60) is None            # not due yet
    finally:
        db.close()


def test_defer_does_not_consume_an_attempt(tmp_path):
    db = _db(tmp_path)
    try:
        job = _job(db)
        db.enqueue_work(job_id=job, kind="TARGET_RECONCILE", idempotency_key=f"{job}:reconcile", max_attempts=5)
        item = db.claim_next_work("w1", lease_seconds=60)
        assert item["attempt"] == 1
        assert db.defer_work(item["id"], "w1", delay_seconds=0, reason="delivery in flight") is True
        w = db.get_work_item(item["id"])
        assert w["status"] == "pending" and w["attempt"] == 0 and w["worker_id"] is None
        assert db.defer_work(item["id"], "w1", delay_seconds=0, reason="again") is False   # no longer owned
        time.sleep(0.01)
        again = db.claim_next_work("w2", lease_seconds=60)
        assert again and again["id"] == item["id"] and again["attempt"] == 1
    finally:
        db.close()


def test_startup_force_reclaim_recovers_crashed_processing_items(tmp_path):
    db = _db(tmp_path)
    try:
        job = _job(db)
        db.enqueue_work(job_id=job, kind="DELIVER_OP", idempotency_key=f"{job}:op", max_attempts=5)
        item = db.claim_next_work("crashed-process#0", lease_seconds=3600)   # lease far in the future
        assert db.reclaim_stale_work() == 0                                   # periodic reaper: not expired
        assert db.reclaim_stale_work(force_all=True) == 1                     # startup: no other live process
        w = db.get_work_item(item["id"])
        assert w["status"] == "retryable" and w["worker_id"] is None
    finally:
        db.close()


def test_live_heartbeat_prevents_reclaim(tmp_path):
    db = _db(tmp_path)
    try:
        job = _job(db)
        db.enqueue_work(job_id=job, kind="MAP", idempotency_key=f"{job}:map", max_attempts=5)
        item = db.claim_next_work("w1", lease_seconds=1)
        _expire_lease(db, item["id"])
        db.heartbeat_work(item["id"], "w1", lease_seconds=60)                 # renewed before the reaper ran
        assert db.reclaim_stale_work() == 0
        assert db.work_owned_by(item["id"], "w1") is True
    finally:
        db.close()
