"""M3E groundwork: product metrics endpoints + optional LangSmith tracing configuration.

Deterministic, offline. Proves the app exposes per-job and portfolio metrics from persisted state,
and that tracing configuration is a strict opt-in no-op when disabled (the app never depends on it).
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.observability import configure_tracing, tracing_status


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AUTO_CONTINUE", "false")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")     # tracing off for the offline suite
    monkeypatch.setenv("LANGSMITH_API_KEY", "")
    from app import config
    config.get_settings.cache_clear()
    config.get_settings().ensure_dirs()
    try:
        with TestClient(create_app()) as c:
            yield c
    finally:
        config.get_settings.cache_clear()


REDUNDANT_CSV = (
    b"Employee ID,Full Name,Work Email,Department,Hire Date,Sex,GenderID\n"
    b"E1,Alice A,alice@x.com,Engineering,2020-01-01,F,0\n"
    b"E2,Bob B,bob@x.com,Sales,2020-02-02,M,1\n"
    b"E3,Carol C,carol@x.com,Finance,2020-03-03,F,0\n"
    b"E4,Dan D,dan@x.com,Sales,2020-04-04,M,1\n")


def _poll(c, jid, want, tries=500, delay=0.02):
    for _ in range(tries):
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] in want:
            return j
        time.sleep(delay)
    return c.get(f"/api/jobs/{jid}").json()


def test_job_metrics_endpoint_reports_mapping_and_intelligence(client):
    jid = client.post("/api/jobs", files=[("files", ("g.csv", REDUNDANT_CSV, "text/csv"))]).json()["id"]
    _poll(client, jid, {"preparation_complete", "mapping_complete", "awaiting_record_review",
                        "blocked_provider", "error"})
    m = client.get(f"/api/jobs/{jid}/metrics").json()
    assert m["job_id"] == jid
    assert set(m) >= {"mapping", "intelligence", "preparation", "reconciliation", "delivery",
                      "human_decisions"}
    # Source intelligence recorded the redundant representation + the auto date/enum work.
    assert m["intelligence"]["redundant_representations"] >= 1
    assert m["intelligence"]["columns_profiled"] == 7
    assert m["mapping"]["rule"] >= 5


def test_portfolio_metrics_endpoint(client):
    settled = {"preparation_complete", "mapping_complete", "awaiting_record_review",
               "blocked_provider", "error"}
    for i in range(2):
        jid = client.post("/api/jobs", files=[("files", (f"g{i}.csv", REDUNDANT_CSV, "text/csv"))]).json()["id"]
        _poll(client, jid, settled)                  # let mapping finish before aggregating
    agg = client.get("/api/metrics").json()
    assert agg["jobs_total"] >= 2
    assert "mapping" in agg and "intelligence" in agg and "delivery" in agg
    assert agg["intelligence"]["redundant_representations"] >= 1


def test_observability_status_tracing_disabled(client):
    o = client.get("/api/observability").json()
    assert o["tracing"]["enabled"] is False          # no key / disabled in the offline suite
    assert o["tracing"]["key_present"] is False
    assert "metrics" in o


def test_configure_tracing_is_noop_without_key():
    s = Settings(langsmith_tracing=True, langsmith_api_key=None)
    st = configure_tracing(s)
    assert st["enabled"] is False
    assert tracing_status(s)["enabled"] is False


def test_configure_tracing_enables_env_when_configured(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    s = Settings(langsmith_tracing=True, langsmith_api_key="ls-fake-key-not-used-for-network",
                 langsmith_project="unit-test-proj")
    st = configure_tracing(s)
    assert st["enabled"] is True and st["project"] == "unit-test-proj"
    import os
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_PROJECT"] == "unit-test-proj"
    assert os.environ["LANGSMITH_API_KEY"] == "ls-fake-key-not-used-for-network"
