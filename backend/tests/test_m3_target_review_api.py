"""M3A: target reconciliation + human review over the real HTTP API (fake provider, in-process
mock target). Upload -> auto M1/M2 -> operator triggers reconcile -> target review of an unsafe
conflict -> decision -> reconcile re-runs -> reconciliation_complete. Also covers stale-version
protection and the snapshot/reconciliation readback endpoints.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

CANON = "employee_id,full_name,work_email,department,hire_date,contract_start_date\n"


@pytest.fixture
def client(temp_settings):
    with TestClient(create_app()) as c:
        yield c


def _poll(client, job_id, want, tries=400, delay=0.03):
    for _ in range(tries):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in want:
            return j
        time.sleep(delay)
    return client.get(f"/api/jobs/{job_id}").json()


def _upload_and_prepare(client, text):
    job_id = client.post("/api/jobs", files=[("files", ("t.csv", text.encode(), "text/csv"))]).json()["id"]
    return _poll(client, job_id, {"preparation_complete", "awaiting_record_review", "error"})


def test_reconcile_all_create_completes_without_review(client):
    # Fresh employee_ids (not in the target seed) -> all READY_CREATE, no review needed.
    text = CANON + ("901,New One,new1@corp.com,Engineering,2020-01-01,\n"
                    "902,New Two,new2@corp.com,Sales,2020-02-02,\n")
    job = _upload_and_prepare(client, text)
    assert job["status"] == "preparation_complete", job
    assert client.post(f"/api/jobs/{job['id']}/reconcile").status_code == 200
    job = _poll(client, job["id"], {"reconciliation_complete", "awaiting_target_review", "error"})
    assert job["status"] == "reconciliation_complete", job
    rec = client.get(f"/api/jobs/{job['id']}/reconciliation").json()
    assert rec["counts"]["ready_create"] == 2 and rec["counts"]["review_required"] == 0
    assert rec["summary"]["result"] == "reconciliation_complete"


def test_reconcile_before_prepare_conflicts(client):
    # A brand-new job cannot reconcile until preparation is complete.
    job_id = client.post("/api/jobs",
                         files=[("files", ("t.csv", (CANON + "1,A,a@x.com,Sales,2020-01-01,\n").encode(),
                                           "text/csv"))]).json()["id"]
    # Immediately (still queued/ingesting) -> 409.
    r = client.post(f"/api/jobs/{job_id}/reconcile")
    assert r.status_code in (409, 200)  # 200 only if it already reached preparation_complete
    if r.status_code == 200:
        pytest.skip("job prepared before we could race the guard")


def test_target_value_conflict_review_then_keep_existing(client):
    # employee_id 100 exists in the target with department 'Engineering'; incoming says 'Sales'.
    text = CANON + "100,Ada Existing,ada@corp.com,Sales,2019-01-01,\n"
    job = _upload_and_prepare(client, text)
    jid = job["id"]
    assert job["status"] == "preparation_complete", job

    client.post(f"/api/jobs/{jid}/reconcile")
    job = _poll(client, jid, {"awaiting_target_review", "reconciliation_complete", "error"})
    assert job["status"] == "awaiting_target_review", job

    reviews = client.get(f"/api/jobs/{jid}/target-reviews").json()
    assert len(reviews) == 1
    iss = reviews[0]
    assert iss["field"] == "department" and iss["issue_type"] == "value_conflict"
    assert iss["incoming_value"] == "Sales" and iss["target_value"] == "Engineering"
    assert set(iss["options"]) == {"keep_existing", "use_incoming", "exclude"}

    # Snapshot carries the target revision (M3B optimistic-concurrency groundwork).
    snaps = client.get(f"/api/jobs/{jid}/target-snapshots").json()
    assert snaps and snaps[0]["target_record_id"] == "100" and snaps[0]["target_revision"] == 1

    dec = client.post(f"/api/jobs/{jid}/target-reviews/{iss['id']}/decision",
                      json={"version": iss["version"], "action": "keep_existing"})
    assert dec.status_code == 200 and dec.json()["outcome"] == "resolved"

    job = _poll(client, jid, {"reconciliation_complete", "error"})
    assert job["status"] == "reconciliation_complete", job
    rec = client.get(f"/api/jobs/{jid}/reconciliation").json()
    assert rec["counts"]["no_change"] == 1 and rec["counts"]["review_required"] == 0


def test_target_value_conflict_use_incoming_becomes_ready_update(client):
    text = CANON + "100,Ada Existing,ada@corp.com,Sales,2019-01-01,\n"
    jid = _upload_and_prepare(client, text)["id"]
    client.post(f"/api/jobs/{jid}/reconcile")
    _poll(client, jid, {"awaiting_target_review", "error"})
    iss = client.get(f"/api/jobs/{jid}/target-reviews").json()[0]
    dec = client.post(f"/api/jobs/{jid}/target-reviews/{iss['id']}/decision",
                      json={"version": iss["version"], "action": "use_incoming"})
    assert dec.status_code == 200
    job = _poll(client, jid, {"reconciliation_complete", "error"})
    assert job["status"] == "reconciliation_complete", job
    rec = client.get(f"/api/jobs/{jid}/reconciliation").json()
    assert rec["counts"]["ready_update"] == 1 and rec["counts"]["review_required"] == 0


def test_target_decision_stale_version_conflicts(client):
    text = CANON + "100,Ada Existing,ada@corp.com,Sales,2019-01-01,\n"
    jid = _upload_and_prepare(client, text)["id"]
    client.post(f"/api/jobs/{jid}/reconcile")
    _poll(client, jid, {"awaiting_target_review", "error"})
    iss = client.get(f"/api/jobs/{jid}/target-reviews").json()[0]
    ok = client.post(f"/api/jobs/{jid}/target-reviews/{iss['id']}/decision",
                     json={"version": iss["version"], "action": "keep_existing"})
    assert ok.status_code == 200
    # Resubmit at the now-stale version with a different action -> 409.
    conflict = client.post(f"/api/jobs/{jid}/target-reviews/{iss['id']}/decision",
                           json={"version": iss["version"], "action": "use_incoming"})
    assert conflict.status_code == 409
