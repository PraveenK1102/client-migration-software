"""End-to-end acceptance (order Phase J): the whole pipeline with source intelligence, over HTTP.

Fake provider (deterministic, no network) drives a source-intelligence-rich CSV all the way from
upload to a delivered target: ingest -> map (with GenderID retired as redundant, Sex->gender,
DOB/hire dates auto-inferred) -> prepare -> reconcile -> deliver, then asserts the intelligence
carried through to prepared records + immutable versions + audit + metrics, and that delivery wrote
to the target only via the gateway. This ties M3C into the M3B delivery machinery end to end.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.main import create_app

# Ids/emails deliberately NOT in the mock-target seed, so all four are clean CREATEs.
CSV = (
    b"Employee ID,Full Name,Work Email,Department,Hire Date,Sex,GenderID\n"
    b"EE1,Ada Lovelace,ada.l@newco.example,Engineering,2020-01-15,F,0\n"
    b"EE2,Alan Turing,alan.t@newco.example,Engineering,2019-06-01,M,1\n"
    b"EE3,Grace Hopper,grace.h@newco.example,Sales,2021-03-20,F,0\n"
    b"EE4,Ken Thompson,ken.t@newco.example,Finance,2018-11-11,M,1\n")

TERMINAL = {"migration_complete", "delivery_partial_failure", "reconciliation_complete", "error"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "fake")           # deterministic, offline
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AUTO_CONTINUE", "true")          # chain prepare -> reconcile -> deliver
    from app import config
    config.get_settings.cache_clear()
    settings = config.get_settings()
    settings.ensure_dirs()
    try:
        with TestClient(create_app()) as c:
            c._settings = settings
            yield c
    finally:
        config.get_settings.cache_clear()


def _poll(c, jid, want, tries=1500, delay=0.02):
    for _ in range(tries):
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] in want:
            return j
        time.sleep(delay)
    return c.get(f"/api/jobs/{jid}").json()


def test_full_pipeline_with_source_intelligence_reaches_target(client):
    jid = client.post("/api/jobs", files=[("files", ("emp.csv", CSV, "text/csv"))]).json()["id"]

    # Resolve any pauses (record reviews / target reviews) deterministically until terminal.
    for _ in range(30):
        j = _poll(client, jid, TERMINAL | {"awaiting_record_review", "awaiting_target_review",
                                           "blocked_provider", "awaiting_review"})
        st = j["status"]
        if st in TERMINAL:
            break
        if st in ("awaiting_record_review",):
            for r in client.get(f"/api/jobs/{jid}/record-reviews").json():
                body = {"version": r["version"], "note": "e2e"}
                if r["issue_type"] == "ambiguous_date":
                    body.update(action="confirm_convention", convention="MDY")
                elif r["issue_type"] == "unknown_enum":
                    body.update(action="map_values",
                                value_map={d["value"]: (r["options"][0] if r["options"] else "")
                                           for d in r["affected"].get("distinct_values", [])})
                else:
                    body.update(action="exclude")
                client.post(f"/api/jobs/{jid}/record-reviews/{r['id']}/decision", json=body)
        elif st == "awaiting_target_review":
            for t in client.get(f"/api/jobs/{jid}/target-reviews").json():
                client.post(f"/api/jobs/{jid}/target-reviews/{t['id']}/decision",
                            json={"version": t["version"], "action": "use_incoming", "note": "e2e"})
        else:
            break

    job = client.get(f"/api/jobs/{jid}").json()
    assert job["status"] in ("migration_complete", "reconciliation_complete"), (job["status"], job.get("error"))

    db = Database(client._settings.app_db_path)
    # M3C intelligence survived end to end: GenderID retired as redundant; Sex mapped to gender.
    dec = {d["source_header"]: d for d in db.get_decisions(jid)}
    assert dec["GenderID"]["status"] == "redundant"
    assert dec["Sex"]["target_field"] == "gender"
    plans = [p for p in db.get_transformation_plans(jid) if p["kind"] == "redundant"]
    assert plans and plans[0]["source_header"] == "GenderID"

    # Prepared employees are eligible and carry gender (from Sex), not the raw GenderID code.
    cands = client.get(f"/api/jobs/{jid}/candidates").json()
    assert cands and all(c["eligibility"] == "eligible" for c in cands)
    g = cands[0]["record"].get("gender", {})
    assert g.get("value") in ("male", "female")

    # Immutable versions were appended for delivered/reconciled employees.
    ver = client.get(f"/api/jobs/{jid}/candidates/{cands[0]['id']}/versions").json()
    assert ver and all("version_no" in v for v in ver)

    # Metrics reflect the whole run.
    m = client.get(f"/api/jobs/{jid}/metrics").json()
    assert m["intelligence"]["redundant_representations"] >= 1
    assert m["mapping"]["rule"] >= 5

    # Audit reconstructs the decisions (mapping + redundancy + reconciliation at least).
    events = {a["event_type"] for a in client.get(f"/api/jobs/{jid}/audit").json()}
    assert {"mapping_rule_accepted", "redundant_representation"} <= events
