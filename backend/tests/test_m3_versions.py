"""M3A.1: immutable, comparable employee record versions.

Versions are append-only, deterministic/idempotent (dedup by snapshot hash), and distinct from
the audit stream. Existing-target employees get a target-baseline v1 (with revision); a safe/
approved change appends v2; new employees start at v1 (no fake v0); NO_CHANGE / excluded / an
unchanged recompute create no (new) version. All driven over HTTP against the manual fixtures.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

DEMO = Path(__file__).resolve().parent.parent.parent / "sample-data" / "manual-demo"
CSV, XLSX = "01_employee_master.csv", "02_employee_updates.xlsx"
CANON = "employee_id,full_name,work_email,department,hire_date,contract_start_date\n"


@pytest.fixture
def client(temp_settings):
    with TestClient(create_app()) as c:
        yield c


def _poll(c, jid, want, tries=400, delay=0.03):
    for _ in range(tries):
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] in want:
            return j
        time.sleep(delay)
    return c.get(f"/api/jobs/{jid}").json()


def _resolve_record_reviews(c, jid):
    for r in c.get(f"/api/jobs/{jid}/record-reviews").json():
        b = {"version": r["version"], "note": "demo"}
        if r["issue_type"] == "ambiguous_date":
            # M3C: one column-scoped convention decision. DMY reads 03/04/2024 as 2024-04-03.
            b.update(action="confirm_convention", convention="DMY")
        elif r["issue_type"] == "value_conflict":
            b.update(action="select", value="Finance")
        else:
            b.update(action="exclude")
        c.post(f"/api/jobs/{jid}/record-reviews/{r['id']}/decision", json=b)


def _run_manual_demo(c) -> tuple[str, dict]:
    files = [("files", (CSV, (DEMO / CSV).read_bytes(), "text/csv")),
             ("files", (XLSX, (DEMO / XLSX).read_bytes(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))]
    jid = c.post("/api/jobs", files=files).json()["id"]
    _poll(c, jid, {"awaiting_record_review", "error"})
    _resolve_record_reviews(c, jid)
    _poll(c, jid, {"preparation_complete", "error"})
    c.post(f"/api/jobs/{jid}/reconcile")
    _poll(c, jid, {"awaiting_target_review", "reconciliation_complete", "error"})
    for t in c.get(f"/api/jobs/{jid}/target-reviews").json():
        c.post(f"/api/jobs/{jid}/target-reviews/{t['id']}/decision",
               json={"version": t["version"], "action": "exclude", "note": "identity mismatch"})
    _poll(c, jid, {"reconciliation_complete", "error"})
    cands = {x["business_key"]: x for x in c.get(f"/api/jobs/{jid}/candidates").json()}
    return jid, cands


def _versions(c, jid, cid):
    return c.get(f"/api/jobs/{jid}/candidates/{cid}/versions").json()


def test_existing_target_baseline_and_safe_update(client):
    jid, cands = _run_manual_demo(client)
    # 200 exists in the target (department blank); incoming supplies Engineering -> READY_UPDATE.
    v = _versions(client, jid, cands["200"]["id"])          # newest first
    assert [x["version_no"] for x in v] == [2, 1]
    v1 = next(x for x in v if x["version_no"] == 1)
    v2 = next(x for x in v if x["version_no"] == 2)
    assert v1["origin"] == "existing_target" and v1["snapshot"]["department"] in (None, "")
    assert v1["target_revision"] == 1                       # baseline carries the target revision
    assert v2["origin"] == "migration" and v2["snapshot"]["department"] == "Engineering"
    assert v2["is_current"] is True and v1["is_current"] is False
    assert v2["parent_version_id"] == v1["id"]              # append-only chain
    # v1 remains the confirmed baseline, unchanged.
    assert v1["snapshot"]["department"] in (None, "")


def test_no_change_employee_has_single_baseline_version(client):
    jid, cands = _run_manual_demo(client)
    for bk in ("100", "300"):                               # identical to target -> baseline only
        v = _versions(client, jid, cands[bk]["id"])
        assert [x["version_no"] for x in v] == [1]
        assert v[0]["origin"] == "existing_target" and v[0]["is_current"] is True


def test_new_employee_starts_at_v1(client):
    jid, cands = _run_manual_demo(client)
    v = _versions(client, jid, cands["800"]["id"])          # 800 not in target seed
    assert [x["version_no"] for x in v] == [1]
    assert v[0]["origin"] == "migration" and v[0]["parent_version_id"] is None  # no fake v0


def test_excluded_employee_has_no_versions(client):
    jid, cands = _run_manual_demo(client)
    assert _versions(client, jid, cands["400"]["id"]) == []


def test_version_compare_reports_only_changed_fields(client):
    jid, cands = _run_manual_demo(client)
    cid = cands["200"]["id"]
    cmp = client.get(f"/api/jobs/{jid}/candidates/{cid}/versions/compare?a=1&b=2").json()
    assert cmp["changed_fields"] == ["department"]
    dept = next(f for f in cmp["fields"] if f["field"] == "department")
    assert dept["changed"] is True and dept["kind"] == "added" and dept["b"] == "Engineering"
    emp = next(f for f in cmp["fields"] if f["field"] == "employee_id")
    assert emp["changed"] is False


def test_reconcile_is_idempotent_no_duplicate_versions(client):
    jid, cands = _run_manual_demo(client)
    cid = cands["200"]["id"]
    before = len(_versions(client, jid, cid))
    client.post(f"/api/jobs/{jid}/reconcile")               # re-run: unchanged effective records
    _poll(client, jid, {"reconciliation_complete"})
    assert len(_versions(client, jid, cid)) == before       # no new version created


def test_audit_links_version_transition(client):
    jid, cands = _run_manual_demo(client)
    ev = [a for a in client.get(f"/api/jobs/{jid}/audit").json() if a["event_type"] == "employee_version"]
    assert ev, "expected employee_version audit events"
    changes = [a for a in ev if isinstance(a["after"], dict) and a["after"].get("from_version")]
    assert any(a["after"].get("from_version") == 1 and a["after"].get("to_version") == 2
               and "department" in (a["after"].get("changed_fields") or []) for a in changes)


def test_cross_candidate_version_access_rejected(client):
    jid_a, cands_a = _run_manual_demo(client)
    # a candidate id that does not belong to job A
    files = [("files", ("t.csv", (CANON + "111,Zed,zed@x.com,Sales,2020-01-01,\n").encode(), "text/csv"))]
    jid_b = client.post("/api/jobs", files=files).json()["id"]
    _poll(client, jid_b, {"preparation_complete", "awaiting_record_review", "error"})
    other_cid = cands_a["100"]["id"]
    assert client.get(f"/api/jobs/{jid_b}/candidates/{other_cid}/versions").status_code == 404


def test_human_use_incoming_creates_human_version_with_note(client):
    # Existing target employee 100 (dept Engineering); incoming says Finance -> target value conflict.
    text = CANON + "100,Ada Existing,ada@corp.com,Finance,2019-01-01,\n"
    jid = client.post("/api/jobs", files=[("files", ("t.csv", text.encode(), "text/csv"))]).json()["id"]
    _poll(client, jid, {"preparation_complete", "error"})
    client.post(f"/api/jobs/{jid}/reconcile")
    _poll(client, jid, {"awaiting_target_review", "error"})
    iss = next(i for i in client.get(f"/api/jobs/{jid}/target-reviews").json() if i["field"] == "department")
    r = client.post(f"/api/jobs/{jid}/target-reviews/{iss['id']}/decision",
                    json={"version": iss["version"], "action": "use_incoming", "note": "client confirmed dept"})
    assert r.status_code == 200
    _poll(client, jid, {"reconciliation_complete", "error"})
    cid = next(x["id"] for x in client.get(f"/api/jobs/{jid}/candidates").json() if x["business_key"] == "100")
    v = _versions(client, jid, cid)
    assert [x["version_no"] for x in v] == [2, 1]
    v2 = next(x for x in v if x["version_no"] == 2)
    assert v2["origin"] == "human" and v2["snapshot"]["department"] == "Finance"
    assert v2["decision_note"] == "client confirmed dept"
    ch = next(c for c in (v2["field_changes"] or []) if c["field"] == "department")
    assert ch["from"] == "Engineering" and ch["to"] == "Finance" and ch["provenance"]  # real provenance
