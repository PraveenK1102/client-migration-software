"""M3I source-file removal + migration delete — HTTP API contract + full-pipeline rebuild (order §C/§D/§F).

Runs the REAL app (worker pool started via the FastAPI lifespan, fake adapter, in-process mock target)
through ``TestClient`` so a removal actually rebuilds mapping → preparation → reconciliation from the
remaining source set, exactly as a consultant would drive it. AUTO_CONTINUE is OFF so the migration
stops PRE-SYNC (never auto-delivers), which is the only window where source-set mutation is allowed;
reconciliation is driven explicitly. Canonical headers keep mapping deterministic (rules resolve them).
"""
from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient

HEADERS = "employee_id,full_name,work_email,hire_date\n"
PREP = {"preparation_complete", "awaiting_record_review"}
RECON = {"reconciliation_complete", "awaiting_target_review"}


def _csv(rows: list[list[str]]) -> bytes:
    return (HEADERS + "\n".join(",".join(r) for r in rows)).encode()


def _files(*specs):
    return [("files", (name, _csv(rows), "text/csv")) for name, rows in specs]


def _ready(c, jid, want, *, tries=1000, delay=0.03):
    """Wait until the job reaches a target status AND no work item is still in flight. A mutation
    issued while the worker still owns the just-finished stage item is correctly refused with 409
    ('wait for the current step to finish'); a consultant simply retries. Tests quiesce first."""
    for _ in range(tries):
        r = c.get(f"/api/jobs/{jid}")
        if r.status_code == 200 and r.json()["status"] in want:
            items = c.get(f"/api/jobs/{jid}/work-items").json()
            if not any(w["status"] in ("pending", "processing", "retryable") for w in items):
                return r.json()
        time.sleep(delay)
    return c.get(f"/api/jobs/{jid}").json()


def _reconcile(c, jid):
    """Drive reconciliation explicitly (AUTO_CONTINUE is off) and wait for it to settle pre-sync."""
    assert c.post(f"/api/jobs/{jid}/reconcile").status_code in (200, 202), "reconcile trigger failed"
    return _ready(c, jid, RECON)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AUTO_CONTINUE", "false")      # stop pre-sync; never auto-deliver to the target
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    from app import config
    config.get_settings.cache_clear()
    config.get_settings().ensure_dirs()
    from app.main import create_app
    with TestClient(create_app()) as c:
        yield c
    config.get_settings.cache_clear()


def _file_ids(c, jid):
    return {f["original_filename"]: f["id"] for f in c.get(f"/api/jobs/{jid}/files").json()}


def _blob_keys(ctx, jid):
    return [ctx.db.get_source_file(f["id"])["blob_key"] for f in ctx.db.get_source_files(jid)]


# =========================================================================== §C: bulk file removal + rebuild
def test_bulk_remove_two_of_three_rebuilds_from_remaining(client):
    c = client
    ctx = c.app.state.ctx
    r = c.post("/api/jobs", files=_files(
        ("a.csv", [["E1", "A One", "e1@x.com", "2020-01-01"]]),
        ("b.csv", [["E2", "B Two", "e2@x.com", "2020-02-02"]]),
        ("c.csv", [["E3", "C Three", "e3@x.com", "2020-03-03"]])))
    jid = r.json()["id"]
    assert _ready(c, jid, PREP)["status"] in PREP
    _reconcile(c, jid)                                            # reconciliation rows now exist
    assert {x["business_key"] for x in c.get(f"/api/jobs/{jid}/candidates").json()} == {"E1", "E2", "E3"}
    assert len(c.get(f"/api/jobs/{jid}/reconciliation").json()["results"]) == 3

    ids = _file_ids(c, jid)
    blob_a = ctx.db.get_source_file(ids["a.csv"])["blob_key"]
    blob_c = ctx.db.get_source_file(ids["c.csv"])["blob_key"]
    rr = c.post(f"/api/jobs/{jid}/files/remove",
                json={"file_ids": [ids["a.csv"], ids["b.csv"]], "note": "uploaded the wrong exports"})
    assert rr.status_code == 200, rr.text
    body = rr.json()
    assert body["remaining_files"] == 1 and body["rebuild_enqueued"] and body["blobs_deleted"] == 2

    # (4) rebuild recomputes from the remaining file; (5) candidates from removed files disappear.
    assert _ready(c, jid, PREP)["status"] in PREP
    assert {x["business_key"] for x in c.get(f"/api/jobs/{jid}/candidates").json()} == {"E3"}
    assert [f["original_filename"] for f in c.get(f"/api/jobs/{jid}/files").json()] == ["c.csv"]
    # (8) stale reconciliation rows from the pre-removal run are gone; a fresh reconcile has just one.
    assert c.get(f"/api/jobs/{jid}/reconciliation").json()["results"] == []
    _reconcile(c, jid)
    assert len(c.get(f"/api/jobs/{jid}/reconciliation").json()["results"]) == 1
    # (13) removed blobs gone, remaining blob kept.
    assert not ctx.blobstore.exists(blob_a) and ctx.blobstore.exists(blob_c)
    # (15) audit records the removal.
    assert any(a["event_type"] == "source_files_removed" for a in c.get(f"/api/jobs/{jid}/audit").json())


def test_remove_clears_cross_file_conflict_and_stale_review(client):
    """Two files share a work email -> a shared-email human review. Removing one file makes the conflict
    disappear on rebuild: no stale review row survives, and preparation completes cleanly."""
    c = client
    r = c.post("/api/jobs", files=_files(
        ("a.csv", [["E1", "A One", "dup@x.com", "2020-01-01"]]),
        ("b.csv", [["E2", "B Two", "dup@x.com", "2020-02-02"]]),
        ("c.csv", [["E5", "C Five", "e5@x.com", "2020-03-03"]])))
    jid = r.json()["id"]
    j = _ready(c, jid, {"awaiting_record_review"} | PREP)
    reviews = c.get(f"/api/jobs/{jid}/record-reviews").json()
    shared = [i for i in reviews if i["issue_type"] == "shared_email"]
    assert shared, f"expected a shared-email review; status={j['status']} reviews={[i['issue_type'] for i in reviews]}"

    ids = _file_ids(c, jid)
    rr = c.post(f"/api/jobs/{jid}/files/remove", json={"file_ids": [ids["a.csv"]]})
    assert rr.status_code == 200, rr.text

    assert _ready(c, jid, PREP)["status"] == "preparation_complete"   # conflict gone -> settles clean
    assert c.get(f"/api/jobs/{jid}/record-reviews").json() == []      # no stale review survives
    assert {x["business_key"] for x in c.get(f"/api/jobs/{jid}/candidates").json()} == {"E2", "E5"}


def test_last_file_cannot_be_removed(client):
    c = client
    r = c.post("/api/jobs", files=_files(("only.csv", [["E1", "A", "a@x.com", "2020-01-01"]])))
    jid = r.json()["id"]
    _ready(c, jid, PREP)
    ids = _file_ids(c, jid)
    rr = c.post(f"/api/jobs/{jid}/files/remove", json={"file_ids": [ids["only.csv"]]})
    assert rr.status_code == 400 and "at least one source file" in rr.json()["detail"]


def test_remove_file_from_another_job_is_rejected(client):
    c = client
    r1 = c.post("/api/jobs", files=_files(("a.csv", [["E1", "A", "a@x.com", "2020-01-01"]]),
                                          ("b.csv", [["E2", "B", "b@x.com", "2020-02-02"]])))
    r2 = c.post("/api/jobs", files=_files(("x.csv", [["E9", "X", "x@x.com", "2020-01-01"]])))
    j1, j2 = r1.json()["id"], r2.json()["id"]
    _ready(c, j1, PREP)
    _ready(c, j2, PREP)
    foreign = list(_file_ids(c, j2).values())[0]
    rr = c.post(f"/api/jobs/{j1}/files/remove", json={"file_ids": [foreign]})
    assert rr.status_code == 404 and "not part of this migration" in rr.json()["detail"]


def test_remove_blocked_after_target_delivery_activity(client):
    """A delivery attempt makes the source set unremovable (it could hide a target side effect)."""
    c = client
    ctx = c.app.state.ctx
    job = ctx.db.create_job(schema_version="employee.v2", provider="fake", model_id="m", adapter_kind="fake")
    for fid in ("f1", "f2"):
        ctx.db.add_source_file(job, file_id=fid, original_filename=f"{fid}.csv", stored_name=f"{fid}.csv",
                               content_type="text/csv", size_bytes=10, blob_key=None)
    ctx.db.create_delivery_operation(job_id=job, candidate_id="c1", employee_id="E1", op_type="CREATE",
                                     payload={}, expected_target_revision=None, target_record_id=None,
                                     before_snapshot=None, desired_version_id=None,
                                     idempotency_key=f"deliver:{job}:c1")
    op = ctx.db.get_delivery_operations(job)[0]["id"]
    ctx.db.transition_delivery_op(op, "PLANNED", "SUCCEEDED")
    rr = c.post(f"/api/jobs/{job}/files/remove", json={"file_ids": ["f1"]})
    assert rr.status_code == 409 and "already started writing to the target" in rr.json()["detail"]
    assert len(ctx.db.get_source_files(job)) == 2               # nothing mutated


# =========================================================================== §D: migration delete
def test_delete_unsynced_migration_removes_it_and_blobs(client):
    c = client
    ctx = c.app.state.ctx
    r = c.post("/api/jobs", files=_files(("a.csv", [["E1", "A", "a@x.com", "2020-01-01"]]),
                                         ("b.csv", [["E2", "B", "b@x.com", "2020-02-02"]])))
    jid = r.json()["id"]
    _reconcile(c, _ready(c, jid, PREP)["id"])                    # reach a real pre-sync settled state
    keys = _blob_keys(ctx, jid)
    d = c.delete(f"/api/jobs/{jid}")
    assert d.status_code == 200 and d.json()["deleted"] and d.json()["blobs_deleted"] == 2
    assert jid not in {j["id"] for j in c.get("/api/jobs").json()}
    assert c.get(f"/api/jobs/{jid}").status_code == 404
    for k in keys:
        assert not ctx.blobstore.exists(k)


def test_delete_blocked_while_target_writes_unreverted(client):
    c = client
    ctx = c.app.state.ctx
    job = ctx.db.create_job(schema_version="employee.v2", provider="fake", model_id="m", adapter_kind="fake")
    ctx.db.add_source_file(job, file_id="f1", original_filename="f1.csv", stored_name="f1.csv",
                           content_type="text/csv", size_bytes=10, blob_key=None)
    ctx.db.create_delivery_operation(job_id=job, candidate_id="c1", employee_id="E1", op_type="CREATE",
                                     payload={}, expected_target_revision=None, target_record_id=None,
                                     before_snapshot=None, desired_version_id=None,
                                     idempotency_key=f"deliver:{job}:c1")
    op = ctx.db.get_delivery_operations(job)[0]["id"]
    ctx.db.transition_delivery_op(op, "PLANNED", "SUCCEEDED")
    d = c.delete(f"/api/jobs/{job}")
    assert d.status_code == 409 and "Roll back those changes" in d.json()["detail"]
    assert not ctx.db.job_is_deleted(job)
    ctx.db.update_delivery_operation(op, status="ROLLED_BACK")   # fully rolled back -> deletion allowed
    assert c.delete(f"/api/jobs/{job}").status_code == 200


def test_delete_does_not_affect_other_migration(client):
    c = client
    keep = c.post("/api/jobs", files=_files(("k.csv", [["E9", "Keep", "k@x.com", "2020-01-01"]]))).json()["id"]
    victim = c.post("/api/jobs", files=_files(("v.csv", [["E1", "V", "v@x.com", "2020-01-01"]]))).json()["id"]
    _ready(c, keep, PREP)
    _ready(c, victim, PREP)
    assert c.delete(f"/api/jobs/{victim}").status_code == 200
    listed = {j["id"] for j in c.get("/api/jobs").json()}
    assert keep in listed and victim not in listed
    assert c.get(f"/api/jobs/{keep}").json()["status"] in PREP


def test_worker_cannot_resurrect_deleted_job(client):
    """A stale work item enqueued after a migration is deleted is dropped (cancelled) by the worker
    guard — the deleted migration is never resurrected and no stage runs against it."""
    c = client
    ctx = c.app.state.ctx
    jid = c.post("/api/jobs", files=_files(("a.csv", [["E1", "A", "a@x.com", "2020-01-01"]]))).json()["id"]
    _ready(c, jid, PREP)
    assert c.delete(f"/api/jobs/{jid}").status_code == 200

    key = f"{jid}:stale:{uuid.uuid4().hex[:8]}"
    ctx.db.enqueue_work(job_id=jid, kind="MAP", idempotency_key=key)   # simulate a stray re-enqueue
    dropped = None
    for _ in range(400):
        w = next((x for x in ctx.db.get_work_items(jid) if x["idempotency_key"] == key), None)
        if w and w["status"] in ("cancelled", "succeeded", "failed"):
            dropped = w
            break
        time.sleep(0.03)
    assert dropped is not None and dropped["status"] == "cancelled"
    assert ctx.db.job_is_deleted(jid)
    assert c.get(f"/api/jobs/{jid}").status_code == 404          # still gone, never resurrected
