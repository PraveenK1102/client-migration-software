"""M3E Groq free-tier rate-limit safety (order §11).

Proves, with a MOCKED clock/sleep and a fake Groq client (no network, no key), that a 429 carrying a
Retry-After header makes the adapter WAIT at least the required interval BEFORE retrying, then
succeed — and that a persistent 429 exhausts a BOUNDED attempt budget (no busy-loop) as a recoverable
error. Covers both the mapping and the value-map transform paths.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from groq import RateLimitError

from app.llm.base import ProposalColumn, ProposalRequest, RecoverableModelError, TransformProposalRequest
from app.llm.groq_adapter import GroqAdapter


def _rate_limit_error(retry_after: str = "5") -> RateLimitError:
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    resp = httpx.Response(429, headers={"retry-after": retry_after}, request=req)
    return RateLimitError("rate limited", response=resp, body=None)


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.finish_reason = "stop"
        self.message = _Msg(content)


class _Completion:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = type("U", (), {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})()


class _FakeCompletions:
    def __init__(self, script):
        self._script = list(script)   # each item: Exception to raise, or str content to return
        self.i = 0
        self.events: list[str] = []

    async def create(self, **kwargs):
        self.events.append("create")
        item = self._script[min(self.i, len(self._script) - 1)]
        self.i += 1
        if isinstance(item, Exception):
            raise item
        return _Completion(item)


class _FakeGroqClient:
    def __init__(self, script):
        self.completions = _FakeCompletions(script)
        self.chat = type("Chat", (), {"completions": self.completions})()


def _adapter(client, *, max_attempts=3, deadline=210.0):
    return GroqAdapter(client=client, model_id="openai/gpt-oss-20b", max_attempts=max_attempts,
                       operation_deadline_seconds=deadline, semaphore=asyncio.Semaphore(1),
                       target_field_names=["employee_id", "full_name", "work_email"])


def _mapping_request():
    return ProposalRequest(table_id="t", source_table_ref={"table_id": "t"},
                           columns=[ProposalColumn(source_column_id="c1", header="Widget Preference",
                                                   observed_types={"text": 1},
                                                   format_indicators={"redaction_class": "enum"},
                                                   samples=["blue"])])


def _patch_sleep(monkeypatch, client):
    slept: list[float] = []

    async def fake_sleep(d):
        client.completions.events.append(f"sleep:{d}")
        slept.append(d)

    monkeypatch.setattr("app.llm.groq_adapter.asyncio.sleep", fake_sleep)
    return slept


def test_429_with_retry_after_waits_then_succeeds_mapping(monkeypatch):
    client = _FakeGroqClient([_rate_limit_error("5"), '{"proposals": []}'])
    slept = _patch_sleep(monkeypatch, client)
    adapter = _adapter(client)

    resp, meta = asyncio.run(adapter.propose_mappings(schema_public={}, request=_mapping_request()))

    assert meta.attempts == 2                       # bounded: one wait, one success
    assert slept == [5.0]                            # honoured the exact Retry-After interval
    # ordering: the wait happened BEFORE the retry request (no premature request)
    assert client.completions.events == ["create", "sleep:5.0", "create"]


def test_429_with_retry_after_waits_then_succeeds_transform(monkeypatch):
    client = _FakeGroqClient([_rate_limit_error("3"), '{"mappings": []}'])
    slept = _patch_sleep(monkeypatch, client)
    adapter = _adapter(client)
    req = TransformProposalRequest(profile_id="p", source_header="Org Function", target_field="department",
                                   target_label="Department", target_description="",
                                   allowed_values=["Engineering", "Sales"], source_values=["Revenue"])

    resp, meta = asyncio.run(adapter.propose_transforms(request=req))

    assert meta.attempts == 2
    assert slept == [3.0]
    assert client.completions.events == ["create", "sleep:3.0", "create"]


def test_persistent_429_exhausts_bounded_attempts(monkeypatch):
    client = _FakeGroqClient([_rate_limit_error("2")] * 10)   # always rate-limited
    slept = _patch_sleep(monkeypatch, client)
    adapter = _adapter(client, max_attempts=3)

    with pytest.raises(RecoverableModelError):
        asyncio.run(adapter.propose_mappings(schema_public={}, request=_mapping_request()))

    # bounded: exactly max_attempts create calls, and it waited before each retry (no busy-loop)
    assert client.completions.events.count("create") == 3
    assert slept == [2.0, 2.0]                        # waits before attempts 2 and 3, then gives up


def test_retry_after_not_fired_before_wait_expires_when_over_deadline(monkeypatch):
    """If the required Retry-After wait would exceed the operation deadline, the adapter reports a
    recoverable error INSTEAD of retrying early (never a premature request)."""
    client = _FakeGroqClient([_rate_limit_error("120"), '{"proposals": []}'])
    slept = _patch_sleep(monkeypatch, client)
    adapter = _adapter(client, max_attempts=3, deadline=30.0)   # 120s wait > 30s deadline

    with pytest.raises(RecoverableModelError):
        asyncio.run(adapter.propose_mappings(schema_public={}, request=_mapping_request()))

    assert slept == []                                # never slept, never fired the premature retry
    assert client.completions.events == ["create"]
