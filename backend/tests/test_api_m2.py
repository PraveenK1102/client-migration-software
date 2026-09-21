"""HTTP-level M2 end-to-end (fake provider; canonical headers -> zero model calls).

Upload -> auto mapping (rules) -> auto preparation -> record review over HTTP ->
resume -> preparation_complete -> prepared-dataset readback.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

REPO = __import__("pathlib").Path(__file__).resolve().parent.parent.parent
SAMPLE = REPO / "sample-data"


@pytest.fixture
def client(temp_settings):
    with TestClient(create_app()) as c:
        yield c


def _poll(client, job_id, want, tries=200, delay=0.05):
    for _ in range(tries):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in want:
            return j
        time.sleep(delay)
    return client.get(f"/api/jobs/{job_id}").json()


def _upload(client, name):
    data = (SAMPLE / name).read_bytes()
    return client.post("/api/jobs", files=[("files", (name, data, "text/csv"))]).json()


def test_prepared_dataset_end_to_end(client):
    job = _upload(client, "canonical_messy.csv")
    job_id = job["id"]
    job = _poll(client, job_id, {"awaiting_record_review", "preparation_complete", "error"})
    assert job["status"] == "awaiting_record_review", job

    # Mapping used rules only (no model proposal requests).
    assert job["summary"]["routing_metrics"]["model_proposal_requests"] == 0
    assert job["counts"]["rule_accepted"] >= 6

    issues = client.get(f"/api/jobs/{job_id}/record-reviews").json()
    # M3C collapses ambiguous dates / unknown enums into ONE column-scoped review each (and
    # auto-resolves the date column here), so the messy file surfaces fewer, higher-level issues.
    assert len(issues) >= 5
    for iss in issues:
        t, opts = iss["issue_type"], iss["options"]
        body = {"version": iss["version"], "reason": "test"}
        if t == "ambiguous_date":                           # M3C: one column-scoped convention decision
            body.update(action="confirm_convention", convention=opts[0]["meaning"])
        elif t == "invalid_value":
            body.update(action="correct", value="2020-01-01")
        elif t == "missing_required":
            body.update(action="correct", value="gina.http@acme.com")
        elif t == "unknown_enum":                           # M3C: one column-scoped value-map decision
            _um = [d["value"] for d in iss["affected"].get("distinct_values", [])]
            body.update(action="map_values", value_map={v: opts[0] for v in _um})
        elif t == "value_conflict":
            body.update(action="select", value=opts[0])
        elif t == "shared_email":
            victim = iss["affected"]["candidates"][1]["candidate_id"]
            body.update(action="exclude", candidate_id=victim)
        else:
            body.update(action="exclude")
        r = client.post(f"/api/jobs/{job_id}/record-reviews/{iss['id']}/decision", json=body)
        assert r.status_code == 200, r.text

    # Wait for the background resume to recompute and finalize (exclude the transient
    # awaiting_record_review we are leaving).
    job = _poll(client, job_id, {"preparation_complete", "error"}, tries=300)
    assert job["status"] == "preparation_complete", job

    ds = client.get(f"/api/jobs/{job_id}/prepared-dataset").json()
    assert ds["ready_for_target"] >= 1
    for emp in ds["employees"]:
        assert emp["employee_id"] and emp["full_name"] and emp["work_email"] and emp["hire_date"]
    # Excluded candidates are reported separately, never in the eligible payload.
    keys_in_payload = {e["employee_id"] for e in ds["employees"]}
    assert "015" not in keys_in_payload  # 015 was excluded as the shared-email duplicate


def test_invalid_human_correction_is_rejected(client):
    job = _upload(client, "canonical_messy.csv")
    job_id = _poll(client, job["id"], {"awaiting_record_review", "error"})["id"]
    issues = client.get(f"/api/jobs/{job_id}/record-reviews").json()
    date_issue = next(i for i in issues if i["issue_type"] in ("ambiguous_date", "invalid_value")
                      and i["field"] == "hire_date")
    # An impossible correction must not close the issue.
    r = client.post(f"/api/jobs/{job_id}/record-reviews/{date_issue['id']}/decision",
                    json={"version": date_issue["version"], "action": "correct", "value": "2024-02-31"})
    assert r.status_code == 400
    assert ctx_still_open(client, job_id, date_issue["id"])


def ctx_still_open(client, job_id, issue_id) -> bool:
    return any(i["id"] == issue_id and i["status"] == "open"
               for i in client.get(f"/api/jobs/{job_id}/record-reviews").json())


def test_retry_mapping_conflicts_when_not_blocked(client):
    job = _upload(client, "canonical_clean.csv")   # rules-only -> never blocked
    job_id = _poll(client, job["id"], {"preparation_complete", "mapping_complete",
                                       "awaiting_record_review"})["id"]
    r = client.post(f"/api/jobs/{job_id}/retry-mapping")
    assert r.status_code == 409


def test_prepare_endpoint_is_idempotent(client):
    job = _upload(client, "canonical_clean.csv")
    job_id = _poll(client, job["id"], {"preparation_complete"})["id"]
    n1 = len(client.get(f"/api/jobs/{job_id}/candidates").json())
    r = client.post(f"/api/jobs/{job_id}/prepare")
    assert r.status_code == 200
    _poll(client, job_id, {"preparation_complete"})
    n2 = len(client.get(f"/api/jobs/{job_id}/candidates").json())
    assert n1 == n2   # re-preparation replaces, does not duplicate
