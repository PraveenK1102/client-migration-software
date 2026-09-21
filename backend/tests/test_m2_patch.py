"""M2.1 correctness-patch regressions:
required-null safety, overlay-before-cross-record, shared-email correct/exclude with
uniqueness recomputation, and the finalize invariant (preparation_complete => blocked==0).
All offline (fake provider; canonical headers -> zero model calls)."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.prepare import finalize_invariant_error

CANON = "employee_id,full_name,work_email,department,hire_date,contract_start_date\n"


@pytest.fixture
def client(temp_settings):
    with TestClient(create_app()) as c:
        yield c


def _poll(client, job_id, want, tries=300, delay=0.03):
    for _ in range(tries):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in want:
            return j
        time.sleep(delay)
    return client.get(f"/api/jobs/{job_id}").json()


def _upload(client, text):
    return client.post("/api/jobs", files=[("files", ("t.csv", text.encode(), "text/csv"))]).json()


def _open_issues(client, job_id):
    return client.get(f"/api/jobs/{job_id}/record-reviews").json()


# ---------- Finding 1: required field cannot be nulled ----------
def test_null_on_required_field_rejected_and_issue_stays_open(client):
    text = CANON + "001,Alice,,Engineering,2020-01-01,\n"  # missing required work_email
    job_id = _poll(client, _upload(client, text)["id"], {"awaiting_record_review", "error"})["id"]
    issues = _open_issues(client, job_id)
    req = next(i for i in issues if i["field"] == "work_email" and i["issue_type"] == "missing_required")
    r = client.post(f"/api/jobs/{job_id}/record-reviews/{req['id']}/decision",
                    json={"version": req["version"], "action": "null"})
    assert r.status_code == 400
    still = _open_issues(client, job_id)
    assert any(i["id"] == req["id"] and i["status"] == "open" for i in still)


# ---------- Finding 2: optional field CAN be nulled ----------
def test_null_on_optional_field_allowed_and_becomes_eligible(client):
    text = CANON + "001,Alice,alice@x.com,Marketing,2020-01-01,\n"  # unknown optional department
    job_id = _poll(client, _upload(client, text)["id"], {"awaiting_record_review", "error"})["id"]
    iss = next(i for i in _open_issues(client, job_id)
               if i["field"] == "department" and i["issue_type"] == "unknown_enum")
    r = client.post(f"/api/jobs/{job_id}/record-reviews/{iss['id']}/decision",
                    json={"version": iss["version"], "action": "null", "reason": "unknown dept"})
    assert r.status_code == 200
    job = _poll(client, job_id, {"preparation_complete", "error"})
    assert job["status"] == "preparation_complete"
    ds = client.get(f"/api/jobs/{job_id}/prepared-dataset").json()
    assert ds["ready_for_target"] == 1 and ds["employees"][0]["department"] is None


# ---------- Finding 3: shared email + exclude one ----------
def test_shared_email_exclude_one_surviving_eligible_zero_blocked(client):
    text = CANON + ("001,Alice,alice@x.com,Engineering,2020-01-01,\n"
                    "002,Bob,dup@x.com,Sales,2020-02-02,\n"
                    "003,Carol,dup@x.com,Finance,2020-03-03,\n")
    job_id = _poll(client, _upload(client, text)["id"], {"awaiting_record_review", "error"})["id"]
    iss = next(i for i in _open_issues(client, job_id) if i["issue_type"] == "shared_email")
    victim = iss["affected"]["candidates"][1]["candidate_id"]
    r = client.post(f"/api/jobs/{job_id}/record-reviews/{iss['id']}/decision",
                    json={"version": iss["version"], "action": "exclude", "candidate_id": victim})
    assert r.status_code == 200
    job = _poll(client, job_id, {"preparation_complete", "error"})
    assert job["status"] == "preparation_complete"
    assert job["prep_summary"]["counts"]["blocked"] == 0
    assert job["prep_summary"]["counts"]["excluded"] == 1
    ds = client.get(f"/api/jobs/{job_id}/prepared-dataset").json()
    ids = {e["employee_id"] for e in ds["employees"]}
    assert "001" in ids and len(ids) == 2 and len(ds["excluded"]) == 1


# ---------- Finding 4: shared email + correct one -> both eligible ----------
def test_shared_email_correct_one_removes_collision_both_eligible(client):
    text = CANON + ("002,Bob,dup@x.com,Sales,2020-02-02,\n"
                    "003,Carol,dup@x.com,Finance,2020-03-03,\n")
    job_id = _poll(client, _upload(client, text)["id"], {"awaiting_record_review", "error"})["id"]
    iss = next(i for i in _open_issues(client, job_id) if i["issue_type"] == "shared_email")
    target = iss["affected"]["candidates"][1]["candidate_id"]
    r = client.post(f"/api/jobs/{job_id}/record-reviews/{iss['id']}/decision",
                    json={"version": iss["version"], "action": "correct",
                          "candidate_id": target, "value": "carol.unique@x.com"})
    assert r.status_code == 200, r.text
    job = _poll(client, job_id, {"preparation_complete", "error"})
    assert job["status"] == "preparation_complete"
    assert job["prep_summary"]["counts"]["eligible"] == 2
    assert job["prep_summary"]["counts"]["blocked"] == 0


# ---------- Finding 5: correction that creates a NEW collision stays blocked ----------
def test_shared_email_correction_creating_new_collision_stays_blocked(client):
    text = CANON + ("001,Alice,alice@x.com,Engineering,2020-01-01,\n"
                    "002,Bob,dup@x.com,Sales,2020-02-02,\n"
                    "003,Carol,dup@x.com,Finance,2020-03-03,\n")
    job_id = _poll(client, _upload(client, text)["id"], {"awaiting_record_review", "error"})["id"]
    iss = next(i for i in _open_issues(client, job_id) if i["issue_type"] == "shared_email")
    target = iss["affected"]["candidates"][1]["candidate_id"]
    # correct one colliding email to ALICE's email -> new collision (001, that candidate)
    r = client.post(f"/api/jobs/{job_id}/record-reviews/{iss['id']}/decision",
                    json={"version": iss["version"], "action": "correct",
                          "candidate_id": target, "value": "alice@x.com"})
    assert r.status_code == 200

    # Wait for the resume to recompute and SURFACE the new collision (not a transient snapshot).
    def has_open_shared():
        return any(i["issue_type"] == "shared_email" for i in _open_issues(client, job_id))
    for _ in range(400):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("preparation_complete", "error"):
            break
        if j["status"] == "awaiting_record_review" and has_open_shared():
            break
        time.sleep(0.03)

    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "awaiting_record_review"   # the new collision must reopen review
    assert has_open_shared()                            # never silently finalized as eligible


# ---------- Finding 6 & 7: finalize invariant ----------
def test_finalize_invariant_helper():
    assert finalize_invariant_error([{"eligibility": "eligible"}], 0) is None
    assert finalize_invariant_error([{"eligibility": "excluded"}], 0) is None
    msg = finalize_invariant_error([{"eligibility": "blocked"}], 0)
    assert msg and "invariant" in msg.lower()


def test_preparation_complete_implies_zero_blocked(client):
    # A clean canonical table finalizes with blocked == 0.
    text = CANON + "001,Alice,alice@x.com,Engineering,2020-01-01,\n"
    job = _poll(client, _upload(client, text)["id"], {"preparation_complete", "error"})
    assert job["status"] == "preparation_complete"
    assert job["prep_summary"]["counts"]["blocked"] == 0


def test_every_blocked_candidate_has_an_open_issue(temp_settings):
    # Property that makes the finalize invariant hold: prepare() never leaves a blocked
    # candidate without an open reviewable issue.
    import asyncio
    from app.runtime import AppContext
    from tests.test_m2_graph import SpyAdapter, _ingest, _map, _prep

    async def run():
        ctx = await AppContext.create(temp_settings, adapter_override=SpyAdapter(), use_override=True)
        try:
            jid = _ingest(ctx, "canonical_messy.csv")
            await _map(ctx, jid); await _prep(ctx, jid)
            open_ids = {i["id"] for i in ctx.db.get_record_issues(jid, status="open")}
            for c in ctx.db.get_candidates(jid):
                if c["eligibility"] == "blocked":
                    import json
                    refs = set(json.loads(c["issue_ids"] or "[]"))
                    assert refs & open_ids, f"blocked candidate {c['business_key']} has no open issue"
        finally:
            await ctx.aclose()
    asyncio.run(run())


# ---------- Finding 8: idempotent re-run ----------
def test_repeat_preparation_is_idempotent(client):
    text = CANON + ("001,Alice,alice@x.com,Engineering,2020-01-01,\n"
                    "002,Bob,bob@x.com,Sales,2020-02-02,\n")
    job_id = _poll(client, _upload(client, text)["id"], {"preparation_complete", "error"})["id"]
    n1 = len(client.get(f"/api/jobs/{job_id}/candidates").json())
    client.post(f"/api/jobs/{job_id}/prepare")
    _poll(client, job_id, {"preparation_complete"})
    n2 = len(client.get(f"/api/jobs/{job_id}/candidates").json())
    assert n1 == n2 == 2
