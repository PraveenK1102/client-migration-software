"""M3B.3 — no-stuck delivery / stale-recovery hotfix. Behavioral tests (real pipeline + the real
WorkerPool, not source-string inspection) for each ordered item:

  1. TERMINAL stale-handling outcomes finalize the job automatically (no operator re-Deliver):
     - immediate re-reconciliation NO_CHANGE  -> SKIPPED_NO_CHANGE -> migration_complete;
     - immediate re-reconciliation EXCLUDED   -> SKIPPED_EXCLUDED  -> migration_complete;
     - re-reconciliation terminal failure     -> FAILED            -> error (never 'delivering').
  2. A TRANSIENT failure while refreshing a stale target RESUMES stale handling and NEVER re-sends
     the old revision-bound UPDATE:
     - 409 -> refetch 500 -> retry -> refetch ok -> safe next state; the old UPDATE is not re-sent;
     - 409 -> refetch 429+Retry-After -> durable retry -> resumes;
     - 409 -> refetch fails past max attempts -> op FAILED + job terminal (no stuck 'delivering');
     - 409 -> refetch non-retryable (auth 401) -> op FAILED (stale_refresh_failed) + job terminal;
     - a STALE_TARGET op with a claimable work item (the post-restart state) resumes and never
       re-sends the old update.
  3. GENERIC (unexpected, unclassified) DELIVER_OP / ROLLBACK_OP exhaustion terminalizes the op
     (FAILED / ROLLBACK_FAILED) and finalizes the job.

Core invariant asserted throughout: no job stays in an active state (delivering / rollback_in_progress
/ stale_target_review_required-without-a-real-open-review) when there is no claimable/runnable work.

Offline: GROQ_API_KEY empty (no model calls); AUTO_CONTINUE disabled so the tests drive delivery
explicitly. Target writes go only through the in-process mock target via TargetEmployeeGateway.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


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
    """Workers ON, default retry settings, AUTO_CONTINUE off (the test drives /deliver)."""
    with _mk_client(tmp_path, monkeypatch) as c:
        yield c
    from app import config
    config.get_settings.cache_clear()


@pytest.fixture
def fast_client(tmp_path, monkeypatch):
    """Workers ON with a tiny backoff so retries/resumes happen quickly. Full default retry budget."""
    with _mk_client(tmp_path, monkeypatch, TARGET_RETRY_BASE_SECONDS=0.05,
                    TARGET_RETRY_MAX_SECONDS=0.2) as c:
        yield c
    from app import config
    config.get_settings.cache_clear()


@pytest.fixture
def fast_exhaust_client(tmp_path, monkeypatch):
    """Workers ON, tiny backoff + small retry budget so exhaustion happens quickly."""
    with _mk_client(tmp_path, monkeypatch, TARGET_MAX_ATTEMPTS=3,
                    TARGET_RETRY_BASE_SECONDS=0.05, TARGET_RETRY_MAX_SECONDS=0.2) as c:
        yield c
    from app import config
    config.get_settings.cache_clear()


@pytest.fixture
def manual_client(tmp_path, monkeypatch):
    """Workers OFF — the test drives WorkerPool methods directly."""
    with _mk_client(tmp_path, monkeypatch, START_WORKERS="false") as c:
        yield c
    from app import config
    config.get_settings.cache_clear()


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
    """Drive to reconciliation_complete, resolving any review with an approve/first-option pick."""
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
    for _ in range(400):
        job = _poll(c, jid, {"reconciliation_complete", "awaiting_target_review", "error"})
        if job["status"] in ("reconciliation_complete", "error"):
            break
        for i in c.get(f"/api/jobs/{jid}/target-reviews?status=open").json():
            c.post(f"/api/jobs/{jid}/target-reviews/{i['id']}/decision",
                   json={"action": "use_incoming", "version": i["version"], "reason": "t"})
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


def _attempts(c, jid, op_id):
    return c.get(f"/api/jobs/{jid}/delivery/operations/{op_id}/attempts").json()


def _audit_events(c, jid):
    a = c.get(f"/api/jobs/{jid}/audit").json()
    rows = a if isinstance(a, list) else a.get("events", [])
    return {r["event_type"] for r in rows}


def _work_counts(c, kind):
    ctx = c.app.state.ctx
    rows = ctx.db._fetchall(
        "SELECT COUNT(*) AS n FROM work_items WHERE kind=? AND status IN ('pending','retryable','processing')",
        (kind,))
    return rows[0]["n"] if rows else 0


def _writes(mc):
    return mc.get("/_mock/stats").json()["counters"].get("write_calls", 0)


def _seed_update_target(mc, eid, *, department=None):
    """Seed an existing target row (rev1) with an empty department so an incoming record is a
    READY_UPDATE that fills it. Returns nothing; asserts the seed took."""
    seed = {"employee_id": eid, "full_name": f"{eid} Person",
            "work_email": f"{eid.lower()}@corp.example", "hire_date": "2020-01-01",
            "department": department}
    assert mc.post("/employees", json={"employee": seed,
                                       "idempotency_key": f"{eid}-seed"}).status_code == 201


def _incoming_csv(eid, department="Sales"):
    return ("Employee ID,Full Name,Work Email,Department,Hire Date\n"
            f"{eid},{eid} Person,{eid.lower()}@corp.example,{department},2020-01-01\n")


# ===========================================================================
# 1. TERMINAL STALE-HANDLING OUTCOMES FINALIZE THE JOB (no operator re-Deliver)
# ===========================================================================

def test_stale_immediate_no_change_finalizes_migration_complete(client):
    """409 -> immediate re-reconciliation NO_CHANGE -> SKIPPED_NO_CHANGE -> migration_complete,
    with ZERO additional migration write and no second /deliver call."""
    c = client
    mc = _mc(c)
    _seed_update_target(mc, "E-NC1")
    jid = _upload(c, _incoming_csv("E-NC1", "Sales"))
    _drive_recon(c, jid)

    # External change makes the target EQUAL to the incoming record (dept Sales @ rev2), so the
    # immediate re-reconciliation after the 409 finds nothing to write.
    assert mc.post("/_mock/employees/E-NC1/mutate",
                   json={"fields": {"department": "Sales"}}).status_code == 200
    writes_before = _writes(mc)

    assert c.post(f"/api/jobs/{jid}/deliver").status_code == 200
    job = _poll(c, jid, {"migration_complete", "delivery_partial_failure", "error"})
    assert job["status"] == "migration_complete", job["status"]

    op = _op_by_candidate(c, jid, "UPDATE")
    assert op["status"] == "SKIPPED_NO_CHANGE", op
    # the old revision-bound UPDATE was never re-sent: exactly one DELIVER attempt, a 409, no write
    atts = [a for a in _attempts(c, jid, op["id"]) if a["action"] == "DELIVER"]
    assert len(atts) == 1 and atts[0]["http_status"] == 409, atts
    assert _writes(mc) == writes_before, "no target write may occur for an immediate NO_CHANGE stale"
    assert _work_counts(c, "DELIVER_OP") == 0, "no claimable delivery work may remain"
    assert "stale_target_detected" in _audit_events(c, jid)


def test_stale_immediate_excluded_finalizes_migration_complete(client):
    """409 -> immediate re-reconciliation EXCLUDED (a resolved exclude overlay) -> SKIPPED_EXCLUDED
    -> migration_complete, no write, no second /deliver call."""
    c = client
    mc = _mc(c)
    db = c.app.state.ctx.db
    _seed_update_target(mc, "E-EX1")
    jid = _upload(c, _incoming_csv("E-EX1", "Sales"))
    _drive_recon(c, jid)

    # Plant a RESOLVED exclude decision for this candidate so re-reconciliation excludes it.
    cid = db.get_target_reconciliation(jid)[0]["candidate_id"]
    db.upsert_target_review_issue(jid, issue_id="ex1-iss", candidate_id=cid, business_key="E-EX1",
                                  field="department", issue_type="scalar_conflict", reason="t",
                                  incoming_value="Sales", target_value="Finance",
                                  match_basis="employee_id",
                                  options=["keep_existing", "use_incoming", "exclude"], affected={})
    db._conn.execute("UPDATE target_review_issues SET status='resolved', resolution=? WHERE id='ex1-iss'",
                     (json.dumps({"action": "exclude"}),))
    db._conn.commit()
    # bump the target revision so the planned UPDATE @ rev1 is genuinely stale
    assert mc.post("/_mock/employees/E-EX1/mutate",
                   json={"fields": {"designation": "Lead"}}).status_code == 200
    writes_before = _writes(mc)

    assert c.post(f"/api/jobs/{jid}/deliver").status_code == 200
    job = _poll(c, jid, {"migration_complete", "delivery_partial_failure", "error"})
    assert job["status"] == "migration_complete", job["status"]

    op = _op_by_candidate(c, jid, "UPDATE")
    assert op["status"] == "SKIPPED_EXCLUDED", op
    assert _writes(mc) == writes_before
    assert _work_counts(c, "DELIVER_OP") == 0


def test_stale_reconcile_failure_finalizes_error(client, monkeypatch):
    """409 -> refetch succeeds -> re-reconciliation raises -> op FAILED and the job becomes a terminal
    failure (error), never left in 'delivering'."""
    c = client
    mc = _mc(c)
    _seed_update_target(mc, "E-RCF")
    jid = _upload(c, _incoming_csv("E-RCF", "Sales"))
    _drive_recon(c, jid)
    assert mc.post("/_mock/employees/E-RCF/mutate",
                   json={"fields": {"department": "Finance"}}).status_code == 200

    def _boom(*a, **k):
        raise RuntimeError("forced re-reconciliation failure")
    # handle_stale_target imports reconcile_single from the module at call time.
    monkeypatch.setattr("app.reconcile_target.reconcile_single", _boom)

    assert c.post(f"/api/jobs/{jid}/deliver").status_code == 200
    job = _poll(c, jid, {"error", "delivery_partial_failure", "migration_complete"})
    assert job["status"] == "error", job["status"]
    op = _op_by_candidate(c, jid, "UPDATE")
    assert op["status"] == "FAILED", op
    assert "re-reconciliation failed" in (op.get("last_error") or ""), op.get("last_error")
    assert _work_counts(c, "DELIVER_OP") == 0


# ===========================================================================
# 2. RESUMABLE STALE REFRESH — never re-send the old revision-bound UPDATE
# ===========================================================================

def test_stale_refresh_transient_500_resumes_no_resend(fast_client):
    """409 -> the stale refetch GET fails 500 once -> durable retry -> refetch succeeds -> re-reconcile
    -> safe terminal (NO_CHANGE here). The old UPDATE is NEVER re-sent."""
    c = fast_client
    mc = _mc(c)
    _seed_update_target(mc, "E-SR1")
    jid = _upload(c, _incoming_csv("E-SR1", "Sales"))
    _drive_recon(c, jid)
    assert mc.post("/_mock/employees/E-SR1/mutate",
                   json={"fields": {"department": "Sales"}}).status_code == 200  # -> eventual NO_CHANGE
    writes_before = _writes(mc)
    # Fail the FIRST stale-refetch GET only (read scope is opt-in; not matched by 'any').
    assert mc.post("/_mock/failures",
                   json={"mode": "status", "status": 500, "count": 1, "scope": "read"}).status_code == 200

    assert c.post(f"/api/jobs/{jid}/deliver").status_code == 200
    job = _poll(c, jid, {"migration_complete", "delivery_partial_failure", "error"})
    assert job["status"] == "migration_complete", job["status"]

    op = _op_by_candidate(c, jid, "UPDATE")
    assert op["status"] == "SKIPPED_NO_CHANGE", op
    ev = _audit_events(c, jid)
    assert "stale_refresh_retry_scheduled" in ev, ev
    assert "stale_target_refetched" in ev, "the refetch must eventually succeed and re-reconcile"
    # never re-sent the stale rev-1 UPDATE: one DELIVER attempt (the 409), no successful write
    atts = [a for a in _attempts(c, jid, op["id"]) if a["action"] == "DELIVER"]
    assert len(atts) == 1 and atts[0]["http_status"] == 409, atts
    assert _writes(mc) == writes_before
    assert _work_counts(c, "DELIVER_OP") == 0


def test_stale_refresh_429_retry_after_resumes(fast_client):
    """409 -> stale refetch GET returns 429 with Retry-After -> durable retry honours it -> resume."""
    c = fast_client
    mc = _mc(c)
    _seed_update_target(mc, "E-SR429")
    jid = _upload(c, _incoming_csv("E-SR429", "Sales"))
    _drive_recon(c, jid)
    assert mc.post("/_mock/employees/E-SR429/mutate",
                   json={"fields": {"department": "Sales"}}).status_code == 200
    writes_before = _writes(mc)
    assert mc.post("/_mock/failures",
                   json={"mode": "status", "status": 429, "count": 1, "scope": "read",
                         "retry_after": 1}).status_code == 200

    assert c.post(f"/api/jobs/{jid}/deliver").status_code == 200
    job = _poll(c, jid, {"migration_complete", "delivery_partial_failure", "error"}, tries=8000)
    assert job["status"] == "migration_complete", job["status"]

    op = _op_by_candidate(c, jid, "UPDATE")
    assert op["status"] == "SKIPPED_NO_CHANGE", op
    # a rate_limited stale refresh retry was scheduled
    aud = c.get(f"/api/jobs/{jid}/audit").json()
    rows = aud if isinstance(aud, list) else aud.get("events", [])
    # The API already decodes `after` to a dict (AuditOut); it is no longer double-JSON-encoded
    # (that was itself a bug this round fixed — see delivery.py/worker.py after=json.dumps() removal).
    sched = [r["after"] for r in rows
             if r["event_type"] == "stale_refresh_retry_scheduled" and r.get("after")]
    assert any(s.get("category") == "rate_limited" for s in sched), sched
    assert _writes(mc) == writes_before
    assert _work_counts(c, "DELIVER_OP") == 0


def test_stale_refresh_exhausted_terminalizes_no_stuck(fast_exhaust_client):
    """409 -> stale refetch GET keeps failing 500 past the retry budget -> op FAILED
    (stale_refresh_retry_exhausted) and the job terminalizes; never stuck in 'delivering'."""
    c = fast_exhaust_client
    mc = _mc(c)
    _seed_update_target(mc, "E-SRX")
    jid = _upload(c, _incoming_csv("E-SRX", "Sales"))
    _drive_recon(c, jid)
    assert mc.post("/_mock/employees/E-SRX/mutate",
                   json={"fields": {"department": "Finance"}}).status_code == 200
    writes_before = _writes(mc)
    assert mc.post("/_mock/failures",
                   json={"mode": "status", "status": 500, "count": 500, "scope": "read"}).status_code == 200

    assert c.post(f"/api/jobs/{jid}/deliver").status_code == 200
    job = _poll(c, jid, {"error", "delivery_partial_failure", "migration_complete"})
    assert job["status"] == "error", job["status"]

    op = _op_by_candidate(c, jid, "UPDATE")
    assert op["status"] == "FAILED", op
    assert (op.get("last_error") or "").startswith("stale_refresh_retry_exhausted:"), op.get("last_error")
    assert "stale_refresh_retry_exhausted" in _audit_events(c, jid)
    assert _writes(mc) == writes_before, "a failed refetch must never produce a target write"
    assert _work_counts(c, "DELIVER_OP") == 0, "no claimable delivery work may remain"


def test_stale_refresh_non_retryable_read_terminalizes(fast_client):
    """409 -> stale refetch GET returns a NON-retryable read error (auth 401) -> op FAILED
    (stale_refresh_failed), no blind retry, job terminalizes."""
    c = fast_client
    mc = _mc(c)
    _seed_update_target(mc, "E-SR401")
    jid = _upload(c, _incoming_csv("E-SR401", "Sales"))
    _drive_recon(c, jid)
    assert mc.post("/_mock/employees/E-SR401/mutate",
                   json={"fields": {"department": "Finance"}}).status_code == 200
    writes_before = _writes(mc)
    assert mc.post("/_mock/failures",
                   json={"mode": "status", "status": 401, "count": 500, "scope": "read"}).status_code == 200

    assert c.post(f"/api/jobs/{jid}/deliver").status_code == 200
    job = _poll(c, jid, {"error", "delivery_partial_failure", "migration_complete"})
    assert job["status"] == "error", job["status"]

    op = _op_by_candidate(c, jid, "UPDATE")
    assert op["status"] == "FAILED", op
    assert (op.get("last_error") or "").startswith("stale_refresh_failed:auth_401"), op.get("last_error")
    assert "stale_refresh_failed" in _audit_events(c, jid)
    assert _writes(mc) == writes_before
    assert _work_counts(c, "DELIVER_OP") == 0


def test_stale_target_op_resumes_on_reclaim_no_resend(manual_client):
    """Restart-shaped state: a STALE_TARGET op with a claimable DELIVER_OP work item (what remains
    after a transient refetch reschedule + process restart) must RESUME stale handling on that work
    item — refetch -> reconcile -> terminalize — and must NEVER re-execute the old update."""
    import asyncio

    c = manual_client
    mc = _mc(c)
    ctx = c.app.state.ctx
    db = ctx.db
    _seed_update_target(mc, "E-RSM")
    jid = _upload(c, _incoming_csv("E-RSM", "Sales"))

    # Drive ingest->map->prepare->reconcile inline via the pool with workers off. A core-only CSV
    # resolves deterministically (no provider proposals, no review gates), so no gate handling here.
    async def _drive():
        pool = ctx.workers
        for _ in range(300):
            item = db.claim_next_work("t", lease_seconds=60)
            if item is None:
                st = db.get_job(jid)["status"]
                if st == "preparation_complete":
                    db.enqueue_work(job_id=jid, kind="TARGET_RECONCILE", idempotency_key=f"{jid}:reconcile")
                    continue
                if st in ("reconciliation_complete", "awaiting_target_review", "error"):
                    break
                await asyncio.sleep(0)
                continue
            await pool._process(item, "t")
        return db.get_job(jid)["status"]

    def _arun(coro):
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    assert _arun(_drive()) == "reconciliation_complete"

    from app.delivery import plan_delivery
    eff = ctx.effective_schema_for_job(jid)
    plan_delivery(db, jid, eff)
    cid = db.get_target_reconciliation(jid)[0]["candidate_id"]
    op = db.get_delivery_operation_for_candidate(jid, cid)
    assert op["op_type"] == "UPDATE"

    # Make the target equal to incoming so the resumed refetch re-reconciles to NO_CHANGE.
    assert mc.post("/_mock/employees/E-RSM/mutate", json={"fields": {"department": "Sales"}}).status_code == 200

    # Simulate the post-restart durable state: op is STALE_TARGET and a claimable DELIVER_OP work
    # item is bound to it (as if a 409 happened then a transient refetch rescheduled the item).
    db.enqueue_work(job_id=jid, kind="DELIVER_OP", idempotency_key="resume-wi",
                    payload={"operation_id": op["id"]})
    it = db.claim_next_work("w-resume", lease_seconds=60)
    db.update_delivery_operation(op["id"], status="STALE_TARGET", work_item_id=it["id"])
    writes_before = _writes(mc)

    _arun(ctx.workers._run_deliver_op(it, worker_id="w-resume"))

    refreshed = db.get_delivery_operation(op["id"])
    assert refreshed["status"] == "SKIPPED_NO_CHANGE", refreshed["status"]
    # resume performed NO write and created NO delivery attempt (the refetch is a read, not a write)
    assert _writes(mc) == writes_before, "resume must never re-send the old update"
    assert db.delivery_attempt_count(op["id"]) == 0
    assert db.get_job(jid)["status"] == "migration_complete"


# ===========================================================================
# 3. GENERIC (unexpected) DELIVER/ROLLBACK WORK EXHAUSTION TERMINALIZES OP + JOB
# ===========================================================================

def test_generic_delivery_exception_exhaustion_terminalizes(fast_exhaust_client, monkeypatch):
    """An UNEXPECTED exception in delivery execution that exhausts the durable work budget via the
    generic failure path must terminalize the op FAILED and finalize the job (never stuck)."""
    c = fast_exhaust_client
    mc = _mc(c)

    async def _boom(*a, **k):
        raise RuntimeError("unexpected delivery explosion")
    monkeypatch.setattr("app.worker.execute_delivery_operation", _boom)

    jid = _upload(c, _incoming_csv("E-GEN", "Sales"))  # brand-new employee -> CREATE
    _drive_recon(c, jid)
    assert c.post(f"/api/jobs/{jid}/deliver").status_code == 200

    job = _poll(c, jid, {"error", "delivery_partial_failure", "migration_complete"})
    assert job["status"] == "error", job["status"]
    op = _op_by_candidate(c, jid, "CREATE")
    assert op["status"] == "FAILED", op
    assert (op.get("last_error") or "").startswith("work_exhausted:"), op.get("last_error")
    assert "delivery_work_exhausted" in _audit_events(c, jid)
    assert _work_counts(c, "DELIVER_OP") == 0, "no claimable delivery work may remain"


def test_generic_rollback_exception_exhaustion_terminalizes(fast_exhaust_client, monkeypatch):
    """An UNEXPECTED exception in rollback execution that exhausts the budget must terminalize the op
    ROLLBACK_FAILED and finalize the job as rollback_partial_failure (never stuck)."""
    c = fast_exhaust_client
    mc = _mc(c)
    jid = _upload(c, _incoming_csv("E-RBGEN", "Sales"))  # CREATE
    _drive_recon(c, jid)
    assert c.post(f"/api/jobs/{jid}/deliver").status_code == 200
    job = _poll(c, jid, {"migration_complete", "delivery_partial_failure", "error"})
    assert job["status"] == "migration_complete", job["status"]
    op = _op_by_candidate(c, jid, "CREATE")
    assert op["status"] == "SUCCEEDED"

    # Now break rollback execution with an unexpected exception (after a successful delivery).
    async def _boom(*a, **k):
        raise RuntimeError("unexpected rollback explosion")
    monkeypatch.setattr("app.worker.execute_rollback_operation", _boom)

    assert c.post(f"/api/jobs/{jid}/rollback").status_code == 200
    job = _poll(c, jid, {"rollback_partial_failure", "rollback_complete", "error"})
    assert job["status"] == "rollback_partial_failure", job["status"]
    op = c.get(f"/api/jobs/{jid}/delivery/operations/{op['id']}").json()
    assert op["status"] == "ROLLBACK_FAILED", op
    assert (op.get("last_error") or "").startswith("work_exhausted:"), op.get("last_error")
    assert "rollback_work_exhausted" in _audit_events(c, jid)
    assert _work_counts(c, "ROLLBACK_OP") == 0, "no claimable rollback work may remain"
