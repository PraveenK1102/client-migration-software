"""Settings loader: one source, cwd-independent dotenv, blank/whitespace/precedence,
empty inherited override, and no secret leakage. Offline; reads no real credentials."""
from __future__ import annotations

import json

from app.config import Settings


def _env(tmp_path, content: str) -> str:
    p = tmp_path / ".env"
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_loads_from_temp_env_independent_of_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    s = Settings(_env_file=_env(tmp_path, "LLM_PROVIDER=groq\nGROQ_API_KEY=dummy-not-real\n"))
    assert s.groq_key == "dummy-not-real"
    st = s.provider_status()
    assert st["configured"] is True and st["category"] == "configured"


def test_absent_env_file_is_configuration_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    s = Settings(_env_file=str(tmp_path / "nope.env"), llm_provider="groq")
    assert s.groq_key is None
    assert s.provider_status()["category"] == "configuration_missing"


def test_blank_key_is_unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    s = Settings(_env_file=_env(tmp_path, "LLM_PROVIDER=groq\nGROQ_API_KEY=\n"))
    assert s.groq_key is None
    assert s.provider_status()["category"] == "configuration_missing"


def test_whitespace_key_is_unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    s = Settings(_env_file=_env(tmp_path, "LLM_PROVIDER=groq\nGROQ_API_KEY=   \n"))
    assert s.groq_key is None


def test_process_env_takes_precedence_over_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "from-process-env")
    s = Settings(_env_file=_env(tmp_path, "LLM_PROVIDER=groq\nGROQ_API_KEY=from-file\n"))
    assert s.groq_key == "from-process-env"     # documented precedence preserved


def test_empty_inherited_override_is_diagnosed(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "")      # empty env shadows a nonempty dotenv value
    s = Settings(_env_file=_env(tmp_path, "LLM_PROVIDER=groq\nGROQ_API_KEY=from-file\n"))
    assert s.groq_key is None
    st = s.provider_status()
    assert st["inherited_empty_override"] is True
    assert st["category"] == "configuration_missing_inherited_empty_override"


def test_provider_status_never_contains_the_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    s = Settings(_env_file=_env(tmp_path, "LLM_PROVIDER=groq\nGROQ_API_KEY=super-secret-abc123\n"))
    blob = json.dumps(s.provider_status())
    assert "super-secret-abc123" not in blob
    assert "key_present" in blob and "key_nonempty" in blob


def test_fake_provider_is_configured_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    s = Settings(_env_file=str(tmp_path / "none.env"), llm_provider="fake")
    st = s.provider_status()
    assert st["configured"] is True and st["requires_key"] is False
