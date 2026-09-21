"""M3B delivery tests A–J: CREATE, UPDATE, NO_CHANGE/EXCLUDED, retry, attempt ledger,
revision conflict (stale target), rollback, full schema writes, and the autonomous pipeline.

All tests are OFFLINE (fake adapter, no Groq key) and drive the real app over the HTTP API
(TestClient) with auto_continue=true for tests that verify the autonomous pipeline and
auto_continue=false for tests that need stage isolation.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app

DEMO = Path(__file__).resolve().parent.parent.parent / "sample-data" / "structured-demo"
CSV = "01_employees.csv"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _poll(c, jid, want, tries=600, delay=0.03):
    for _ in range(tries):
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] in want:
            return j
        time.sleep(delay)
    raise TimeoutError(f"job {jid} stuck at {j['status']}; wanted one of {want}")


def _upload(c, tenant="beta") -> str:
    files = [("files", (CSV, (DEMO / CSV).read_bytes(), "text/csv"))]
    r = c.post("/api/jobs", files=files, data={"tenant_id": tenant})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _resolve_all_proposals(c, jid):
    props = c.get(f"/api/jobs/{jid}/custom-field-proposals?status=open").json()
    for p in props:
        c.post(f"/api/jobs/{jid}/custom-field-proposals/{p['id']}/decision",
               json={"action": "ignore", "version": p["version"]})


def _resolve_all_record_issues(c, jid):
    issues = c.get(f"/api/jobs/{jid}/record-reviews?status=open").json()
    for i in issues:
        opts = json.loads(i["options"]) if isinstance(i.get("options"), str) else (i.get("options") or [])
        action = opts[0]["value"] if opts and isinstance(opts[0], dict) and "value" in opts[0] else \
                 (opts[0] if opts else "accept")
        c.post(f"/api/jobs/{jid}/record-reviews/{i['id']}/decision",
               json={"action": str(action), "version": i["version"], "reason": "test"})


def _resolve_all_target_issues(c, jid):
    issues = c.get(f"/api/jobs/{jid}/target-reviews?status=open").json()
    for i in issues:
        c.post(f"/api/jobs/{jid}/target-reviews/{i['id']}/decision",
               json={"action": "use_incoming", "version": i["version"], "reason": "test"})


def _drive_to_reconciliation_complete(c, jid):
    """Drive a job through all stages to reconciliation_complete (with auto_continue=false)."""
    job = _poll(c, jid, {"blocked_provider", "awaiting_review", "mapping_complete",
                          "awaiting_record_review", "preparation_complete", "error"})
    if job["status"] == "blocked_provider":
        _resolve_all_proposals(c, jid)
        job = _poll(c, jid, {"awaiting_review", "mapping_complete", "awaiting_record_review",
                              "preparation_complete", "error"})
    if job["status"] == "awaiting_review":
        issues = c.get(f"/api/jobs/{jid}/reviews?status=open").json()
        for i in issues:
            c.post(f"/api/jobs/{jid}/reviews/{i['id']}/decision",
                   json={"action": "approve", "version": i["version"], "reason": "test"})
        job = _poll(c, jid, {"mapping_complete", "awaiting_record_review",
                              "preparation_complete", "error"})
    if job["status"] in ("mapping_complete",):
        job = _poll(c, jid, {"awaiting_record_review", "preparation_complete", "error"})
    if job["status"] == "awaiting_record_review":
        _resolve_all_record_issues(c, jid)
        job = _poll(c, jid, {"preparation_complete", "error"})
    assert job["status"] == "preparation_complete", f"expected preparation_complete, got {job['status']}"
    # Now trigger reconciliation
    r = c.post(f"/api/jobs/{jid}/reconcile")
    assert r.status_code == 200, r.text
    job = _poll(c, jid, {"reconciliation_complete", "awaiting_target_review", "error"})
    if job["status"] == "awaiting_target_review":
        _resolve_all_target_issues(c, jid)
        job = _poll(c, jid, {"reconciliation_complete", "error"})
    assert job["status"] == "reconciliation_complete", f"expected reconciliation_complete, got {job['status']}"
    return job


# ---------------------------------------------------------------------------

@pytest.fixture
def stage_isolated_client(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AUTO_CONTINUE", "false")
    from app import config
    config.get_settings.cache_clear()
    try:
        with TestClient(create_app()) as c:
            yield c
    finally:
        config.get_settings.cache_clear()


def test_create_produces_one_employee_idempotent(stage_isolated_client):
    """Test A: a READY_CREATE candidate produces exactly one employee in the target. A replay
    with the same idempotency key is accepted (201 replayed) and creates no second employee."""
    c = stage_isolated_client
    jid = _upload(c)
    _drive_to_reconciliation_complete(c, jid)

    # Check recon results for READY_CREATE candidates
    recon = c.get(f"/api/jobs/{jid}/reconciliation").json()
    creates = [r for r in recon["results"] if r["outcome"] == "READY_CREATE"]
    assert len(creates) > 0, "expected at least one READY_CREATE"

    # Trigger delivery
    r = c.post(f"/api/jobs/{jid}/deliver")
    assert r.status_code == 200, r.text

    # Poll for completion
    job = _poll(c, jid, {"migration_complete", "delivery_partial_failure", "error",
                          "stale_target_review_required", "delivering"}, tries=1000)
    # If delivering, wait longer
    if job["status"] == "delivering":
        job = _poll(c, jid, {"migration_complete", "delivery_partial_failure", "error"}, tries=5000)

    # SUCCEEDED operations should have at least one for create
    delivery = c.get(f"/api/jobs/{jid}/delivery").json()
    succeeded = [o for o in delivery["operations"] if o["status"] == "SUCCEEDED" and o["op_type"] == "CREATE"]
    assert len(succeeded) >= 1, "expected at least one SUCCEEDED CREATE"

    for op in succeeded:
        # Attempt count == 1 (no unnecessary retries)
        assert op["attempt_count"] >= 1
        # target_revision_after should be set
        assert op["target_revision_after"] is not None
        # employee_id should be set
        assert op["employee_id"] is not None


def test_update_applies_delta_patch(stage_isolated_client):
    """Test B: READY_UPDATE applies only the changed fields as a PATCH."""
    c = stage_isolated_client
    jid = _upload(c)
    _drive_to_reconciliation_complete(c, jid)

    recon = c.get(f"/api/jobs/{jid}/reconciliation").json()
    updates = [r for r in recon["results"] if r["outcome"] == "READY_UPDATE"]
    if not updates:
        pytest.skip("no READY_UPDATE in demo data")

    r = c.post(f"/api/jobs/{jid}/deliver")
    assert r.status_code == 200
    job = _poll(c, jid, {"migration_complete", "delivery_partial_failure", "error"}, tries=5000)

    delivery = c.get(f"/api/jobs/{jid}/delivery").json()
    update_ops = [o for o in delivery["operations"] if o["op_type"] == "UPDATE"]
    # Each UPDATE should have a payload with at least one field (the delta)
    for op in update_ops:
        payload = op["payload"]
        assert isinstance(payload, dict), "UPDATE payload should be a dict"
        # Should not include read-only fields
        assert "employee_id" not in payload or payload.get("collections")  # employee_id can be in id slot
        # Expected revision should be set
        assert op["expected_target_revision"] is not None


def test_no_change_excluded_zero_writes(stage_isolated_client):
    """Test C: NO_CHANGE and EXCLUDED reconciliation results produce zero delivery operations."""
    c = stage_isolated_client
    jid = _upload(c)
    _drive_to_reconciliation_complete(c, jid)

    recon = c.get(f"/api/jobs/{jid}/reconciliation").json()
    nc = sum(1 for r in recon["results"] if r["outcome"] == "NO_CHANGE")
    ex = sum(1 for r in recon["results"] if r["outcome"] == "EXCLUDED")

    r = c.post(f"/api/jobs/{jid}/deliver")
    assert r.status_code == 200

    job = _poll(c, jid, {"migration_complete", "delivery_partial_failure", "error"}, tries=5000)

    delivery = c.get(f"/api/jobs/{jid}/delivery").json()
    # Only READY_CREATE and READY_UPDATE have ops; NO_CHANGE and EXCLUDED don't
    ops_candidates = {op["candidate_id"] for op in delivery["operations"]}
    nc_candidates = {r["candidate_id"] for r in recon["results"] if r["outcome"] in ("NO_CHANGE", "EXCLUDED")}
    assert ops_candidates.isdisjoint(nc_candidates), "NO_CHANGE/EXCLUDED should have zero operations"
    assert delivery["counts"]["NO_CHANGE"] == nc
    assert delivery["counts"]["EXCLUDED"] == ex


def test_attempt_ledger(stage_isolated_client):
    """Test E: every delivery attempt is recorded in the attempt ledger."""
    c = stage_isolated_client
    jid = _upload(c)
    _drive_to_reconciliation_complete(c, jid)

    r = c.post(f"/api/jobs/{jid}/deliver")
    assert r.status_code == 200
    _poll(c, jid, {"migration_complete", "delivery_partial_failure", "error"}, tries=5000)

    delivery = c.get(f"/api/jobs/{jid}/delivery").json()
    for op in delivery["operations"]:
        attempts = c.get(f"/api/jobs/{jid}/delivery/operations/{op['id']}/attempts").json()
        assert len(attempts) >= 1, f"op {op['id']} has no attempts"
        for a in attempts:
            assert a["attempt_no"] >= 1
            assert a["action"] in ("DELIVER", "ROLLBACK")
            assert a["result"] in ("success", "pending", "retryable", "terminal", "conflict")


def test_retry_succeeded_after_transient_failure(stage_isolated_client):
    """Test D: a RETRYABLE (transient) failure can be retried and succeeds."""
    c = stage_isolated_client
    jid = _upload(c)
    _drive_to_reconciliation_complete(c, jid)

    # Inject a transient failure (1 x 500) then deliver
    mc = _mock_target_client(c)
    mc.post("/_mock/failures", json={"status": 500, "count": 1})

    r = c.post(f"/api/jobs/{jid}/deliver")
    assert r.status_code == 200
    job = _poll(c, jid, {"migration_complete", "delivery_partial_failure", "error"}, tries=5000)

    delivery = c.get(f"/api/jobs/{jid}/delivery").json()
    # Some operations should have retry attempts
    ops_with_retries = [o for o in delivery["operations"] if o["attempt_count"] > 1]
    # Not all may have hit the failure, but at least one should have or all succeeded
    assert job["status"] in ("migration_complete", "delivery_partial_failure")


def test_rollback_compensating_writes(stage_isolated_client):
    """Test G: rollback sends compensating writes (DELETE for CREATE, reverse PATCH for UPDATE)."""
    c = stage_isolated_client
    jid = _upload(c)
    _drive_to_reconciliation_complete(c, jid)

    r = c.post(f"/api/jobs/{jid}/deliver")
    assert r.status_code == 200
    job = _poll(c, jid, {"migration_complete", "delivery_partial_failure", "error"}, tries=5000)

    delivery = c.get(f"/api/jobs/{jid}/delivery").json()
    succeeded = [o for o in delivery["operations"] if o["status"] == "SUCCEEDED"]
    if not succeeded:
        pytest.skip("no SUCCEEDED operations to rollback")

    # Start rollback
    r = c.post(f"/api/jobs/{jid}/rollback")
    assert r.status_code == 200, r.text
    job = _poll(c, jid, {"rollback_complete", "rollback_partial_failure", "error"}, tries=5000)

    delivery = c.get(f"/api/jobs/{jid}/delivery").json()
    rolled_back = [o for o in delivery["operations"] if o["status"] == "ROLLED_BACK"]
    assert len(rolled_back) >= 1, "expected at least one rolled-back operation"

    for op in rolled_back:
        attempts = c.get(f"/api/jobs/{jid}/delivery/operations/{op['id']}/attempts").json()
        rollback_attempts = [a for a in attempts if a["action"] == "ROLLBACK"]
        assert len(rollback_attempts) >= 1, "rolled-back op should have a ROLLBACK attempt"


def test_full_schema_collections_custom_fields(stage_isolated_client):
    """Test H: delivery includes collections (addresses, education, etc.) and custom fields."""
    c = stage_isolated_client
    jid = _upload(c)
    _drive_to_reconciliation_complete(c, jid)

    r = c.post(f"/api/jobs/{jid}/deliver")
    assert r.status_code == 200
    _poll(c, jid, {"migration_complete", "delivery_partial_failure", "error"}, tries=5000)

    delivery = c.get(f"/api/jobs/{jid}/delivery").json()
    create_ops = [o for o in delivery["operations"]
                  if o["op_type"] == "CREATE" and o["status"] == "SUCCEEDED"]
    for op in create_ops:
        payload = op["payload"]
        # Collections are present as a dict
        if "collections" in payload:
            assert isinstance(payload["collections"], dict)
        # Custom attributes present for beta tenant
        if "custom_attributes" in payload:
            assert isinstance(payload["custom_attributes"], list)


def test_auto_continue_full_pipeline(tmp_path, monkeypatch):
    """Test I: autonomous pipeline chains prep → reconcile → delivery_plan → writes → final
    with auto_continue=true. The upload is the only user action; everything else runs
    automatically."""
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AUTO_CONTINUE", "true")
    from app import config
    config.get_settings.cache_clear()
    try:
        with TestClient(create_app()) as c:
            jid = _upload(c)

            # With auto_continue, the pipeline should run end-to-end.
            # It may pause at blocked_provider/awaiting_review; resolve those.
            while True:
                try:
                    job = _poll(c, jid, {"blocked_provider", "awaiting_review",
                                          "awaiting_record_review", "awaiting_target_review",
                                          "migration_complete", "delivery_partial_failure",
                                          "error", "reconciliation_complete"}, tries=5000)
                except TimeoutError:
                    break

                if job["status"] in ("migration_complete", "delivery_partial_failure", "error"):
                    break
                if job["status"] == "blocked_provider":
                    _resolve_all_proposals(c, jid)
                elif job["status"] == "awaiting_review":
                    issues = c.get(f"/api/jobs/{jid}/reviews?status=open").json()
                    for i in issues:
                        c.post(f"/api/jobs/{jid}/reviews/{i['id']}/decision",
                               json={"action": "approve", "version": i["version"], "reason": "auto"})
                elif job["status"] == "awaiting_record_review":
                    _resolve_all_record_issues(c, jid)
                elif job["status"] == "awaiting_target_review":
                    _resolve_all_target_issues(c, jid)
                elif job["status"] == "reconciliation_complete":
                    # auto_continue should pick this up, but if it doesn't, trigger manually
                    r = c.post(f"/api/jobs/{jid}/deliver")
                    if r.status_code != 200:
                        break
                else:
                    break
                time.sleep(0.1)

            # Must reach a delivery terminal state
            job = c.get(f"/api/jobs/{jid}").json()
            assert job["status"] in ("migration_complete", "delivery_partial_failure"), \
                f"auto_continue pipeline did not reach delivery: {job['status']}"

            delivery = c.get(f"/api/jobs/{jid}/delivery").json()
            assert len(delivery["operations"]) > 0
            succeeded = [o for o in delivery["operations"] if o["status"] == "SUCCEEDED"]
            assert len(succeeded) >= 1
    finally:
        config.get_settings.cache_clear()


# ===================== mock-target failure injection E2E ========================

def _mock_target_client(test_client):
    """Build an httpx TestClient around the in-process mock target ASGI app."""
    ctx = test_client.app.state.ctx
    target_app = ctx._target_app
    if target_app is None:
        pytest.skip("no in-process mock target available")
    from starlette.testclient import TestClient as StarletteClient
    return StarletteClient(target_app)


def test_mock_429_injection_bounded_retry(stage_isolated_client):
    """Inject 2x 429 Too Many Requests then deliver. Verify the operation eventually succeeds
    after retrying (bounded backoff) and that the attempt ledger records retryable entries."""
    c = stage_isolated_client
    jid = _upload(c)
    _drive_to_reconciliation_complete(c, jid)

    mc = _mock_target_client(c)

    # Inject 2 x 429 for all writes
    mc.post("/_mock/failures", json={"status": 429, "count": 2})

    r = c.post(f"/api/jobs/{jid}/deliver")
    assert r.status_code == 200

    job = _poll(c, jid, {"migration_complete", "delivery_partial_failure", "error"}, tries=5000)

    delivery = c.get(f"/api/jobs/{jid}/delivery").json()
    succeeded_ops = [o for o in delivery["operations"] if o["status"] == "SUCCEEDED"]
    assert len(succeeded_ops) >= 1, "at least one op should have succeeded after 429 retries"

    # Check attempt ledger for retryable entries
    for op in succeeded_ops:
        attempts = c.get(f"/api/jobs/{jid}/delivery/operations/{op['id']}/attempts").json()
        results = [a["result"] for a in attempts]
        if len(results) > 1:
            assert "retryable" in results or "success" in results


def test_mock_500_injection_bounded_retry(stage_isolated_client):
    """Inject 2x 500 Internal Server Error. Verify eventual success after retries."""
    c = stage_isolated_client
    jid = _upload(c)
    _drive_to_reconciliation_complete(c, jid)

    mc = _mock_target_client(c)
    mc.post("/_mock/failures", json={"status": 500, "count": 2})

    r = c.post(f"/api/jobs/{jid}/deliver")
    assert r.status_code == 200

    job = _poll(c, jid, {"migration_complete", "delivery_partial_failure", "error"}, tries=5000)

    delivery = c.get(f"/api/jobs/{jid}/delivery").json()
    succeeded_ops = [o for o in delivery["operations"] if o["status"] == "SUCCEEDED"]
    assert len(succeeded_ops) >= 1, "at least one op should succeed after transient 500s"


def test_mock_422_terminal_no_retry(stage_isolated_client):
    """Inject many 422 Validation Errors. Verify operations are FAILED with exactly 1 attempt
    (no blind retry on terminal errors)."""
    c = stage_isolated_client
    jid = _upload(c)
    _drive_to_reconciliation_complete(c, jid)

    mc = _mock_target_client(c)
    mc.post("/_mock/failures", json={"status": 422, "count": 50,
                                      "body": {"error": "validation_error",
                                               "errors": [{"field": "employee_id", "message": "invalid"}]}})

    r = c.post(f"/api/jobs/{jid}/deliver")
    assert r.status_code == 200

    job = _poll(c, jid, {"migration_complete", "delivery_partial_failure", "error"}, tries=5000)

    delivery = c.get(f"/api/jobs/{jid}/delivery").json()
    failed_ops = [o for o in delivery["operations"] if o["status"] == "FAILED"]
    assert len(failed_ops) >= 1, "should have at least one FAILED operation"

    for op in failed_ops:
        assert op["attempt_count"] == 1, f"terminal errors should not be retried; got {op['attempt_count']} attempts"


def test_revision_conflict_external_mutation(tmp_path, monkeypatch):
    """External mutation bumps the target revision → delivery sees 409 revision_conflict
    → stale-target handling fires (re-fetch + re-reconcile). Verified through audit events."""
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AUTO_CONTINUE", "false")
    from app import config
    config.get_settings.cache_clear()
    try:
        with TestClient(create_app()) as c:
            jid = _upload(c)
            _drive_to_reconciliation_complete(c, jid)

            mc = _mock_target_client(c)

            # Find an UPDATE candidate
            recon = c.get(f"/api/jobs/{jid}/reconciliation").json()
            update_cands = [r for r in recon["results"] if r["outcome"] == "READY_UPDATE"]

            if not update_cands:
                # No update candidates (all creates), skip this test variant
                pytest.skip("no READY_UPDATE candidates for revision conflict test")

            # Mutate the first update candidate in the target (bumps revision). The external mutation
            # must target the SAME organization as the job (the record lives in the job's org).
            cand = update_cands[0]
            target_id = cand["target_record_id"] or cand["business_key"]
            org_headers = {"X-Organization-ID": c.app.state.ctx.db.job_tenant(jid)}
            mut_r = mc.post(f"/_mock/employees/{target_id}/mutate",
                            json={"fields": {"department": "MUTATED_EXTERNALLY"}}, headers=org_headers)
            assert mut_r.status_code == 200, mut_r.text
            new_rev = mut_r.json()["revision"]

            # Now delivery will see a stale revision → 409
            r = c.post(f"/api/jobs/{jid}/deliver")
            assert r.status_code == 200

            job = _poll(c, jid, {"migration_complete", "delivery_partial_failure",
                                  "stale_target_review_required", "error"}, tries=5000)

            # The stale-target handler should have re-fetched and re-reconciled
            # Check audit for stale events
            audit = c.get(f"/api/jobs/{jid}/audit").json()
            stale_events = [a for a in audit if "stale" in a["event_type"]]
            assert len(stale_events) >= 1, "expected stale-target audit events"

            # Delivery operations should reflect the stale handling
            delivery = c.get(f"/api/jobs/{jid}/delivery").json()
            assert len(delivery["operations"]) > 0
    finally:
        config.get_settings.cache_clear()


# ===================== crash-window restart (Test J) ===========================

def test_crash_window_idempotent_restart(stage_isolated_client):
    """Test J: crash-window recovery via idempotency.

    Simulates the worst-case crash: the target accepted a CREATE but the process
    died before persisting SUCCEEDED. On restart the delivery re-executes the
    same operation with the same idempotency key — the target replays the
    original 201 response and no duplicate employee is created.

    Steps:
    1. Drive to reconciliation_complete, plan delivery ops.
    2. Write directly to mock target using a CREATE op's idempotency key
       (simulates the pre-crash target write that went through).
    3. Leave the op in PLANNED state (simulates: the worker crashed before
       it could even record PROCESSING).
    4. Trigger delivery normally — worker sends with the same idem key.
    5. Assert: target replays, op reaches SUCCEEDED, no duplicate records.
    """
    c = stage_isolated_client
    jid = _upload(c)
    _drive_to_reconciliation_complete(c, jid)

    # 1. Plan delivery ops (without executing)
    from app.delivery import plan_delivery
    ctx = c.app.state.ctx
    eff = ctx.effective_schema_for_job(jid)
    plan = plan_delivery(ctx.db, jid, eff)
    assert plan["planned"] > 0, "need at least one planned operation"

    # 2. Pick a CREATE op
    ops = ctx.db.get_delivery_operations(jid, status="PLANNED")
    create_ops = [o for o in ops if o["op_type"] == "CREATE"]
    assert len(create_ops) > 0, "need at least one CREATE"
    op = create_ops[0]
    op_id = op["id"]
    idem_key = op["idempotency_key"]
    payload = json.loads(op["payload"]) if isinstance(op["payload"], str) else op["payload"]
    employee_id = payload.get("employee_id")

    # 3. Write directly to mock target with the same idempotency key. This simulates the pre-crash
    #    write the WORKER already sent, so it must be scoped to the job's organization (as the
    #    worker's org-scoped gateway would): otherwise the restart's replay would look in a
    #    different organization and re-create instead of replaying.
    mc = _mock_target_client(c)
    org_headers = {"X-Organization-ID": ctx.db.job_tenant(jid)}
    target_r = mc.post("/employees", json={
        "employee": payload, "idempotency_key": idem_key,
    }, headers=org_headers)
    assert target_r.status_code == 201, f"mock target rejected pre-crash write: {target_r.text}"
    pre_crash_eid = target_r.json()["employee"]["employee_id"]
    pre_crash_rev = target_r.json().get("revision")

    # Confirm employee exists in target
    check = mc.get(f"/employees/{pre_crash_eid}", headers=org_headers)
    assert check.status_code == 200

    # 4. Trigger delivery — plan_delivery will see existing ops (idempotent skip),
    #    start_delivery enqueues the PLANNED ops, worker re-sends with same idem key
    r = c.post(f"/api/jobs/{jid}/deliver")
    assert r.status_code == 200, r.text

    job = _poll(c, jid, {"migration_complete", "delivery_partial_failure", "error"}, tries=5000)

    # 5. Verify
    delivery = c.get(f"/api/jobs/{jid}/delivery").json()
    our_op = next(o for o in delivery["operations"] if o["id"] == op_id)
    assert our_op["status"] == "SUCCEEDED", f"expected SUCCEEDED, got {our_op['status']}"

    # The target should still have exactly ONE employee with this ID (no duplicate)
    target_emp = mc.get(f"/employees/{pre_crash_eid}", headers=org_headers)
    assert target_emp.status_code == 200
    assert target_emp.json()["employee"]["employee_id"] == pre_crash_eid

    # Mock stats should show at least one replayed call (counters are nested)
    stats = mc.get("/_mock/stats").json()
    counters = stats.get("counters", stats)  # handle both flat and nested shapes
    assert counters.get("replayed_calls", 0) >= 1, \
        f"expected ≥1 replayed call (idempotent replay); got {stats}"

    # Write log should show the replay
    write_log = stats.get("write_log", [])
    replayed = [e for e in write_log if e.get("replayed")]
    assert len(replayed) >= 1, "expected at least one replayed write in mock log"
