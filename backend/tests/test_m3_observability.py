"""M3A.1: manual-QA observability endpoints (files, work-items, staged-row preview, lineage,
audit categories) exercised over the real HTTP API with the supplied manual-demo fixtures.

These fixtures use canonical/alias headers, so the whole flow runs with ZERO model calls even
under the fake provider (rules resolve every column first).
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

DEMO = Path(__file__).resolve().parent.parent.parent / "sample-data" / "manual-demo"
CSV = "01_employee_master.csv"
XLSX = "02_employee_updates.xlsx"


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


def _upload_demo(client) -> str:
    files = [
        ("files", (CSV, (DEMO / CSV).read_bytes(), "text/csv")),
        ("files", (XLSX, (DEMO / XLSX).read_bytes(),
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
    ]
    return client.post("/api/jobs", files=files).json()["id"]


def test_files_and_workitems_endpoints(client):
    jid = _upload_demo(client)
    _poll(client, jid, {"awaiting_record_review", "error"})
    files = client.get(f"/api/jobs/{jid}/files").json()
    assert {f["original_filename"] for f in files} == {CSV, XLSX}
    for f in files:
        assert f["parse_status"] == "parsed"
        assert f["sha256"] and f["size_bytes"] > 0
    work = client.get(f"/api/jobs/{jid}/work-items").json()
    ingest = [w for w in work if w["kind"] == "INGEST_FILE"]
    assert len(ingest) == 2 and all(w["status"] == "succeeded" for w in ingest)
    assert all("attempt" in w and w["max_attempts"] >= 1 for w in work)


def test_three_logical_tables_and_thirteen_rows_zero_model(client):
    jid = _upload_demo(client)
    job = _poll(client, jid, {"awaiting_record_review", "error"})
    tables = client.get(f"/api/jobs/{jid}/profiles").json()["tables"]
    names = sorted((t["original_filename"], t["sheet_name"]) for t in tables)
    assert names == [(CSV, None), (XLSX, "Employee Updates"), (XLSX, "Historical Records")]
    assert sum(t["n_rows"] for t in tables) == 13
    # deterministic aliases -> no model proposal requests were needed
    assert job["summary"]["routing_metrics"]["model_proposal_requests"] == 0
    assert job["counts"]["rule_accepted"] == 18 and job["counts"]["model_accepted"] == 0


def test_staged_row_preview_bounds_and_content(client):
    jid = _upload_demo(client)
    _poll(client, jid, {"awaiting_record_review", "error"})
    tables = {(t["original_filename"], t["sheet_name"]): t["table_id"]
              for t in client.get(f"/api/jobs/{jid}/profiles").json()["tables"]}
    csv_tid = tables[(CSV, None)]
    page = client.get(f"/api/jobs/{jid}/tables/{csv_tid}/rows?offset=0&limit=3").json()
    assert page["total"] == 7 and len(page["rows"]) == 3 and page["limit"] == 3
    r0 = {c["header"]: c["value"] for c in page["rows"][0]["cells"]}
    assert r0["Employee Number"] == "100" and r0["Work Email Address"] == "ada@corp.com"
    # pagination offset
    page2 = client.get(f"/api/jobs/{jid}/tables/{csv_tid}/rows?offset=6&limit=50").json()
    assert len(page2["rows"]) == 1 and page2["rows"][0]["row_number"] >= 7


def test_staged_row_preview_caps_limit(client):
    jid = _upload_demo(client)
    _poll(client, jid, {"awaiting_record_review", "error"})
    tid = client.get(f"/api/jobs/{jid}/profiles").json()["tables"][0]["table_id"]
    page = client.get(f"/api/jobs/{jid}/tables/{tid}/rows?limit=100000").json()
    assert page["limit"] == 100                       # hard server-side cap


def test_staged_row_preview_rejects_table_from_another_job(client):
    jid_a = _upload_demo(client)
    _poll(client, jid_a, {"awaiting_record_review", "error"})
    tid_a = client.get(f"/api/jobs/{jid_a}/profiles").json()["tables"][0]["table_id"]
    jid_b = _upload_demo(client)
    _poll(client, jid_b, {"awaiting_record_review", "error"})
    # Table from job A must not be readable under job B.
    assert client.get(f"/api/jobs/{jid_b}/tables/{tid_a}/rows").status_code == 404


def test_prepared_candidate_lineage_has_real_provenance(client):
    jid = _upload_demo(client)
    _poll(client, jid, {"awaiting_record_review", "error"})
    # resolve the two demo reviews so 200 (merged) becomes eligible
    for r in client.get(f"/api/jobs/{jid}/record-reviews").json():
        body = {"version": r["version"]}
        if r["issue_type"] == "ambiguous_date":
            body.update(action="correct", value="2024-04-03")
        else:
            body.update(action="select", value="Finance")
        client.post(f"/api/jobs/{jid}/record-reviews/{r['id']}/decision", json=body)
    _poll(client, jid, {"preparation_complete", "error"})
    cands = {c["business_key"]: c for c in client.get(f"/api/jobs/{jid}/candidates").json()}
    # employee 200 merged from the CSV (dept blank) + the XLSX 'Employee Updates' sheet (Engineering)
    c200 = cands["200"]
    dept = c200["record"]["department"]
    assert dept["value"] == "Engineering"
    provs = dept["provenance"]
    files_seen = {p["original_filename"] for p in provs}
    assert XLSX in files_seen                                     # value came from the XLSX
    for p in provs:                                               # provenance carries real refs
        assert "row_number" in p and "header" in p and "original_filename" in p
    # 200 contributed from both files -> multiple source refs
    assert len({(s["original_filename"], s.get("sheet_name")) for s in c200["source_refs"]}) >= 2


def test_audit_categories_present_for_filtering(client):
    jid = _upload_demo(client)
    _poll(client, jid, {"awaiting_record_review", "error"})
    for r in client.get(f"/api/jobs/{jid}/record-reviews").json():
        body = {"version": r["version"]}
        if r["issue_type"] == "ambiguous_date":
            body.update(action="correct", value="2024-04-03")
        else:
            body.update(action="select", value="Finance")
        client.post(f"/api/jobs/{jid}/record-reviews/{r['id']}/decision", json=body)
    _poll(client, jid, {"preparation_complete", "error"})
    cats = {a["category"] for a in client.get(f"/api/jobs/{jid}/audit").json()}
    assert {"ingestion", "mapping", "preparation", "human"} <= cats
    # human decisions are surfaced as their own audit events with a before/after summary
    human = [a for a in client.get(f"/api/jobs/{jid}/audit").json() if a["category"] == "human"]
    assert any(a["event_type"] == "record_decision" and a["after"].get("value") == "Finance"
               for a in human)
