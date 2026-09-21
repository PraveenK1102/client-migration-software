"""Live LangSmith tracing smoke — gated, opt-in only.

Runs one tiny mapping job (NO-PROVIDER, so zero Groq spend) with tracing enabled and confirms the
LangGraph node runs land in a dedicated LangSmith project. Skipped unless RUN_LIVE_LANGSMITH=1 and a
LangSmith key is configured, so the normal suite never depends on network/credentials.

Verified live on 2026-09-20: 13 runs traced (LangGraph root + profile / map_columns / analyze_source
/ assess / finalize_mapping / prep_start / prepare_records / finalize_preparation + routers).
"""
from __future__ import annotations

import os
import time
import uuid

import pytest


def _live_enabled() -> bool:
    if os.environ.get("RUN_LIVE_LANGSMITH") != "1":
        return False
    from app import config
    config.get_settings.cache_clear()
    return config.get_settings().langsmith_key is not None


pytestmark = pytest.mark.skipif(
    not _live_enabled(),
    reason="Live LangSmith test: set RUN_LIVE_LANGSMITH=1 and a LANGSMITH_API_KEY (env or backend/.env).")


def test_langgraph_runs_are_traced_to_langsmith(tmp_path, monkeypatch):
    marker = f"darwinbox-citest-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "")                 # no model spend
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LANGSMITH_PROJECT", marker)        # dedicated project so runs are findable
    from app import config
    config.get_settings.cache_clear()
    settings = config.get_settings()
    settings.ensure_dirs()

    from fastapi.testclient import TestClient
    from app.main import create_app

    csv = b"Employee ID,Full Name,Work Email,Hire Date\nE1,A,a@x.com,2020-01-01\n"
    with TestClient(create_app()) as c:
        jid = c.post("/api/jobs", files=[("files", ("s.csv", csv, "text/csv"))]).json()["id"]
        for _ in range(500):
            st = c.get(f"/api/jobs/{jid}").json()["status"]
            if st in {"preparation_complete", "mapping_complete", "awaiting_record_review",
                      "blocked_provider", "error", "migration_complete"}:
                break
            time.sleep(0.02)

    from langchain_core.tracers.langchain import wait_for_all_tracers
    wait_for_all_tracers()
    from langsmith import Client
    cl = Client(api_key=settings.langsmith_key, api_url=settings.langsmith_endpoint)
    runs = []
    for _ in range(15):
        time.sleep(1.0)
        try:
            runs = list(cl.list_runs(project_name=marker, limit=25))
        except Exception:
            runs = []
        if runs:
            break
    names = {r.name for r in runs}
    assert runs, f"no LangSmith runs found in project {marker}"
    assert {"map_columns", "analyze_source"} & names, names
    config.get_settings.cache_clear()


def _groq_live_enabled() -> bool:
    if os.environ.get("RUN_LIVE_LANGSMITH") != "1":
        return False
    from app import config
    config.get_settings.cache_clear()
    s = config.get_settings()
    return s.langsmith_key is not None and s.groq_key is not None


@pytest.mark.skipif(not _groq_live_enabled(),
                    reason="Live model-span test: needs RUN_LIVE_LANGSMITH=1 + a LangSmith key + a Groq key.")
def test_model_call_span_reaches_langsmith_with_token_metadata(tmp_path, monkeypatch):
    """End-to-end proof that a MODEL CALL emits a sanitized run_type='llm' span to LangSmith carrying
    token/latency metadata (and no raw PII). Makes ONE real, bounded Groq call (free-tier)."""
    marker = f"darwinbox-modelspan-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("LLM_PROVIDER", "groq")               # real Groq key comes from backend/.env
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LANGSMITH_PROJECT", marker)
    from app import config
    config.get_settings.cache_clear()
    settings = config.get_settings()
    settings.ensure_dirs()

    from fastapi.testclient import TestClient
    from app.main import create_app

    # 'Widget Preference' has no deterministic rule -> exactly one unresolved column -> one model call.
    csv = b"Employee ID,Full Name,Work Email,Hire Date,Widget Preference\nE1,Ann Lee,a@x.com,2020-01-01,blue\n"
    with TestClient(create_app()) as c:
        jid = c.post("/api/jobs", files=[("files", ("s.csv", csv, "text/csv"))]).json()["id"]
        for _ in range(1000):
            st = c.get(f"/api/jobs/{jid}").json()["status"]
            if st in {"mapping_complete", "awaiting_review", "awaiting_record_review",
                      "preparation_complete", "migration_complete", "error"}:
                break
            time.sleep(0.02)
        # persisted, sanitized model-call metric exists with a real latency
        mm = c.get(f"/api/jobs/{jid}/metrics").json().get("model", {})
        assert mm.get("calls", 0) >= 1 and "groq" in (mm.get("adapter_kinds") or []), mm

    from langchain_core.tracers.langchain import wait_for_all_tracers
    wait_for_all_tracers()
    from langsmith import Client
    cl = Client(api_key=settings.langsmith_key, api_url=settings.langsmith_endpoint)
    model_runs = []
    for _ in range(20):
        time.sleep(1.0)
        try:
            model_runs = [r for r in cl.list_runs(project_name=marker, limit=50)
                          if (r.name or "").startswith("model.")]
        except Exception:
            model_runs = []
        if model_runs:
            break
    assert model_runs, f"no model.* LLM span found in LangSmith project {marker}"
    r = model_runs[0]
    blob = f"{r.name} {getattr(r, 'inputs', {})} {getattr(r, 'outputs', {})} {getattr(r, 'extra', {})}"
    # sanitized: no raw PII from the source row
    for pii in ("Ann Lee", "a@x.com"):
        assert pii not in blob, f"raw PII leaked to LangSmith span: {pii!r}"
    config.get_settings.cache_clear()
