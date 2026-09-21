"""M3D: model-call observability is REAL and PII-safe.

- tracing disabled  -> a no-op tracer, no LangSmith import/dependency, metrics still persisted;
- tracing enabled   -> exactly one sanitized span per model call, carrying model/attempts/latency/
                       tokens, and NEVER raw names/emails/phones/ids or the raw prompt;
- latency + token counts are actually captured from the Groq completion (usage) — not invented.
"""
from __future__ import annotations

import asyncio
import json
import types

import httpx

from app.db import Database
from app.ingest import parse_file
from app.llm.base import ProposalCallMeta, ProposalRequest
from app.llm.groq_adapter import GroqAdapter
from app.model_projection import to_model_safe_column
from app.observability import (
    ModelTracer,
    build_tracer,
    configure_tracing,
    model_call_metrics,
    record_model_call,
)
from app.profiling import profile_table
from app.schema_loader import get_target_schema

_CSV = (
    "FullName,WorkEmail,MobilePhone,EmployeeCode\n"
    "Priya Nair,priya.nair@acme.com,+91 98765 43210,00042\n"
    "Rahul Mehta,rahul.mehta@acme.com,+91 91234 55501,00043\n"
    "Sara Khan,sara.khan@acme.com,+91 90000 12377,00044\n"
).encode()
_RAW_PII = ["Priya", "Nair", "priya.nair@acme.com", "98765 43210", "00042", "Rahul", "Sara"]


class _RecordingTracer(ModelTracer):
    enabled = True

    def __init__(self):
        self.spans: list[dict] = []

    def model_call(self, **kwargs):
        self.spans.append(kwargs)


def _db(temp_settings) -> Database:
    return Database(temp_settings.app_db_path, busy_timeout_ms=temp_settings.sqlite_busy_timeout_ms)


def _col_summary():
    profs = profile_table(*_parsed())
    cols = [to_model_safe_column(p) for p in profs]
    return [{"header": c.header, "redaction_class": (c.format_indicators or {}).get("redaction_class")}
            for c in cols]


def _parsed():
    pf = parse_file(filename="p.csv", data=_CSV, stored_name="s", max_bytes=10_000_000, max_rows=1000)
    return pf.tables[0], pf.records


# ------------------------------------------------------------- disabled path ------------------
def test_tracing_disabled_is_noop_and_metrics_still_persist(temp_settings):
    assert temp_settings.tracing_enabled is False
    status = configure_tracing(temp_settings)
    assert status["enabled"] is False

    tracer = build_tracer(temp_settings)
    assert tracer.enabled is False and type(tracer).__name__ == "ModelTracer"

    db = _db(temp_settings)
    job = db.create_job(schema_version="employee.v2", provider="fake", model_id="fake/x", adapter_kind="fake")
    meta = ProposalCallMeta(adapter_kind="fake", model_id="fake/x", attempts=1)
    record_model_call(db, tracer, job_id=job, kind="mapping_proposal", table_id="t",
                      adapter_kind="fake", model_id="fake/x", columns_summary=_col_summary(),
                      meta=meta, status="ok", n_proposals=4)
    m = model_call_metrics(db, job)
    assert m["calls"] == 1 and m["ok"] == 1 and m["by_kind"]["mapping_proposal"] == 1


# ------------------------------------------------------------- enabled (recording) path --------
def test_enabled_tracer_emits_one_sanitized_span_without_pii(temp_settings):
    tracer = _RecordingTracer()
    db = _db(temp_settings)
    job = db.create_job(schema_version="employee.v2", provider="groq", model_id="openai/gpt-oss-20b",
                        adapter_kind="groq")
    meta = ProposalCallMeta(adapter_kind="groq", model_id="openai/gpt-oss-20b", attempts=2,
                            latency_ms=812.5, prompt_tokens=1200, completion_tokens=90,
                            total_tokens=1290, status="ok")
    record_model_call(db, tracer, job_id=job, kind="mapping_proposal", table_id="t1",
                      adapter_kind="groq", model_id="openai/gpt-oss-20b",
                      columns_summary=_col_summary(), meta=meta, status="ok", n_proposals=4)

    assert len(tracer.spans) == 1
    span = tracer.spans[0]
    blob = json.dumps(span, ensure_ascii=False)
    for pii in _RAW_PII:
        assert pii not in blob, f"PII leaked into the LangSmith span: {pii!r}"
    # sanitized signal IS present
    assert span["outputs"]["attempts"] == 2
    assert span["outputs"]["latency_ms"] == 812.5
    assert span["outputs"]["total_tokens"] == 1290
    assert span["inputs"]["model"] == "openai/gpt-oss-20b"
    headers = {c["header"] for c in span["inputs"]["columns"]}
    assert {"WorkEmail", "FullName"} <= headers            # headers (field names) allowed
    classes = {c["redaction_class"] for c in span["inputs"]["columns"]}
    assert "email" in classes                               # safe shape signal present

    # persisted metric mirrors the span
    m = model_call_metrics(db, job)
    assert m["total_tokens"] == 1290 and m["total_attempts"] == 2 and m["latency_ms_avg"] == 812.5


# ------------------------------------------------------------- latency/tokens ARE real ---------
async def test_latency_and_tokens_captured_from_groq_completion(temp_settings):
    """The adapter must populate latency + token usage from the completion — not fabricate them."""
    usage = types.SimpleNamespace(prompt_tokens=321, completion_tokens=64, total_tokens=385)
    msg = types.SimpleNamespace(content=json.dumps({"proposals": []}))
    completion = types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")],
                                       usage=usage)

    class _Client:
        def __init__(self):
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

        async def _create(self, **kwargs):
            await asyncio.sleep(0.002)   # non-zero latency to observe
            return completion

    adapter = GroqAdapter(client=_Client(), model_id="openai/gpt-oss-20b", max_attempts=2,
                          operation_deadline_seconds=30.0, semaphore=asyncio.Semaphore(1),
                          target_field_names=list(get_target_schema().field_names))
    req = ProposalRequest(table_id="t", source_table_ref={"table_id": "t"}, columns=_toy_cols())
    _resp, meta = await adapter.propose_mappings(schema_public={}, request=req)
    assert meta.prompt_tokens == 321 and meta.completion_tokens == 64 and meta.total_tokens == 385
    assert meta.latency_ms > 0.0 and meta.status == "ok"


def _toy_cols():
    profs = profile_table(*_parsed())
    return [to_model_safe_column(p) for p in profs]
