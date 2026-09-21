"""M3A: deterministic incoming-vs-existing-target reconciliation (10 cases).

Reconciliation is SEPARATE from M2 incoming-vs-incoming. It reads the confirmed target only
through the HTTP gateway (never its tables), classifies conservatively, and NEVER writes.
Core safety: never overwrite a non-empty target value with a different non-empty incoming value
automatically; never fuzzy-match by name; unsafe conflicts escalate to human review. Human
overlays (keep_existing / use_incoming / exclude) recompute deterministically.

The gateway is exercised in-process via httpx ASGITransport against a fresh seeded mock target
(its own SQLite) — no live port. Seed (mock_target/service.py):
  100 Ada  ada@corp.com  Engineering       2019-01-01  (contract null)
  200 Bob  bob@corp.com  <dept null>       2020-02-02  (contract null)
  300 Carol carol@corp.com Sales           2018-03-03
  400 Dave dave@corp.com Finance           2017-04-04
  401 Erin erin@corp.com People Operations 2016-05-05
"""
from __future__ import annotations

import httpx
import pytest

from app.db import Database
from app.reconcile_target import reconcile, reconciliation_invariant_error
from app.schema_loader import get_target_schema
from app.target_gateway import TargetEmployeeGateway
from mock_target.service import create_app as create_target_app

FIELDS = ["employee_id", "full_name", "work_email", "department", "hire_date", "contract_start_date"]


@pytest.fixture
async def env(tmp_path):
    schema = get_target_schema()
    db = Database(tmp_path / "app.db")
    target_app = create_target_app(db_path=str(tmp_path / "target.db"))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=target_app), base_url="http://t")
    gw = TargetEmployeeGateway(client=client)
    try:
        yield db, gw, schema
    finally:
        await client.aclose()
        db.close()


def _cand(cid: str, fields: dict) -> dict:
    rec = {f: {"value": fields.get(f), "status": "resolved" if fields.get(f) is not None else "null"}
           for f in FIELDS}
    return {"id": cid, "business_key": fields.get("employee_id"), "eligibility": "eligible",
            "record": rec, "source_refs": [], "issue_ids": []}


def _setup(db: Database, cands: list[dict]) -> str:
    job = db.create_job(schema_version="employee.v1", provider="fake", model_id="m", adapter_kind="fake")
    db.replace_candidates(job, cands)
    return job


def _by_cand(res: dict, cid: str) -> dict:
    return next(r for r in res["results"] if r["candidate_id"] == cid)


def _persist_and_resolve(db: Database, job: str, issue: dict, action: str) -> None:
    """Mimic the review endpoint: persist the reconcile issue, then resolve it with an overlay."""
    db.upsert_target_review_issue(
        job, issue_id=issue["id"], candidate_id=issue["candidate_id"],
        business_key=issue.get("business_key"), field=issue.get("field"), issue_type=issue["issue_type"],
        reason=issue["reason"], incoming_value=issue.get("incoming_value"),
        target_value=issue.get("target_value"), match_basis=issue["match_basis"],
        options=issue.get("options", []), affected=issue.get("affected", {}))
    cur = db.get_target_review_issue(issue["id"])
    db.resolve_target_issue_and_enqueue(
        job, issue["id"], expected_version=cur["version"],
        resolution={"action": action, "field": issue.get("field"), "actor": "human"})


# ------------------------------- the 10 cases -------------------------------
async def test_case01_ready_create_new_employee(env):
    db, gw, schema = env
    job = _setup(db, [_cand("c1", {"employee_id": "999", "full_name": "Zoe New",
                                   "work_email": "zoe@corp.com", "department": "Sales",
                                   "hire_date": "2021-01-01"})])
    res = await reconcile(db, gw, job, schema)
    r = _by_cand(res, "c1")
    assert r["outcome"] == "READY_CREATE" and r["match_basis"] == "none"


async def test_case02_no_change_exact_match(env):
    db, gw, schema = env
    job = _setup(db, [_cand("c1", {"employee_id": "100", "full_name": "Ada Existing",
                                   "work_email": "ada@corp.com", "department": "Engineering",
                                   "hire_date": "2019-01-01"})])
    res = await reconcile(db, gw, job, schema)
    r = _by_cand(res, "c1")
    assert r["outcome"] == "NO_CHANGE" and r["match_basis"] == "employee_id"


async def test_case03_ready_update_fills_blank_target_field(env):
    db, gw, schema = env
    # Target 200 has a NULL department; incoming supplies one, everything else matches.
    job = _setup(db, [_cand("c1", {"employee_id": "200", "full_name": "Bob Existing",
                                   "work_email": "bob@corp.com", "department": "Sales",
                                   "hire_date": "2020-02-02"})])
    res = await reconcile(db, gw, job, schema)
    r = _by_cand(res, "c1")
    assert r["outcome"] == "READY_UPDATE"
    assert r["diff"]["department"]["status"] == "update"


async def test_case04_review_required_value_conflict(env):
    db, gw, schema = env
    # Target 100 department Engineering; incoming Finance (both non-empty, differ) -> unsafe.
    job = _setup(db, [_cand("c1", {"employee_id": "100", "full_name": "Ada Existing",
                                   "work_email": "ada@corp.com", "department": "Finance",
                                   "hire_date": "2019-01-01"})])
    res = await reconcile(db, gw, job, schema)
    r = _by_cand(res, "c1")
    assert r["outcome"] == "REVIEW_REQUIRED"
    assert r["diff"]["department"]["status"] == "conflict"
    assert any(i["field"] == "department" and i["issue_type"] == "value_conflict" for i in res["issues"])


async def test_case05_review_required_email_owned_by_other_id(env):
    db, gw, schema = env
    # New id 555 but its work_email belongs to confirmed target 300 (Carol).
    job = _setup(db, [_cand("c1", {"employee_id": "555", "full_name": "Imposter",
                                   "work_email": "carol@corp.com", "department": "Sales",
                                   "hire_date": "2021-01-01"})])
    res = await reconcile(db, gw, job, schema)
    r = _by_cand(res, "c1")
    assert r["outcome"] == "REVIEW_REQUIRED"
    iss = next(i for i in res["issues"] if i["candidate_id"] == "c1")
    assert iss["issue_type"] == "email_owned_by_other" and iss["options"] == ["exclude"]


async def test_case06_review_required_id_and_email_resolve_to_different_records(env):
    db, gw, schema = env
    # id 400 -> Dave; email erin@corp.com -> 401 Erin. Identity conflict.
    job = _setup(db, [_cand("c1", {"employee_id": "400", "full_name": "Dave Existing",
                                   "work_email": "erin@corp.com", "department": "Finance",
                                   "hire_date": "2017-04-04"})])
    res = await reconcile(db, gw, job, schema)
    r = _by_cand(res, "c1")
    assert r["outcome"] == "REVIEW_REQUIRED"
    assert any(i["issue_type"] == "id_email_mismatch" for i in res["issues"])


async def test_case07_human_exclude_marks_excluded(env):
    db, gw, schema = env
    job = _setup(db, [_cand("c1", {"employee_id": "555", "full_name": "Imposter",
                                   "work_email": "carol@corp.com"})])
    res1 = await reconcile(db, gw, job, schema)
    iss = next(i for i in res1["issues"] if i["candidate_id"] == "c1")
    _persist_and_resolve(db, job, iss, "exclude")
    res2 = await reconcile(db, gw, job, schema)
    assert _by_cand(res2, "c1")["outcome"] == "EXCLUDED"


async def test_case08_human_keep_existing_becomes_no_change(env):
    db, gw, schema = env
    job = _setup(db, [_cand("c1", {"employee_id": "100", "full_name": "Ada Existing",
                                   "work_email": "ada@corp.com", "department": "Finance",
                                   "hire_date": "2019-01-01"})])
    res1 = await reconcile(db, gw, job, schema)
    iss = next(i for i in res1["issues"] if i["field"] == "department")
    _persist_and_resolve(db, job, iss, "keep_existing")
    res2 = await reconcile(db, gw, job, schema)
    r = _by_cand(res2, "c1")
    assert r["outcome"] == "NO_CHANGE"
    assert r["diff"]["department"]["decision"] == "keep_existing"


async def test_case09_human_use_incoming_becomes_ready_update(env):
    db, gw, schema = env
    job = _setup(db, [_cand("c1", {"employee_id": "100", "full_name": "Ada Existing",
                                   "work_email": "ada@corp.com", "department": "Finance",
                                   "hire_date": "2019-01-01"})])
    res1 = await reconcile(db, gw, job, schema)
    iss = next(i for i in res1["issues"] if i["field"] == "department")
    _persist_and_resolve(db, job, iss, "use_incoming")
    res2 = await reconcile(db, gw, job, schema)
    r = _by_cand(res2, "c1")
    assert r["outcome"] == "READY_UPDATE"
    assert r["diff"]["department"]["decision"] == "use_incoming"


async def test_case10_never_autooverwrite_and_never_fuzzy_match_by_name(env):
    db, gw, schema = env
    job = _setup(db, [
        # (a) same id, DIFFERENT non-empty full_name -> must NOT auto-overwrite; escalates.
        _cand("conflict", {"employee_id": "100", "full_name": "Ada RENAMED",
                           "work_email": "ada@corp.com", "department": "Engineering",
                           "hire_date": "2019-01-01"}),
        # (b) NEW id + NEW email but the SAME NAME as target 100 -> must be CREATE, not matched.
        _cand("namesake", {"employee_id": "888", "full_name": "Ada Existing",
                           "work_email": "ada.new@corp.com", "department": "Sales",
                           "hire_date": "2022-02-02"}),
    ])
    res = await reconcile(db, gw, job, schema)
    conflict = _by_cand(res, "conflict")
    assert conflict["outcome"] == "REVIEW_REQUIRED"
    assert conflict["diff"]["full_name"]["status"] == "conflict"        # not silently overwritten
    namesake = _by_cand(res, "namesake")
    assert namesake["outcome"] == "READY_CREATE" and namesake["match_basis"] == "none"  # no name match


# ---------------------- snapshots + completion invariant ----------------------
async def test_snapshot_persists_target_revision(env):
    db, gw, schema = env
    job = _setup(db, [_cand("c1", {"employee_id": "100", "full_name": "Ada Existing",
                                   "work_email": "ada@corp.com", "department": "Engineering",
                                   "hire_date": "2019-01-01"})])
    res = await reconcile(db, gw, job, schema)
    snap = next(s for s in res["snapshots"] if s["candidate_id"] == "c1")
    assert snap["target_record_id"] == "100"
    assert snap["target_revision"] == 1          # revision captured for M3B optimistic concurrency
    assert snap["match_basis"] == "employee_id"


async def test_completion_invariant_blocks_until_conflicts_resolved(env):
    db, gw, schema = env
    job = _setup(db, [_cand("c1", {"employee_id": "100", "full_name": "Ada Existing",
                                   "work_email": "ada@corp.com", "department": "Finance",
                                   "hire_date": "2019-01-01"})])
    res1 = await reconcile(db, gw, job, schema)
    iss = next(i for i in res1["issues"] if i["field"] == "department")
    # Persist the open issue so the invariant sees it.
    db.upsert_target_review_issue(
        job, issue_id=iss["id"], candidate_id=iss["candidate_id"], business_key=iss.get("business_key"),
        field=iss.get("field"), issue_type=iss["issue_type"], reason=iss["reason"],
        incoming_value=iss.get("incoming_value"), target_value=iss.get("target_value"),
        match_basis=iss["match_basis"], options=iss.get("options", []), affected=iss.get("affected", {}))
    assert reconciliation_invariant_error(db, job, res1["results"]) is not None   # review_required blocks

    _persist_and_resolve(db, job, iss, "keep_existing")
    res2 = await reconcile(db, gw, job, schema)
    assert reconciliation_invariant_error(db, job, res2["results"]) is None       # now complete-able


async def test_reconciliation_is_idempotent(env):
    db, gw, schema = env
    job = _setup(db, [_cand("c1", {"employee_id": "999", "full_name": "Zoe New",
                                   "work_email": "zoe@corp.com", "department": "Sales",
                                   "hire_date": "2021-01-01"})])
    a = await reconcile(db, gw, job, schema)
    b = await reconcile(db, gw, job, schema)
    assert a["counts"] == b["counts"]
    assert [r["outcome"] for r in a["results"]] == [r["outcome"] for r in b["results"]]
