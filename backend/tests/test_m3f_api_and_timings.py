"""M3F §A + §I — migration-list summary fields on GET /api/jobs and per-stage compute timings.

Runs the real pipeline (fake adapter) end to end and checks that:
  * the job LIST exposes source filenames, an employee-record count, and the open-decision count so a
    migration card renders without per-job follow-up requests (no frontend N+1);
  * per-stage COMPUTE timings are persisted and surfaced, excluding human-review wait from compute.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def _poll(client, job_id, want, tries=150, delay=0.1):
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


def _drive_to_end(client, sample_files):
    files = [
        ("files", ("legacy_hr.csv", sample_files["legacy_hr.csv"], "text/csv")),
        ("files", ("employee_export.xlsx", sample_files["employee_export.xlsx"],
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
    ]
    job_id = client.post("/api/jobs", files=files).json()["id"]
    job = _poll(client, job_id, {"awaiting_review", "error"})
    assert job["status"] == "awaiting_review", job
    # resolve the single mapping review (Start -> contract_start_date) to let the pipeline continue.
    reviews = client.get(f"/api/jobs/{job_id}/reviews").json()
    for iss in reviews:
        client.post(f"/api/jobs/{job_id}/reviews/{iss['id']}/decision",
                    json={"version": iss["version"], "action": "correct",
                          "corrected_target": "contract_start_date", "reason": "t"})
    _poll(client, job_id, {"preparation_complete", "awaiting_record_review", "reconciliation_complete",
                           "migration_complete", "awaiting_target_review", "error"})
    return job_id


def test_job_list_has_migration_card_summary(client, sample_files):
    job_id = _drive_to_end(client, sample_files)
    jobs = client.get("/api/jobs").json()
    assert isinstance(jobs, list) and jobs
    row = next(j for j in jobs if j["id"] == job_id)
    # source filenames present on the list row (no follow-up /files request needed)
    assert set(row["source_filenames"]) == {"legacy_hr.csv", "employee_export.xlsx"}
    assert isinstance(row["row_count"], int) and row["row_count"] > 0
    assert "open_reviews" in row and isinstance(row["open_reviews"], int)


def test_job_metrics_report_stage_compute_timings(client, sample_files):
    job_id = _drive_to_end(client, sample_files)
    m = client.get(f"/api/jobs/{job_id}/metrics").json()
    assert "timings" in m
    t = m["timings"]
    for key in ("parse_stage", "mapping", "preparation", "reconcile", "delivery"):
        assert key in t["stages"], t["stages"]
    # parse + mapping actually ran, so their compute time is positive.
    assert t["stages"]["parse_stage"] > 0
    assert t["stages"]["mapping"] > 0
    assert t["total_compute_ms"] >= t["stages"]["mapping"]
    # human-review wait is a separate field, never folded into compute time.
    assert "human_review_wait_ms" in t and "total_compute_ms" in t
