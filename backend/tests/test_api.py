"""API-level tests through the real FastAPI app (fake provider, no network)."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def _poll(client, job_id, want, tries=100, delay=0.1):
    for _ in range(tries):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in want:
            return j
        time.sleep(delay)
    return client.get(f"/api/jobs/{job_id}").json()


@pytest.fixture
def client(temp_settings):
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_health_and_schema(client):
    h = client.get("/api/health").json()
    assert h["adapter_kind"] == "fake" and h["adapter_available"] is True
    assert h["deterministic_available"] is True and h["configured"] is True
    assert "env_file" in h and h["env_file_exists"] in (True, False)
    s = client.get("/api/schema").json()
    assert s["version"] == "employee.v2"
    # The six legacy (v1) fields remain part of the contract, with the same requiredness.
    legacy = {"employee_id", "full_name", "work_email", "department", "hire_date", "contract_start_date"}
    names = {f["name"] for f in s["fields"]}
    assert legacy <= names and len(names) >= 20
    assert {f["name"] for f in s["fields"] if f["required"]} == {"employee_id", "full_name", "work_email", "hire_date"}
    assert {c["key"] for c in s["collections"]} >= {"addresses", "emergency_contacts", "dependents",
                                                     "education_history", "vehicles"}
    assert "vehicles[].registration_number" in s["target_paths"]


def test_unsupported_upload_rejected(client):
    r = client.post("/api/jobs", files=[("files", ("cv.pdf", b"%PDF-1.4", "application/pdf"))])
    assert r.status_code == 400
    assert "CSV and XLSX" in r.json()["detail"]


def test_missing_job_is_404(client):
    assert client.get("/api/jobs/nope").status_code == 404


def test_full_flow_upload_review_correct_resume(client, sample_files):
    files = [
        ("files", ("legacy_hr.csv", sample_files["legacy_hr.csv"], "text/csv")),
        ("files", ("employee_export.xlsx", sample_files["employee_export.xlsx"],
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
    ]
    r = client.post("/api/jobs", files=files)
    assert r.status_code == 200
    job = r.json()
    assert job["adapter_kind"] == "fake"
    job_id = job["id"]

    job = _poll(client, job_id, {"awaiting_review", "error"})
    assert job["status"] == "awaiting_review", job

    # Autonomous mappings visible.
    mappings = client.get(f"/api/jobs/{job_id}/mappings").json()
    assert len(mappings["accepted"]) >= 10

    reviews = client.get(f"/api/jobs/{job_id}/reviews").json()
    assert len(reviews) == 1
    issue = reviews[0]
    assert issue["source_header"] == "Start"
    assert set(issue["candidate_target_fields"]) == {"hire_date", "contract_start_date"}
    assert issue["affected_non_empty_rows"] > 0

    # Correct it -> resume the same job.
    dec = client.post(f"/api/jobs/{job_id}/reviews/{issue['id']}/decision",
                      json={"version": issue["version"], "action": "correct",
                            "corrected_target": "contract_start_date", "reason": "contract export"})
    assert dec.status_code == 200
    assert dec.json()["outcome"] == "resolved"

    # Mapping completes, then fresh jobs auto-continue into M2 preparation.
    job = _poll(client, job_id, {"mapping_complete", "preparing_records",
                                 "awaiting_record_review", "preparation_complete", "error"})
    assert job["status"] in ("mapping_complete", "preparing_records",
                             "awaiting_record_review", "preparation_complete")
    assert job["summary"]["result"] == "mapping_complete"  # mapping summary persisted

    mappings = client.get(f"/api/jobs/{job_id}/mappings").json()
    start = [m for m in mappings["accepted"] if m["source_header"] == "Start"]
    assert start and start[0]["target_field"] == "contract_start_date"
    assert start[0]["actor"] == "human"

    audit = client.get(f"/api/jobs/{job_id}/audit").json()
    kinds = {a["event_type"] for a in audit}
    assert {"ingested", "profiled", "mapping_rule_accepted", "proposed", "issue_created",
            "issue_resolved", "mapping_complete"} <= kinds


def test_stale_decision_conflicts(client, sample_files):
    files = [
        ("files", ("legacy_hr.csv", sample_files["legacy_hr.csv"], "text/csv")),
        ("files", ("employee_export.xlsx", sample_files["employee_export.xlsx"],
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
    ]
    job_id = client.post("/api/jobs", files=files).json()["id"]
    job = _poll(client, job_id, {"awaiting_review", "error"})
    assert job["status"] == "awaiting_review"
    issue = client.get(f"/api/jobs/{job_id}/reviews").json()[0]

    ok = client.post(f"/api/jobs/{job_id}/reviews/{issue['id']}/decision",
                     json={"version": issue["version"], "action": "approve"})
    assert ok.status_code == 200
    # Resubmit at the stale version with a different action -> 409.
    conflict = client.post(f"/api/jobs/{job_id}/reviews/{issue['id']}/decision",
                           json={"version": issue["version"], "action": "correct",
                                 "corrected_target": "hire_date"})
    assert conflict.status_code == 409
