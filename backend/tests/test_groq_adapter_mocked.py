"""Groq adapter tests with a MOCKED client (no network, no key, no charges).

Covers: valid structured proposal, missing/invalid credentials, rate limiting +
retry-budget exhaustion, timeout/server failure, invalid/truncated output with a
single corrective retry, and Retry-After exceeding the operation deadline.
"""
from __future__ import annotations

import asyncio
import json
import types

import httpx
import pytest
from groq import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    RateLimitError,
)

from app.llm.base import (
    ModelConfigError,
    ModelResponseInvalid,
    ProposalColumn,
    ProposalRequest,
    RecoverableModelError,
)
from app.llm.groq_adapter import GroqAdapter
from app.schema_loader import get_target_schema

_REQ = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
FIELDS = list(get_target_schema().field_names)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _fast(*_a, **_k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _fast)


class ScriptedClient:
    """Mimics AsyncGroq: .chat.completions.create pops a scripted action per call."""

    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = 0
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create)
        )

    async def _create(self, **kwargs):
        self.calls += 1
        act = self.actions.pop(0)
        if isinstance(act, Exception):
            raise act
        return act


def _completion(content, finish="stop"):
    msg = types.SimpleNamespace(content=content)
    choice = types.SimpleNamespace(message=msg, finish_reason=finish)
    return types.SimpleNamespace(choices=[choice])


def _valid_content():
    return json.dumps({"proposals": [{
        "source_column_id": "c1", "source_header": "Emp", "proposed_target_field": "employee_id",
        "alternative_target_fields": [], "is_ambiguous": False, "ambiguity_reason": None,
        "evidence": ["header looks like an id"], "confidence": 0.9,
    }]})


def _req():
    return ProposalRequest(table_id="t", source_table_ref={"table_id": "t"},
                           columns=[ProposalColumn("c1", "Emp", {"number": 1}, {}, ["001"])])


def _adapter(client, *, max_attempts=3, deadline=100.0):
    return GroqAdapter(client=client, model_id="openai/gpt-oss-20b", max_attempts=max_attempts,
                       operation_deadline_seconds=deadline, semaphore=asyncio.Semaphore(2),
                       target_field_names=FIELDS)


def _rate_limit(retry_after="0"):
    return RateLimitError("rate limited",
                          response=httpx.Response(429, headers={"retry-after": retry_after}, request=_REQ),
                          body=None)


async def test_valid_structured_proposal():
    c = ScriptedClient([_completion(_valid_content())])
    resp, meta = await _adapter(c).propose_mappings(schema_public={}, request=_req())
    assert resp.proposals[0].proposed_target_field == "employee_id"
    assert meta.attempts == 1 and c.calls == 1


async def test_missing_or_invalid_credentials_not_retried():
    err = AuthenticationError("invalid api key",
                              response=httpx.Response(401, request=_REQ), body=None)
    c = ScriptedClient([err])
    with pytest.raises(ModelConfigError) as ei:
        await _adapter(c).propose_mappings(schema_public={}, request=_req())
    assert c.calls == 1  # not retried
    assert "GROQ_API_KEY" in str(ei.value) or "auth" in str(ei.value).lower()


async def test_bad_request_schema_not_retried():
    err = BadRequestError("invalid response_format schema",
                          response=httpx.Response(400, request=_REQ), body=None)
    c = ScriptedClient([err])
    with pytest.raises(ModelConfigError):
        await _adapter(c).propose_mappings(schema_public={}, request=_req())
    assert c.calls == 1


async def test_unknown_model_not_retried():
    err = NotFoundError("model not found", response=httpx.Response(404, request=_REQ), body=None)
    c = ScriptedClient([err])
    with pytest.raises(ModelConfigError):
        await _adapter(c).propose_mappings(schema_public={}, request=_req())
    assert c.calls == 1


async def test_rate_limit_exhausts_budget():
    c = ScriptedClient([_rate_limit("0"), _rate_limit("0"), _rate_limit("0")])
    with pytest.raises(RecoverableModelError):
        await _adapter(c, max_attempts=3).propose_mappings(schema_public={}, request=_req())
    assert c.calls == 3  # exactly the attempt budget, no more


async def test_timeout_then_server_error_exhausts_budget():
    c = ScriptedClient([
        APITimeoutError(request=_REQ),
        InternalServerError("boom", response=httpx.Response(500, request=_REQ), body=None),
        APIConnectionError(message="conn", request=_REQ),
    ])
    with pytest.raises(RecoverableModelError):
        await _adapter(c, max_attempts=3).propose_mappings(schema_public={}, request=_req())
    assert c.calls == 3


async def test_invalid_output_then_valid_uses_one_corrective_retry():
    c = ScriptedClient([_completion("not json at all"), _completion(_valid_content())])
    resp, meta = await _adapter(c, max_attempts=3).propose_mappings(schema_public={}, request=_req())
    assert resp.proposals[0].proposed_target_field == "employee_id"
    assert meta.attempts == 2 and c.calls == 2


async def test_invalid_output_twice_raises_and_never_fabricates():
    c = ScriptedClient([_completion("{bad"), _completion("{still bad")])
    with pytest.raises(ModelResponseInvalid):
        await _adapter(c, max_attempts=3).propose_mappings(schema_public={}, request=_req())
    # only ONE corrective retry, within budget -> 2 calls, not 3
    assert c.calls == 2


async def test_truncated_output_not_accepted():
    # finish_reason == length -> treated invalid; corrective retry then valid
    c = ScriptedClient([_completion(_valid_content(), finish="length"),
                        _completion(_valid_content())])
    resp, meta = await _adapter(c, max_attempts=3).propose_mappings(schema_public={}, request=_req())
    assert meta.attempts == 2


async def test_retry_after_exceeding_deadline_reports_recoverable_without_retrying():
    c = ScriptedClient([_rate_limit("1000")])  # server says wait 1000s
    with pytest.raises(RecoverableModelError):
        await _adapter(c, max_attempts=3, deadline=5.0).propose_mappings(schema_public={}, request=_req())
    assert c.calls == 1  # did not sleep past the deadline or retry early
