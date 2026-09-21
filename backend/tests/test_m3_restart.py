"""M3A: durability across a REAL backend OS-process restart.

This is deliberately NOT an in-process AppContext re-instantiation. It boots the FastAPI app
in a separate uvicorn process, drives a job to a paused human-review state (persisted in SQLite
+ the LangGraph checkpoint), HARD-KILLS the process (SIGKILL), boots a fresh process on the same
DATA_DIR, and proves: the paused review survives, work is not lost or duplicated, and resuming
finishes the job. WAL + durable work/issue state + the file-backed checkpointer make this hold.
"""
from __future__ import annotations

import importlib.util
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

BACKEND = Path(__file__).resolve().parent.parent
SAMPLE = BACKEND.parent / "sample-data"

pytestmark = pytest.mark.skipif(importlib.util.find_spec("uvicorn") is None,
                                reason="uvicorn not installed")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_server(data_dir: Path, port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["DATA_DIR"] = str(data_dir)
    env["LLM_PROVIDER"] = "fake"
    env["AUTO_CONTINUE"] = "false"   # stage isolation: test polls for preparation_complete, not migration_complete
    env.pop("GROQ_API_KEY", None)
    log = open(data_dir / f"uvicorn_{port}_{int(time.time()*1000)}.log", "w")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--no-access-log"],
        cwd=str(BACKEND), env=env, stdout=log, stderr=subprocess.STDOUT)


def _wait_health(port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as c:
        while time.time() < deadline:
            try:
                if c.get("/api/health").status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(0.25)
    raise TimeoutError(f"server on {port} did not become healthy")


def _poll(client: httpx.Client, job_id: str, want: set[str], tries=400, delay=0.1) -> dict:
    for _ in range(tries):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in want:
            return j
        time.sleep(delay)
    return client.get(f"/api/jobs/{job_id}").json()


def _hard_kill(proc: subprocess.Popen) -> None:
    try:
        os.kill(proc.pid, signal.SIGKILL)   # true crash, not a graceful shutdown
    except ProcessLookupError:
        pass
    proc.wait(timeout=10)


def _resolve_record_issue_body(iss: dict) -> dict:
    t, opts = iss["issue_type"], iss["options"]
    body = {"version": iss["version"], "reason": "restart-test"}
    if t == "ambiguous_date":                               # M3C: one column-scoped convention decision
        body.update(action="confirm_convention", convention=opts[0]["meaning"])
    elif t == "invalid_value":
        body.update(action="correct", value="2020-01-01")
    elif t == "missing_required":
        body.update(action="correct", value="fixed@acme.com")
    elif t == "unknown_enum":                               # M3C: one column-scoped value-map decision
        _um = [d["value"] for d in iss["affected"].get("distinct_values", [])]
        body.update(action="map_values", value_map={v: opts[0] for v in _um})
    elif t == "value_conflict":
        body.update(action="select", value=opts[0])
    elif t == "shared_email":
        body.update(action="exclude", candidate_id=iss["affected"]["candidates"][1]["candidate_id"])
    else:
        body.update(action="exclude")
    return body


def test_paused_review_survives_real_process_restart(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    proc = _start_server(data_dir, port)
    try:
        _wait_health(port)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as c:
            data = (SAMPLE / "canonical_messy.csv").read_bytes()
            job_id = c.post("/api/jobs",
                            files=[("files", ("canonical_messy.csv", data, "text/csv"))]).json()["id"]
            job = _poll(c, job_id, {"awaiting_record_review", "error"})
            assert job["status"] == "awaiting_record_review", job
            open_before = {i["id"] for i in c.get(f"/api/jobs/{job_id}/record-reviews").json()}
            assert open_before
    finally:
        _hard_kill(proc)   # SIGKILL: no graceful shutdown hooks run

    # --- Fresh OS process, same DATA_DIR ---
    port2 = _free_port()
    proc2 = _start_server(data_dir, port2)
    try:
        _wait_health(port2)
        with httpx.Client(base_url=f"http://127.0.0.1:{port2}", timeout=10) as c:
            job = c.get(f"/api/jobs/{job_id}").json()
            assert job["status"] == "awaiting_record_review", job   # paused state survived the crash
            issues = c.get(f"/api/jobs/{job_id}/record-reviews").json()
            assert {i["id"] for i in issues} == open_before          # exact same open issues

            for iss in issues:
                r = c.post(f"/api/jobs/{job_id}/record-reviews/{iss['id']}/decision",
                           json=_resolve_record_issue_body(iss))
                assert r.status_code == 200, r.text

            job = _poll(c, job_id, {"preparation_complete", "error"})
            assert job["status"] == "preparation_complete", job      # resumed to completion
            n1 = len(c.get(f"/api/jobs/{job_id}/candidates").json())
            assert n1 >= 1
            # Completed ingest/map work is not repeated: exactly one succeeded INGEST + one MAP.
            work = c.get(f"/api/jobs/{job_id}/work-items").json()
            assert len([w for w in work if w["kind"] == "INGEST_FILE" and w["status"] == "succeeded"]) == 1
            assert len([w for w in work if w["kind"] == "MAP"]) == 1
    finally:
        _hard_kill(proc2)
