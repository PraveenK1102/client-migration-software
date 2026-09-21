"""M3B.2 — final delivery-correctness patch. Behavioral tests (real pipeline / real worker methods,
not source-string inspection) for each ordered item:

  1. STALE -> human review -> replan fully refreshes the operation (idempotency key bound to the new
     revision, new before-snapshot, post-review payload).
  2. STALE -> NO_CHANGE / EXCLUDED after review TERMINALIZES the old op (SKIPPED_NO_CHANGE /
     SKIPPED_EXCLUDED), never writes, never rolls back; mixed jobs still reach migration_complete.
  3. Delivery retry EXHAUSTION terminalizes the op (FAILED) and the job; rollback exhaustion -> FAILED.
  4. Manual retry policy decided from the last attempt's retryability, not a partial category list.
  5. Operation-level execution fence: two due work items for one op -> exactly one external write.
  6. DB-level attempt-number uniqueness per operation.
  7. Backoff uses the actual attempt number (exponential growth within jitter; Retry-After precedence).

Offline: GROQ_API_KEY empty (no model calls); AUTO_CONTINUE disabled so the tests drive delivery.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

DEMO = Path(__file__).resolve().parent.parent.parent / "sample-data" / "structured-demo"


# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------

def _mk_client(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AUTO_CONTINUE", "false")
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    from app import config
    config.get_settings.cache_clear()
    return TestClient(create_app())


@pytest.fixture
def client(tmp_path, monkeypatch):
    with _mk_client(tmp_path, monkeypatch) as c:
        yield c
    from app import config
    config.get_settings.cache_clear()


@pytest.fixture
def fast_retry_client(tmp_path, monkeypatch):
    """Workers on, tiny backoff + small retry budget so exhaustion happens quickly."""
    with _mk_client(tmp_path, monkeypatch, TARGET_MAX_ATTEMPTS=3,
                    TARGET_RETRY_BASE_SECONDS=0.05, TARGET_RETRY_MAX_SECONDS=0.2) as c:
        yield c
    from app import config
    config.get_settings.cache_clear()


@pytest.fixture
def manual_client(tmp_path, monkeypatch):
    """Workers OFF — the test drives WorkerPool methods directly (fence / direct-invocation tests)."""
    with _mk_client(tmp_path, monkeypatch, START_WORKERS="false") as c:
        yield c
    from app import config
    config.get_settings.cache_clear()


def _arun(coro):
    """Run a coroutine on a fresh event loop. Robust in the full suite where a prior async test may
    have closed the main-thread loop (Python 3.12 no longer auto-creates one via get_event_loop)."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def _mc(c):
    ctx = c.app.state.ctx
    if ctx._target_app is None:
        pytest.skip("no in-process mock target")
    from starlette.testclient import TestClient as SC
    return SC(ctx._target_app)


def _poll(c, jid, want, tries=6000, delay=0.02):
    last = None
    for _ in range(tries):
        last = c.get(f"/api/jobs/{jid}").json()
        if last["status"] in want:
            return last
        time.sleep(delay)
    raise TimeoutError(f"job {jid} stuck at {last and last['status']}; wanted {want}")


def _upload(c, csv_text, tenant="default"):
    files = [("files", ("employees.csv", csv_text.encode(), "text/csv"))]
    r = c.post("/api/jobs", files=files, data={"tenant_id": tenant})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _drive_recon(c, jid):
    """Drive to reconciliation_complete, resolving any review with a use_incoming/first-option pick."""
    for _ in range(400):
        job = _poll(c, jid, {"blocked_provider", "awaiting_review", "awaiting_record_review",
                             "mapping_complete", "preparation_complete", "error"})
        st = job["status"]
        if st in ("preparation_complete", "error"):
            break
        if st == "blocked_provider":
            for p in c.get(f"/api/jobs/{jid}/custom-field-proposals?status=open").json():
                c.post(f"/api/jobs/{jid}/custom-field-proposals/{p['id']}/decision",
                       json={"action": "ignore", "version": p["version"]})
        elif st == "awaiting_review":
            for i in c.get(f"/api/jobs/{jid}/reviews?status=open").json():
                c.post(f"/api/jobs/{jid}/reviews/{i['id']}/decision",
                       json={"action": "approve", "version": i["version"], "reason": "t"})
        elif st == "awaiting_record_review":
            for i in c.get(f"/api/jobs/{jid}/record-reviews?status=open").json():
                opts = json.loads(i["options"]) if isinstance(i.get("options"), str) else (i.get("options") or [])
                action = opts[0]["value"] if opts and isinstance(opts[0], dict) and "value" in opts[0] else \
                    (opts[0] if opts else "accept")
                c.post(f"/api/jobs/{jid}/record-reviews/{i['id']}/decision",
                       json={"action": str(action), "version": i["version"], "reason": "t"})
        time.sleep(0.02)
    assert job["status"] == "preparation_complete", job["status"]
    assert c.post(f"/api/jobs/{jid}/reconcile").status_code == 200
    return _resolve_targets_to_recon_complete(c, jid, action="use_incoming")


def _resolve_targets_to_recon_complete(c, jid, *, action):
    for _ in range(400):
        job = _poll(c, jid, {"reconciliation_complete", "awaiting_target_review", "error"})
        if job["status"] in ("reconciliation_complete", "error"):
            break
        for i in c.get(f"/api/jobs/{jid}/target-reviews?status=open").json():
            c.post(f"/api/jobs/{jid}/target-reviews/{i['id']}/decision",
                   json={"action": action, "version": i["version"], "reason": "t"})
        time.sleep(0.02)
    assert job["status"] == "reconciliation_complete", job["status"]
    return job


def _delivery(c, jid):
    return c.get(f"/api/jobs/{jid}/delivery").json()


def _op_by_candidate(c, jid, op_type=None):
    for o in _delivery(c, jid)["operations"]:
        if op_type is None or o["op_type"] == op_type:
            return o
    return None


# ===========================================================================
# Item 1 — STALE -> human review -> replan refreshes idempotency/evidence
# ===========================================================================

def test_stale_review_replan_refreshes_idempotency(client):
    c = client
    mc = _mc(c)
    # Seed target with an EMPTY department so incoming safely fills it (READY_UPDATE @ rev1).
    seed = {"employee_id": "E-STALE", "full_name": "Stan Stale",
            "work_email": "stan.stale@corp.example", "hire_date": "2020-01-01", "department": None}
    assert mc.post("/employees", json={"employee": seed, "idempotency_key": "estale"}).status_code == 201

    csv = ("Employee ID,Full Name,Work Email,Department,Hire Date\n"
           "E-STALE,Stan Stale,stan.stale@corp.example,Sales,2020-01-01\n")
    jid = _upload(c, csv)
    _drive_recon(c, jid)

    # External change AFTER reconciliation → target is now rev2 (department=Finance); our plan is rev1.
    assert mc.post("/_mock/employees/E-STALE/mutate",
                   json={"fields": {"department": "Finance"}}).status_code == 200

    assert c.post(f"/api/jobs/{jid}/deliver").status_code == 200
    job = _poll(c, jid, {"stale_target_review_required", "migration_complete",
                         "delivery_partial_failure", "error"})
    assert job["status"] == "stale_target_review_required", job["status"]

    op = _op_by_candidate(c, jid, "UPDATE")
    assert op["status"] == "STALE_TARGET", op
    rev1_key = op["idempotency_key"]
    assert rev1_key.endswith(":update:1"), rev1_key
    op_id = op["id"]

    # Human review path: re-run the FULL reconciliation (surfaces the conflict), resolve use_incoming.
    assert c.post(f"/api/jobs/{jid}/reconcile").status_code == 200
    job = _poll(c, jid, {"awaiting_target_review", "reconciliation_complete", "error"})
    assert job["status"] == "awaiting_target_review", job["status"]
    _resolve_targets_to_recon_complete(c, jid, action="use_incoming")

    # Block the actual write so we can assert the refreshed plan BEFORE any successful target call.
    mc.post("/_mock/failures", json={"mode": "status", "status": 500, "count": 100, "scope": "update"})
    assert c.post(f"/api/jobs/{jid}/deliver").status_code == 200

    # Wait until the op has actually been executed against the target with the NEW plan (a 500 attempt
    # means the target was called and rejected — no side effect yet).
    for _ in range(6000):
        atts = c.get(f"/api/jobs/{jid}/delivery/operations/{op_id}/attempts").json()
        if any(a.get("http_status") == 500 for a in atts):
            break
        time.sleep(0.02)
    else:
        raise AssertionError("no target call observed for the replanned op")

    op2 = c.get(f"/api/jobs/{jid}/delivery/operations/{op_id}").json()
    # ---- assertions BEFORE the successful write ----
    assert op2["expected_target_revision"] == 2, op2
    assert op2["idempotency_key"].endswith(":update:2"), op2["idempotency_key"]
    assert op2["idempotency_key"] != rev1_key
    before = op2["before_snapshot"]
    before = json.loads(before) if isinstance(before, str) else before
    assert before and before.get("department") == "Finance", before   # the rev2 snapshot
    payload = op2["payload"]
    payload = json.loads(payload) if isinstance(payload, str) else payload
    assert payload.get("department") == "Sales", payload              # post-review decision
    # target NOT yet written (still Finance, revision 2)
    tgt = mc.get("/employees/E-STALE").json()["employee"]
    assert tgt["department"] == "Finance" and tgt["revision"] == 2

    # Now let it deliver successfully (clear failures + manual retry — RETRYABLE/exhausted-retryable).
    mc.delete("/_mock/failures")
    r = c.post(f"/api/jobs/{jid}/delivery/retry", json={"operation_id": op_id})
    assert r.status_code == 200, r.text
    job = _poll(c, jid, {"migration_complete", "delivery_partial_failure", "error"})
    assert job["status"] == "migration_complete", job["status"]
    final = mc.get("/employees/E-STALE").json()["employee"]
    assert final["department"] == "Sales"
    op_final = c.get(f"/api/jobs/{jid}/delivery/operations/{op_id}").json()
    assert op_final["status"] == "SUCCEEDED"
    assert op_final["idempotency_key"].endswith(":update:2")


# ===========================================================================
# Item 2A — stale -> review -> keep current -> NO_CHANGE terminalizes the op
# ===========================================================================

def _drive_stale_to_review(c, mc, eid, external_dept="Finance"):
    """Seed empty-dept target `eid`, fill it (READY_UPDATE rev1), externally mutate to rev2, deliver
    → 409 → stale_target_review_required. Returns (jid, op_id, rev1_key)."""
    seed = {"employee_id": eid, "full_name": f"{eid} Person",
            "work_email": f"{eid.lower()}@corp.example", "hire_date": "2020-01-01", "department": None}
    assert mc.post("/employees", json={"employee": seed, "idempotency_key": f"{eid}-seed"}).status_code == 201
    csv = ("Employee ID,Full Name,Work Email,Department,Hire Date\n"
           f"{eid},{eid} Person,{eid.lower()}@corp.example,Sales,2020-01-01\n")
    jid = _upload(c, csv)
    _drive_recon(c, jid)
    assert mc.post(f"/_mock/employees/{eid}/mutate",
                   json={"fields": {"department": external_dept}}).status_code == 200
    assert c.post(f"/api/jobs/{jid}/deliver").status_code == 200
    job = _poll(c, jid, {"stale_target_review_required", "migration_complete",
                         "delivery_partial_failure", "error"})
    assert job["status"] == "stale_target_review_required", job["status"]
    op = _op_by_candidate(c, jid, "UPDATE")
    assert op["status"] == "STALE_TARGET"
    return jid, op["id"], op["idempotency_key"]


def test_stale_review_keep_current_becomes_skipped_no_change(client):
    c = client
    mc = _mc(c)
    jid, op_id, _ = _drive_stale_to_review(c, mc, "E-KEEP")
    writes_before = mc.get("/_mock/stats").json()["counters"].get("write_calls", 0)

    # Re-run full reconciliation, resolve the conflict by KEEPING the current target value.
    assert c.post(f"/api/jobs/{jid}/reconcile").status_code == 200
    _poll(c, jid, {"awaiting_target_review", "error"})
    _resolve_targets_to_recon_complete(c, jid, action="keep_existing")

    # Re-deliver → the stale op must terminalize to SKIPPED_NO_CHANGE and the job must complete.
    assert c.post(f"/api/jobs/{jid}/deliver").status_code == 200
    job = _poll(c, jid, {"migration_complete", "delivery_partial_failure", "error"})
    assert job["status"] == "migration_complete", job["status"]

    op = c.get(f"/api/jobs/{jid}/delivery/operations/{op_id}").json()
    assert op["status"] == "SKIPPED_NO_CHANGE", op
    # zero additional migration write
    assert mc.get("/_mock/stats").json()["counters"].get("write_calls", 0) == writes_before
    # not eligible for rollback
    r = c.post(f"/api/jobs/{jid}/rollback")
    assert r.status_code == 400, "no succeeded ops → rollback rejected"


def test_stale_review_exclude_becomes_skipped_excluded(client):
    c = client
    mc = _mc(c)
    jid, op_id, _ = _drive_stale_to_review(c, mc, "E-EXCL")
    writes_before = mc.get("/_mock/stats").json()["counters"].get("write_calls", 0)

    assert c.post(f"/api/jobs/{jid}/reconcile").status_code == 200
    _poll(c, jid, {"awaiting_target_review", "error"})
    _resolve_targets_to_recon_complete(c, jid, action="exclude")

    assert c.post(f"/api/jobs/{jid}/deliver").status_code == 200
    job = _poll(c, jid, {"migration_complete", "delivery_partial_failure", "error"})
    assert job["status"] == "migration_complete", job["status"]

    op = c.get(f"/api/jobs/{jid}/delivery/operations/{op_id}").json()
    assert op["status"] == "SKIPPED_EXCLUDED", op
    assert mc.get("/_mock/stats").json()["counters"].get("write_calls", 0) == writes_before
    # rollback ignores it (no succeeded ops)
    assert c.post(f"/api/jobs/{jid}/rollback").status_code == 400


def test_mixed_job_stale_terminalized_reaches_migration_complete(client):
    """One op succeeds (CREATE); another goes stale -> review -> exclude -> SKIPPED_EXCLUDED.
    Final job status is migration_complete, not stale_target_review_required/delivering."""
    c = client
    mc = _mc(c)
    # E-MIX: existing target with empty dept → fill → READY_UPDATE; E-NEW: brand new → CREATE.
    seed = {"employee_id": "E-MIX", "full_name": "Mimi Mix", "work_email": "mimi.mix@corp.example",
            "hire_date": "2020-01-01", "department": None}
    assert mc.post("/employees", json={"employee": seed, "idempotency_key": "emix"}).status_code == 201
    csv = ("Employee ID,Full Name,Work Email,Department,Hire Date\n"
           "E-NEW,Nadia New,nadia.new@corp.example,Sales,2021-02-02\n"
           "E-MIX,Mimi Mix,mimi.mix@corp.example,Sales,2020-01-01\n")
    jid = _upload(c, csv)
    _drive_recon(c, jid)
    assert mc.post("/_mock/employees/E-MIX/mutate",
                   json={"fields": {"department": "Finance"}}).status_code == 200

    assert c.post(f"/api/jobs/{jid}/deliver").status_code == 200
    _poll(c, jid, {"stale_target_review_required", "delivery_partial_failure", "error"})
    ops = {o["employee_id"]: o for o in _delivery(c, jid)["operations"]}
    assert ops["E-NEW"]["status"] == "SUCCEEDED"
    assert ops["E-MIX"]["status"] == "STALE_TARGET"

    assert c.post(f"/api/jobs/{jid}/reconcile").status_code == 200
    _poll(c, jid, {"awaiting_target_review", "error"})
    _resolve_targets_to_recon_complete(c, jid, action="exclude")

    assert c.post(f"/api/jobs/{jid}/deliver").status_code == 200
    job = _poll(c, jid, {"migration_complete", "delivery_partial_failure",
                         "stale_target_review_required", "error"})
    assert job["status"] == "migration_complete", job["status"]
    ops = {o["employee_id"]: o for o in _delivery(c, jid)["operations"]}
    assert ops["E-NEW"]["status"] == "SUCCEEDED"
    assert ops["E-MIX"]["status"] == "SKIPPED_EXCLUDED"


def test_handle_stale_target_immediate_excluded_not_failed(manual_client):
    """handle_stale_target: an immediate re-reconciliation that returns EXCLUDED must terminalize
    the op as SKIPPED_EXCLUDED (no write needed), never FAILED."""
    c = manual_client
    mc = _mc(c)
    ctx = c.app.state.ctx
    db = ctx.db

    seed = {"employee_id": "E-IMEX", "full_name": "Ivy Imex", "work_email": "ivy.imex@corp.example",
            "hire_date": "2020-01-01", "department": None}
    assert mc.post("/employees", json={"employee": seed, "idempotency_key": "eimex"}).status_code == 201
    csv = ("Employee ID,Full Name,Work Email,Department,Hire Date\n"
           "E-IMEX,Ivy Imex,ivy.imex@corp.example,Sales,2020-01-01\n")
    jid = _upload(c, csv)

    # Drive the pipeline with the worker pool OFF by running the stages inline through the pool object.
    async def _drive():
        # ingest + map + prepare + reconcile by pumping the queue with a temporary worker id.
        pool = ctx.workers
        for _ in range(200):
            item = db.claim_next_work("t-worker", lease_seconds=60)
            if item is None:
                # resolve any gate then continue
                job = db.get_job(jid)
                stt = job["status"]
                if stt == "blocked_provider":
                    for p in db.get_custom_field_proposals(jid):
                        if p["status"] == "open":
                            db.resolve_custom_field_proposal_and_enqueue(
                                jid, p["id"], expected_version=p["version"],
                                resolution={"action": "ignore", "actor": "t"})
                    continue
                if stt in ("preparation_complete",):
                    db.enqueue_work(job_id=jid, kind="TARGET_RECONCILE",
                                    idempotency_key=f"{jid}:reconcile")
                    continue
                if stt in ("reconciliation_complete", "awaiting_target_review", "error"):
                    break
                await asyncio.sleep(0)
                continue
            await pool._process(item, "t-worker")
        return db.get_job(jid)["status"]

    status = _arun(_drive())
    assert status == "reconciliation_complete", status

    recon = {r["candidate_id"]: r for r in db.get_target_reconciliation(jid)}
    cid = next(iter(recon))
    assert recon[cid]["outcome"] == "READY_UPDATE"

    # Plan a delivery op, then mark the candidate excluded (a resolved exclude overlay) so the
    # immediate re-reconciliation inside handle_stale_target returns EXCLUDED.
    eff = ctx.effective_schema_for_job(jid)
    from app.delivery import plan_delivery, handle_stale_target
    plan_delivery(db, jid, eff)
    op = db.get_delivery_operation_for_candidate(jid, cid)
    assert op and op["op_type"] == "UPDATE"
    db.upsert_target_review_issue(jid, issue_id="imex-iss", candidate_id=cid,
                                  business_key=op["employee_id"], field="department",
                                  issue_type="scalar_conflict", reason="t", incoming_value="Sales",
                                  target_value="Finance", match_basis="employee_id",
                                  options=["keep_existing", "use_incoming", "exclude"], affected={})
    db._conn.execute("UPDATE target_review_issues SET status='resolved', resolution=? WHERE id='imex-iss'",
                     (json.dumps({"action": "exclude"}),))
    db._conn.commit()
    # bump the target revision so the op is genuinely stale on refetch
    mc.post("/_mock/employees/E-IMEX/mutate", json={"fields": {"designation": "Lead"}})

    result = _arun(
        handle_stale_target(db, ctx.gateway, jid, op, eff))
    assert result == "excluded", result
    refreshed = db.get_delivery_operation(op["id"])
    assert refreshed["status"] == "SKIPPED_EXCLUDED", refreshed["status"]


# ===========================================================================
# Item 3 — retry exhaustion terminalizes op + job (delivery and rollback)
# ===========================================================================

def test_delivery_retry_exhaustion_terminalizes(fast_retry_client):
    c = fast_retry_client
    mc = _mc(c)
    mc.post("/_mock/failures", json={"mode": "status", "status": 500, "count": 500, "scope": "create"})
    csv = ("Employee ID,Full Name,Work Email,Department,Hire Date\n"
           "E-EXH,Xander Exhaust,xander.exh@corp.example,Sales,2021-01-01\n")
    jid = _upload(c, csv)
    _drive_recon(c, jid)
    assert c.post(f"/api/jobs/{jid}/deliver").status_code == 200

    job = _poll(c, jid, {"delivery_partial_failure", "error", "migration_complete"})
    assert job["status"] in ("delivery_partial_failure", "error"), job["status"]
    op = _op_by_candidate(c, jid, "CREATE")
    assert op["status"] == "FAILED", op
    assert (op.get("last_error") or "").startswith("retry_exhausted:"), op.get("last_error")
    # no pending/retryable work remains for the op
    counts = {r["status"]: r["c"] for r in []}  # placeholder, replaced below
    remaining = _pending_deliver_work(c)
    assert remaining == 0, f"expected no claimable DELIVER_OP work, found {remaining}"

    # Manual retry IS allowed because the last underlying failure was retryable (500). It creates a
    # fresh generation; clear the fault so it now succeeds.
    mc.delete("/_mock/failures")
    r = c.post(f"/api/jobs/{jid}/delivery/retry", json={"operation_id": op["id"]})
    assert r.status_code == 200, r.text
    job = _poll(c, jid, {"migration_complete", "delivery_partial_failure", "error"})
    assert job["status"] == "migration_complete", job["status"]
    assert c.get(f"/api/jobs/{jid}/delivery/operations/{op['id']}").json()["status"] == "SUCCEEDED"


def _pending_deliver_work(c):
    ctx = c.app.state.ctx
    rows = ctx.db._fetchall(
        "SELECT COUNT(*) AS n FROM work_items WHERE kind='DELIVER_OP' AND status IN ('pending','retryable')")
    return rows[0]["n"] if rows else 0


def test_rollback_retry_exhaustion_terminalizes(fast_retry_client):
    c = fast_retry_client
    mc = _mc(c)
    csv = ("Employee ID,Full Name,Work Email,Department,Hire Date\n"
           "E-RBX,Rob Rollbackx,rob.rbx@corp.example,Sales,2021-01-01\n")
    jid = _upload(c, csv)
    _drive_recon(c, jid)
    assert c.post(f"/api/jobs/{jid}/deliver").status_code == 200
    job = _poll(c, jid, {"migration_complete", "delivery_partial_failure", "error"})
    assert job["status"] == "migration_complete", job["status"]
    op = _op_by_candidate(c, jid, "CREATE")
    assert op["status"] == "SUCCEEDED"

    # Rollback of a CREATE = compensating DELETE; make DELETE fail retryably past the budget.
    mc.post("/_mock/failures", json={"mode": "status", "status": 500, "count": 500, "scope": "delete"})
    assert c.post(f"/api/jobs/{jid}/rollback").status_code == 200
    job = _poll(c, jid, {"rollback_partial_failure", "rollback_complete", "error"})
    assert job["status"] == "rollback_partial_failure", job["status"]
    op = c.get(f"/api/jobs/{jid}/delivery/operations/{op['id']}").json()
    assert op["status"] == "ROLLBACK_FAILED", op
    assert (op.get("last_error") or "").startswith("retry_exhausted:"), op.get("last_error")


# ===========================================================================
# Item 4 — manual retry policy from persisted attempt retryability
# ===========================================================================

def _make_op(db, jid, *, status, op_type="CREATE", last_retryable=None, category=None, http=None):
    cid = f"c-{uuid.uuid4().hex[:8]}"
    op = db.create_delivery_operation(
        job_id=jid, candidate_id=cid, employee_id="E1", op_type=op_type,
        payload={"employee_id": "E1", "full_name": "n", "work_email": "e@x.com", "hire_date": "2020-01-01"},
        expected_target_revision=(1 if op_type == "UPDATE" else None),
        target_record_id=("E1" if op_type == "UPDATE" else None),
        before_snapshot=None, desired_version_id=None,
        idempotency_key=f"deliver:{jid}:{cid}:{op_type.lower()}")
    db.update_delivery_operation(op["id"], status=status)
    if last_retryable is not None:
        aid = db.add_delivery_attempt(operation_id=op["id"], attempt_no=1, action="DELIVER")
        db.complete_delivery_attempt(aid, http_status=http, retryable=last_retryable,
                                     error_category=category, retry_after=None,
                                     target_request_id=None, response_meta=None,
                                     result="retryable" if last_retryable else "terminal")
    return op["id"]


def test_manual_retry_policy(manual_client):
    c = manual_client
    db = c.app.state.ctx.db
    jid = db.create_job(schema_version="1", provider="groq", model_id="m",
                        adapter_kind="fake", tenant_id="default")

    def retry(op_id):
        return c.post(f"/api/jobs/{jid}/delivery/retry", json={"operation_id": op_id})

    # ---- allowed ----
    allowed_500 = _make_op(db, jid, status="FAILED", last_retryable=True, category="server_500", http=500)
    assert retry(allowed_500).status_code == 200
    allowed_429 = _make_op(db, jid, status="FAILED", last_retryable=True, category="rate_limited", http=429)
    assert retry(allowed_429).status_code == 200
    retryable_op = _make_op(db, jid, status="RETRYABLE", last_retryable=True, category="server_503", http=503)
    assert retry(retryable_op).status_code == 200

    # ---- rejected ----
    val = _make_op(db, jid, status="FAILED", last_retryable=False, category="validation_validation_error", http=422)
    assert retry(val).status_code == 400
    auth = _make_op(db, jid, status="FAILED", last_retryable=False, category="auth_401", http=401)
    assert retry(auth).status_code == 400
    forbid = _make_op(db, jid, status="FAILED", last_retryable=False, category="auth_403", http=403)
    assert retry(forbid).status_code == 400
    exists = _make_op(db, jid, status="FAILED", last_retryable=False, category="already_exists", http=409)
    assert retry(exists).status_code == 400
    email = _make_op(db, jid, status="FAILED", last_retryable=False, category="email_in_use", http=409)
    assert retry(email).status_code == 400
    stale = _make_op(db, jid, status="STALE_TARGET", op_type="UPDATE",
                     last_retryable=False, category="revision_conflict", http=409)
    rs = retry(stale)
    assert rs.status_code == 400
    assert "stale_target" in rs.json()["detail"].lower()


# ===========================================================================
# Item 5 — operation-level execution fence (concurrency)
# ===========================================================================

def _seed_create_op(db, jid, eid, key_suffix="create"):
    cid = f"c-{eid}"
    return db.create_delivery_operation(
        job_id=jid, candidate_id=cid, employee_id=eid, op_type="CREATE",
        payload={"employee_id": eid, "full_name": f"{eid}", "work_email": f"{eid.lower()}@x.com",
                 "hire_date": "2020-01-01"},
        expected_target_revision=None, target_record_id=None, before_snapshot=None,
        desired_version_id=None, idempotency_key=f"deliver:{jid}:{cid}:{key_suffix}")


def test_operation_execution_fence_deliver(manual_client):
    c = manual_client
    mc = _mc(c)
    ctx = c.app.state.ctx
    db = ctx.db
    jid = db.create_job(schema_version="1", provider="groq", model_id="m",
                        adapter_kind="fake", tenant_id="default")
    op = _seed_create_op(db, jid, "E-FENCE")

    # Two DISTINCT due DELIVER_OP work items for the SAME operation.
    db.enqueue_work(job_id=jid, kind="DELIVER_OP", idempotency_key="wfence1",
                    payload={"operation_id": op["id"]})
    db.enqueue_work(job_id=jid, kind="DELIVER_OP", idempotency_key="wfence2",
                    payload={"operation_id": op["id"]})
    a = db.claim_next_work("worker-A", lease_seconds=60)
    b = db.claim_next_work("worker-B", lease_seconds=60)
    assert a and b and a["id"] != b["id"]

    async def _run():
        await asyncio.gather(
            ctx.workers._run_deliver_op(a, worker_id="worker-A"),
            ctx.workers._run_deliver_op(b, worker_id="worker-B"),
        )
    _arun(_run())

    # Exactly one execution: one attempt row, one real create, zero idempotency replays (the fence
    # skipped the loser BEFORE any HTTP call — not relying on target-side idempotency).
    attempts = db.get_delivery_attempts(op["id"])
    assert len(attempts) == 1, f"attempt history must not be duplicated: {attempts}"
    stats = mc.get("/_mock/stats").json()["counters"]
    assert stats.get("create_calls", 0) == 1, stats
    assert stats.get("replayed_calls", 0) == 0, "fence must skip the loser, not replay it"
    assert db.get_delivery_operation(op["id"])["status"] == "SUCCEEDED"
    assert mc.get("/employees/E-FENCE").status_code == 200


def test_operation_execution_fence_rollback(manual_client):
    c = manual_client
    mc = _mc(c)
    ctx = c.app.state.ctx
    db = ctx.db
    jid = db.create_job(schema_version="1", provider="groq", model_id="m",
                        adapter_kind="fake", tenant_id="default")
    op = _seed_create_op(db, jid, "E-RBFENCE")

    # Deliver once (single work item) so the op is SUCCEEDED.
    db.enqueue_work(job_id=jid, kind="DELIVER_OP", idempotency_key="wrb0",
                    payload={"operation_id": op["id"]})
    it = db.claim_next_work("w0", lease_seconds=60)
    _arun(ctx.workers._run_deliver_op(it, worker_id="w0"))
    assert db.get_delivery_operation(op["id"])["status"] == "SUCCEEDED"

    from app.delivery import plan_rollback
    plan_rollback(db, jid)
    db.enqueue_work(job_id=jid, kind="ROLLBACK_OP", idempotency_key="wrb1",
                    payload={"operation_id": op["id"]})
    db.enqueue_work(job_id=jid, kind="ROLLBACK_OP", idempotency_key="wrb2",
                    payload={"operation_id": op["id"]})
    a = db.claim_next_work("worker-A", lease_seconds=60)
    b = db.claim_next_work("worker-B", lease_seconds=60)
    assert a and b and a["id"] != b["id"]

    async def _run():
        await asyncio.gather(
            ctx.workers._run_rollback_op(a, worker_id="worker-A"),
            ctx.workers._run_rollback_op(b, worker_id="worker-B"),
        )
    _arun(_run())

    rb_attempts = [x for x in db.get_delivery_attempts(op["id"]) if x["action"] == "ROLLBACK"]
    assert len(rb_attempts) == 1, f"rollback attempt history must not be duplicated: {rb_attempts}"
    stats = mc.get("/_mock/stats").json()["counters"]
    assert stats.get("delete_calls", 0) == 1, stats
    assert db.get_delivery_operation(op["id"])["status"] == "ROLLED_BACK"


# ===========================================================================
# Item 6 — DB-level attempt-number uniqueness
# ===========================================================================

def test_attempt_number_unique_constraint():
    from app.db import Database
    with tempfile.TemporaryDirectory() as td:
        db = Database(os.path.join(td, "t.db"))
        jid = db.create_job(schema_version="1", provider="groq", model_id="m",
                            adapter_kind="fake", tenant_id="default")
        op = db.create_delivery_operation(
            job_id=jid, candidate_id="c1", employee_id="e1", op_type="CREATE",
            payload={"x": 1}, expected_target_revision=None, target_record_id=None,
            before_snapshot=None, desired_version_id=None, idempotency_key="k")
        db.add_delivery_attempt(operation_id=op["id"], attempt_no=1, action="DELIVER")
        with pytest.raises(sqlite3.IntegrityError):
            db.add_delivery_attempt(operation_id=op["id"], attempt_no=1, action="DELIVER")
        # a different attempt_no is fine
        db.add_delivery_attempt(operation_id=op["id"], attempt_no=2, action="DELIVER")
        assert db.delivery_attempt_count(op["id"]) == 2


# ===========================================================================
# Item 7 — backoff uses the actual attempt number
# ===========================================================================

def test_backoff_exponential_growth_and_retry_after():
    from app.delivery import compute_backoff

    class S:
        target_retry_base_seconds = 1.0
        target_retry_max_seconds = 100.0

    s = S()
    # Each attempt's delay must lie within the jittered exponential band [0.5,1.5]*base*2^(n-1).
    for n in range(1, 6):
        base_delay = min(1.0 * (2 ** (n - 1)), 100.0)
        for _ in range(20):  # jitter is random → sample several times
            d = compute_backoff(n, s)
            assert base_delay * 0.5 <= d <= base_delay * 1.5 + 1e-9, (n, d, base_delay)
    # The band strictly increases with the attempt number (no constant/off-by-one backoff).
    assert compute_backoff(1, s) <= compute_backoff(5, s) or True  # bands overlap; check upper bound:
    assert min(compute_backoff(5, s) for _ in range(50)) > max(compute_backoff(1, s) for _ in range(50)) * 0.5
    # Retry-After takes precedence (honored, capped at target_retry_max_seconds).
    d = compute_backoff(1, s, retry_after=50.0)
    assert d >= 50.0
    d_capped = compute_backoff(1, s, retry_after=999.0)
    assert d_capped <= 100.0
