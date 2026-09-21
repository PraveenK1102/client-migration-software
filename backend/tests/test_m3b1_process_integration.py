"""M3B.1 process-level integration tests (real ports, real process restart).

These tests DO NOT use httpx ASGITransport. They run:
  * the mock target as a genuinely separate uvicorn process on its own port + own SQLite, and
  * the migration backend as a separate uvicorn process with TARGET_INPROCESS=false, so every
    target call crosses a real TCP HTTP boundary through TargetEmployeeGateway.

Covered:
  A. test_separate_process_delivery_scenarios — CREATE / UPDATE / NO_CHANGE / transient-retry /
     terminal-failure / stale-target(409)->refetch / rollback CREATE + rollback UPDATE, all over
     a real port.
  B. test_durable_retry_across_restart — a retryable failure schedules a retry work item with a
     FUTURE available_at; the backend is KILLED (SIGKILL) before it is due; the item is shown to be
     still persisted in SQLite; the backend is restarted; the retry then executes EXACTLY ONCE and
     the target side effect is not duplicated (idempotency key).

Offline: GROQ_API_KEY is empty (no model calls); the data is core-field-only so mapping resolves
deterministically. These are skipped only if uvicorn/httpx are unavailable.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("uvicorn")

BACKEND_DIR = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# subprocess + HTTP helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_health(url: str, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code == 200:
                return
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.3)
    raise TimeoutError(f"{url} not healthy within {timeout}s (last error: {last})")


def _start_target(port: int, db_path: Path, log_path: Path):
    env = dict(os.environ)
    env["TARGET_DB_PATH"] = str(db_path)
    env["MOCK_TARGET_ADMIN"] = "1"
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "mock_target.service:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND_DIR), env=env, stdout=log, stderr=subprocess.STDOUT)
    return proc, log


def _start_backend(port: int, target_port: int, data_dir: Path, log_path: Path, **extra_env):
    env = dict(os.environ)
    env.update({
        "DATA_DIR": str(data_dir),
        "TARGET_INPROCESS": "false",
        "TARGET_BASE_URL": f"http://127.0.0.1:{target_port}",
        "LLM_PROVIDER": "groq",
        "GROQ_API_KEY": "",
        "AUTO_CONTINUE": "false",
        "START_WORKERS": "true",
    })
    env.update({k: str(v) for k, v in extra_env.items()})
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND_DIR), env=env, stdout=log, stderr=subprocess.STDOUT)
    return proc, log


def _kill(proc) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGKILL)
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _term(proc) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        _kill(proc)


def _poll_job(c: "httpx.Client", jid: str, want: set[str], tries: int = 1200, delay: float = 0.1) -> dict:
    last = None
    for _ in range(tries):
        last = c.get(f"/api/jobs/{jid}").json()
        if last["status"] in want:
            return last
        time.sleep(delay)
    raise TimeoutError(f"job {jid} stuck at {last and last['status']}; wanted {want}")


def _upload(c: "httpx.Client", csv_text: str, tenant: str = "default") -> str:
    files = [("files", ("employees.csv", csv_text.encode(), "text/csv"))]
    r = c.post("/api/jobs", files=files, data={"tenant_id": tenant})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _drive_to_recon_complete(c: "httpx.Client", jid: str) -> dict:
    """Robust state-machine driver: keep resolving whatever review state appears until the job
    reaches preparation_complete, then reconcile through any target review to reconciliation_complete."""
    wait = {"blocked_provider", "awaiting_review", "awaiting_record_review",
            "preparation_complete", "error"}
    for _ in range(400):
        job = _poll_job(c, jid, wait | {"mapping_complete"})
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
                       json={"action": "approve", "version": i["version"], "reason": "test"})
        elif st == "awaiting_record_review":
            for i in c.get(f"/api/jobs/{jid}/record-reviews?status=open").json():
                opts = json.loads(i["options"]) if isinstance(i.get("options"), str) else (i.get("options") or [])
                action = opts[0]["value"] if opts and isinstance(opts[0], dict) and "value" in opts[0] else \
                    (opts[0] if opts else "accept")
                c.post(f"/api/jobs/{jid}/record-reviews/{i['id']}/decision",
                       json={"action": str(action), "version": i["version"], "reason": "test"})
        elif st == "mapping_complete":
            time.sleep(0.2)
        time.sleep(0.2)
    assert job["status"] == "preparation_complete", f"got {job['status']}"
    r = c.post(f"/api/jobs/{jid}/reconcile")
    assert r.status_code == 200, r.text
    for _ in range(400):
        job = _poll_job(c, jid, {"reconciliation_complete", "awaiting_target_review", "error"})
        if job["status"] in ("reconciliation_complete", "error"):
            break
        for i in c.get(f"/api/jobs/{jid}/target-reviews?status=open").json():
            c.post(f"/api/jobs/{jid}/target-reviews/{i['id']}/decision",
                   json={"action": "use_incoming", "version": i["version"], "reason": "test"})
        time.sleep(0.2)
    assert job["status"] == "reconciliation_complete", f"got {job['status']}"
    return job


# ===========================================================================
# A. Separate-process delivery scenarios over a REAL HTTP port
# ===========================================================================

def test_separate_process_delivery_scenarios(tmp_path, capsys):
    target_port = _free_port()
    backend_port = _free_port()
    target_db = tmp_path / "target_ext.db"
    data_dir = tmp_path / "appdata"
    tproc = bproc = None
    report: list[str] = []
    try:
        tproc, _ = _start_target(target_port, target_db, tmp_path / "target.log")
        _wait_health(f"http://127.0.0.1:{target_port}/health")
        report.append(f"mock target: separate process pid={tproc.pid} port={target_port} db={target_db.name}")

        bproc, _ = _start_backend(backend_port, target_port, data_dir, tmp_path / "backend.log")
        _wait_health(f"http://127.0.0.1:{backend_port}/api/health")
        report.append(f"backend: separate process pid={bproc.pid} port={backend_port} TARGET_INPROCESS=false "
                      f"TARGET_BASE_URL=http://127.0.0.1:{target_port}")

        tgt = httpx.Client(base_url=f"http://127.0.0.1:{target_port}", timeout=10.0)
        api = httpx.Client(base_url=f"http://127.0.0.1:{backend_port}", timeout=30.0)

        # Confirm the app really reaches the target over the port (gateway health via reconcile later).
        h = tgt.get("/health").json()
        report.append(f"target /health over port: employees={h['employees']} admin={h['admin']}")

        # --- Pre-seed target rows for UPDATE, NO_CHANGE, STALE via the real HTTP API ---
        tgt.post("/employees", json={"idempotency_key": "seed-upd", "employee": {
            "employee_id": "P-UPD", "full_name": "Uma Update", "work_email": "uma.upd@corp.example",
            "hire_date": "2020-01-01", "department": None}})
        tgt.post("/employees", json={"idempotency_key": "seed-nochg", "employee": {
            "employee_id": "P-NOCHG", "full_name": "Nora NoChange", "work_email": "nora.nc@corp.example",
            "hire_date": "2020-02-02", "department": "Finance", "designation": "Analyst"}})
        tgt.post("/employees", json={"idempotency_key": "seed-stale", "employee": {
            "employee_id": "P-STALE", "full_name": "Sam Stale", "work_email": "sam.stale@corp.example",
            "hire_date": "2020-03-03", "department": None}})

        # ---- Scenario set 1: CREATE + UPDATE + NO_CHANGE in one job ----
        csv1 = (
            "Employee ID,Full Name,Work Email,Department,Hire Date,Designation\n"
            "P-NEW1,Nyla New,nyla.new@corp.example,Engineering,2021-05-05,Engineer\n"
            "P-NEW2,Ravi New,ravi.new@corp.example,Sales,2021-06-06,Rep\n"
            "P-UPD,Uma Update,uma.upd@corp.example,Finance,2020-01-01,Analyst\n"
            "P-NOCHG,Nora NoChange,nora.nc@corp.example,Finance,2020-02-02,Analyst\n"
        )
        jid1 = _upload(api, csv1)
        _drive_to_recon_complete(api, jid1)
        assert api.post(f"/api/jobs/{jid1}/deliver").status_code == 200
        _poll_job(api, jid1, {"migration_complete", "delivery_partial_failure", "error"})
        d1 = api.get(f"/api/jobs/{jid1}/delivery").json()
        by_type = {}
        for o in d1["operations"]:
            by_type.setdefault((o["op_type"], o["status"]), 0)
            by_type[(o["op_type"], o["status"])] += 1
        created = sum(1 for o in d1["operations"] if o["op_type"] == "CREATE" and o["status"] == "SUCCEEDED")
        updated = sum(1 for o in d1["operations"] if o["op_type"] == "UPDATE" and o["status"] == "SUCCEEDED")
        assert created == 2, f"expected 2 CREATE SUCCEEDED, got {by_type}"
        assert updated == 1, f"expected 1 UPDATE SUCCEEDED, got {by_type}"
        # NO_CHANGE produced no operation
        assert not any(o["employee_id"] == "P-NOCHG" for o in d1["operations"]), "NO_CHANGE should not deliver"
        # Verify writes actually landed in the separate target
        assert tgt.get("/employees/P-NEW1").status_code == 200
        assert tgt.get("/employees/P-UPD").json()["employee"]["department"] == "Finance"
        report.append(f"scenario CREATE/UPDATE/NO_CHANGE: ops={dict((f'{k[0]}:{k[1]}', v) for k, v in by_type.items())}, "
                      f"NO_CHANGE emitted 0 ops (P-NOCHG untouched)")

        # ---- Scenario 2: transient 500 x2 -> retry -> success ----
        tgt.post("/_mock/failures", json={"mode": "status", "status": 500, "count": 2, "scope": "create"})
        csv2 = ("Employee ID,Full Name,Work Email,Department,Hire Date\n"
                "P-RETRY,Rhea Retry,rhea.retry@corp.example,Engineering,2022-01-01\n")
        jid2 = _upload(api, csv2)
        _drive_to_recon_complete(api, jid2)
        assert api.post(f"/api/jobs/{jid2}/deliver").status_code == 200
        _poll_job(api, jid2, {"migration_complete", "delivery_partial_failure", "error"})
        d2 = api.get(f"/api/jobs/{jid2}/delivery").json()
        op2 = next(o for o in d2["operations"] if o["employee_id"] == "P-RETRY")
        att2 = api.get(f"/api/jobs/{jid2}/delivery/operations/{op2['id']}/attempts").json()
        assert op2["status"] == "SUCCEEDED", f"retry op did not succeed: {op2['status']}"
        assert len(att2) >= 2, f"expected >=2 attempts (retries), got {len(att2)}"
        report.append(f"scenario transient 500x2 -> retry: attempts={len(att2)} final={op2['status']}")

        # ---- Scenario 3: transient 429 with Retry-After -> retry -> success ----
        tgt.post("/_mock/failures", json={"mode": "status", "status": 429, "count": 1,
                                          "scope": "create", "retry_after": 1})
        csv3 = ("Employee ID,Full Name,Work Email,Department,Hire Date\n"
                "P-429,Quin Rate,quin.rate@corp.example,Sales,2022-02-02\n")
        jid3 = _upload(api, csv3)
        _drive_to_recon_complete(api, jid3)
        assert api.post(f"/api/jobs/{jid3}/deliver").status_code == 200
        _poll_job(api, jid3, {"migration_complete", "delivery_partial_failure", "error"})
        d3 = api.get(f"/api/jobs/{jid3}/delivery").json()
        op3 = next(o for o in d3["operations"] if o["employee_id"] == "P-429")
        att3 = api.get(f"/api/jobs/{jid3}/delivery/operations/{op3['id']}/attempts").json()
        cats3 = [a.get("error_category") for a in att3]
        assert op3["status"] == "SUCCEEDED"
        assert any(cat == "rate_limited" for cat in cats3), f"expected a rate_limited attempt, got {cats3}"
        report.append(f"scenario transient 429+Retry-After -> retry: attempts={len(att3)} cats={cats3} final={op3['status']}")

        # ---- Scenario 4: terminal 422 -> FAILED, no blind retry ----
        tgt.post("/_mock/failures", json={"mode": "status", "status": 422, "count": 50, "scope": "create"})
        csv4 = ("Employee ID,Full Name,Work Email,Department,Hire Date\n"
                "P-TERM,Tara Terminal,tara.term@corp.example,Finance,2022-03-03\n")
        jid4 = _upload(api, csv4)
        _drive_to_recon_complete(api, jid4)
        assert api.post(f"/api/jobs/{jid4}/deliver").status_code == 200
        _poll_job(api, jid4, {"migration_complete", "delivery_partial_failure", "error"})
        d4 = api.get(f"/api/jobs/{jid4}/delivery").json()
        op4 = next(o for o in d4["operations"] if o["employee_id"] == "P-TERM")
        att4 = api.get(f"/api/jobs/{jid4}/delivery/operations/{op4['id']}/attempts").json()
        assert op4["status"] == "FAILED", f"terminal op should be FAILED, got {op4['status']}"
        assert len(att4) == 1, f"terminal failure must not blind-retry; attempts={len(att4)}"
        tgt.delete("/_mock/failures")
        report.append(f"scenario terminal 422: status={op4['status']} attempts={len(att4)} (no blind retry)")

        # ---- Scenario 5: stale target 409 -> refetch/reconcile ----
        csv5 = ("Employee ID,Full Name,Work Email,Department,Hire Date\n"
                "P-STALE,Sam Stale,sam.stale@corp.example,People Operations,2020-03-03\n")
        jid5 = _upload(api, csv5)
        _drive_to_recon_complete(api, jid5)
        # Mutate the target AFTER reconciliation so the stored expected_revision is now stale.
        mut = tgt.post("/_mock/employees/P-STALE/mutate", json={"fields": {"designation": "Lead"}})
        assert mut.status_code == 200
        assert api.post(f"/api/jobs/{jid5}/deliver").status_code == 200
        _poll_job(api, jid5, {"migration_complete", "delivery_partial_failure",
                              "stale_target_review_required", "error"})
        audits5 = api.get(f"/api/jobs/{jid5}/audit").json()
        events5 = {a["event_type"] for a in (audits5 if isinstance(audits5, list) else audits5.get("events", []))}
        assert "stale_target_detected" in events5, f"expected stale detection; events={sorted(events5)}"
        assert "stale_target_refetched" in events5, f"expected refetch; events={sorted(events5)}"
        report.append(f"scenario stale 409 -> refetch/reconcile: audit has "
                      f"{sorted(e for e in events5 if e.startswith('stale_'))}")

        # ---- Scenario 6: rollback CREATE + rollback UPDATE (job 1) ----
        assert api.post(f"/api/jobs/{jid1}/rollback").status_code == 200
        _poll_job(api, jid1, {"rollback_complete", "rollback_partial_failure", "error"})
        d1b = api.get(f"/api/jobs/{jid1}/delivery").json()
        rb_create = [o for o in d1b["operations"] if o["op_type"] == "CREATE" and o["status"] == "ROLLED_BACK"]
        rb_update = [o for o in d1b["operations"] if o["op_type"] == "UPDATE" and o["status"] == "ROLLED_BACK"]
        assert len(rb_create) == 2, f"expected 2 rolled-back CREATEs, got {len(rb_create)}"
        assert len(rb_update) == 1, f"expected 1 rolled-back UPDATE, got {len(rb_update)}"
        # CREATE rollback => target row deleted; UPDATE rollback => department back to None (before_snapshot)
        assert tgt.get("/employees/P-NEW1").status_code == 404, "rollback CREATE should delete the target row"
        assert tgt.get("/employees/P-UPD").json()["employee"]["department"] is None, \
            "rollback UPDATE should restore the empty department"
        report.append(f"scenario rollback: CREATE->DELETE x{len(rb_create)} (P-NEW1 now 404), "
                      f"UPDATE->PUT x{len(rb_update)} (P-UPD department restored to null)")

        tgt.close(); api.close()

        print("\n=== A. SEPARATE-PROCESS DELIVERY EVIDENCE ===")
        for line in report:
            print("  - " + line)
    finally:
        _term(bproc)
        _term(tproc)


# ===========================================================================
# B. Durable delayed retry across a REAL process restart
# ===========================================================================

def test_durable_retry_across_restart(tmp_path, capsys):
    target_port = _free_port()
    backend_port = _free_port()
    target_db = tmp_path / "target_ext.db"
    data_dir = tmp_path / "appdata"
    app_db = data_dir / "app.db"
    tproc = bproc = bproc2 = None
    report: list[str] = []
    # Large backoff base so the scheduled retry is clearly in the future when we KILL the backend.
    backoff_env = {"TARGET_RETRY_BASE_SECONDS": "8", "TARGET_RETRY_MAX_SECONDS": "60",
                   "TARGET_MAX_ATTEMPTS": "25", "WORK_MAX_ATTEMPTS": "25"}
    try:
        tproc, _ = _start_target(target_port, target_db, tmp_path / "target.log")
        _wait_health(f"http://127.0.0.1:{target_port}/health")
        bproc, _ = _start_backend(backend_port, target_port, data_dir, tmp_path / "backend1.log",
                                  **backoff_env)
        _wait_health(f"http://127.0.0.1:{backend_port}/api/health")

        tgt = httpx.Client(base_url=f"http://127.0.0.1:{target_port}", timeout=10.0)
        api = httpx.Client(base_url=f"http://127.0.0.1:{backend_port}", timeout=30.0)

        # 1) Make target CREATE fail retryably (500) for many calls.
        tgt.post("/_mock/failures", json={"mode": "status", "status": 500, "count": 100, "scope": "create"})

        csv = ("Employee ID,Full Name,Work Email,Department,Hire Date\n"
               "D-1,Dana Durable,dana.durable@corp.example,Engineering,2021-01-01\n")
        jid = _upload(api, csv)
        _drive_to_recon_complete(api, jid)
        assert api.post(f"/api/jobs/{jid}/deliver").status_code == 200

        # 2) Wait until a retry is scheduled with a FUTURE available_at (first attempt failed).
        op_id = None
        deadline = time.time() + 30
        while time.time() < deadline:
            d = api.get(f"/api/jobs/{jid}/delivery").json()
            if d["operations"]:
                op = d["operations"][0]
                op_id = op["id"]
                atts = api.get(f"/api/jobs/{jid}/delivery/operations/{op_id}/attempts").json()
                if atts and op["status"] in ("RETRYABLE", "PROCESSING"):
                    break
            time.sleep(0.2)
        assert op_id, "no delivery operation appeared"

        # Inspect the DURABLE queue directly: a retryable DELIVER_OP with available_at in the future.
        def _future_retry_rows():
            conn = sqlite3.connect(str(app_db)); conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT id, status, attempt, available_at FROM work_items "
                    "WHERE kind='DELIVER_OP'").fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        # Give the first failure a moment to be recorded and re-enqueued with backoff.
        future_rows = []
        deadline = time.time() + 30
        while time.time() < deadline:
            rows = _future_retry_rows()
            now_iso = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
            future_rows = [r for r in rows if r["status"] in ("retryable", "pending")
                           and r["available_at"] > now_iso]
            if future_rows:
                break
            time.sleep(0.3)
        assert future_rows, f"expected a retryable work item with future available_at; rows={_future_retry_rows()}"
        pre_attempts = api.get(f"/api/jobs/{jid}/delivery/operations/{op_id}/attempts").json()
        report.append(f"before kill: work_items(DELIVER_OP)={_future_retry_rows()}")
        report.append(f"before kill: op status=RETRYABLE, attempts_so_far={len(pre_attempts)}, "
                      f"retry scheduled in the future (available_at > now)")

        # 3) KILL the backend (SIGKILL) before the retry is due. Target process stays alive.
        _kill(bproc); bproc = None

        # 4) Confirm the retry work item is STILL persisted after the crash.
        persisted = _future_retry_rows()
        assert any(r["status"] in ("retryable", "pending") for r in persisted), \
            f"retry work item not persisted after kill: {persisted}"
        report.append(f"after kill: work item still persisted in SQLite: {persisted}")

        # 5) Clear the injected failures so the retry can succeed once the backend is back.
        tgt.delete("/_mock/failures")

        # 6) Restart the backend against the SAME data dir + SAME target.
        bproc2, _ = _start_backend(backend_port, target_port, data_dir, tmp_path / "backend2.log",
                                   **backoff_env)
        _wait_health(f"http://127.0.0.1:{backend_port}/api/health")
        api2 = httpx.Client(base_url=f"http://127.0.0.1:{backend_port}", timeout=30.0)

        # 7) The durable retry becomes due and executes; job reaches migration_complete.
        job = _poll_job(api2, jid, {"migration_complete", "delivery_partial_failure", "error"},
                        tries=2000, delay=0.2)
        assert job["status"] == "migration_complete", f"job did not complete after restart: {job['status']}"

        # 8) Exactly one employee created; no duplicate side effect (idempotency).
        assert tgt.get("/employees/D-1").status_code == 200
        stats = tgt.get("/_mock/stats").json()
        create_calls = stats["counters"].get("create_calls", 0)
        emp_count = stats["employees"]
        d_final = api2.get(f"/api/jobs/{jid}/delivery").json()
        op_final = next(o for o in d_final["operations"] if o["id"] == op_id)
        assert op_final["status"] == "SUCCEEDED"
        assert create_calls == 1, f"target performed the CREATE side effect {create_calls} times (expected exactly 1)"
        # The write log shows a single successful create for D-1 (idempotency prevented duplicates).
        creates_for_d1 = [w for w in stats["write_log"]
                          if w["employee_id"] == "D-1" and w["operation"] == "create" and not w["replayed"]]
        assert len(creates_for_d1) == 1, f"expected exactly 1 real create for D-1; got {creates_for_d1}"
        report.append(f"after restart: job={job['status']}, op={op_final['status']}, "
                      f"target create_calls={create_calls}, employees_with_D-1=1, "
                      f"real creates for D-1={len(creates_for_d1)} (no duplicate side effect)")

        tgt.close(); api2.close()
        print("\n=== B. DURABLE RETRY ACROSS REAL RESTART EVIDENCE ===")
        for line in report:
            print("  - " + line)
    finally:
        _term(bproc)
        _term(bproc2)
        _term(tproc)
