"""Regression test: the live-smoke gate must recognize a key from backend/.env,
not only from the process environment (otherwise the live test silently skips)."""
from __future__ import annotations

from app import config
from tests.test_smoke_groq_live import live_enabled


def test_gate_off_without_flag(monkeypatch):
    monkeypatch.delenv("RUN_LIVE_GROQ", raising=False)
    assert live_enabled() is False


def test_gate_off_with_flag_but_no_key_anywhere(monkeypatch):
    monkeypatch.setenv("RUN_LIVE_GROQ", "1")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(config, "get_settings",
                        lambda: config.Settings(llm_provider="groq", groq_api_key=None))
    assert live_enabled() is False


def test_gate_on_when_key_only_in_env_file(monkeypatch):
    """RUN flag set + key present via settings/.env but NOT in os.environ -> enabled."""
    monkeypatch.setenv("RUN_LIVE_GROQ", "1")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)  # not in process env
    monkeypatch.setattr(config, "get_settings",
                        lambda: config.Settings(llm_provider="groq",
                                                groq_api_key="dummy-not-a-real-key"))
    assert live_enabled() is True
