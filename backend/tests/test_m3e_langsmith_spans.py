"""M3E LangSmith model-span content (order §8) — offline plumbing.

Verifies that ``record_model_call`` builds interview-useful, PII-safe span inputs/outputs: the mapping
span carries sanitized header→proposed-target results + schema context; the transform span carries
batch index/total + a bounded value map; both carry attempts/latency/token usage; neither carries raw
values. The actual PARENT/CHILD nesting under a LangGraph node needs a live LangSmith run and is
asserted in the gated live test.
"""
from __future__ import annotations

from app.llm.base import ProposalCallMeta
from app.observability import ModelTracer, record_model_call


class _CapturingTracer(ModelTracer):
    enabled = True

    def __init__(self):
        self.calls = []

    def model_call(self, **kwargs):
        self.calls.append(kwargs)


class _StubDB:
    def __init__(self):
        self.rows = []

    def add_model_call(self, job_id, **kw):
        self.rows.append(kw)


def _meta():
    return ProposalCallMeta(adapter_kind="groq", model_id="openai/gpt-oss-20b", attempts=1,
                            latency_ms=812.5, prompt_tokens=1200, completion_tokens=90,
                            total_tokens=1290, notes=["transient RateLimitError; backoff 2.00s"])


def test_mapping_span_has_sanitized_results_and_tokens():
    tr, db = _CapturingTracer(), _StubDB()
    record_model_call(db, tr, job_id="j1", kind="mapping_proposal", table_id="t",
                      adapter_kind="groq", model_id="openai/gpt-oss-20b",
                      columns_summary=[{"header": "Corporate Email", "redaction_class": "email"}],
                      meta=_meta(), status="ok", n_proposals=1,
                      input_extra={"n_unresolved": 1, "target_schema_version": "employee.v2",
                                   "n_target_paths": 40},
                      output_extra={"proposals": [{"header": "Corporate Email",
                                                   "proposed_target_field": "work_email",
                                                   "is_ambiguous": False}]})
    assert len(tr.calls) == 1
    call = tr.calls[0]
    assert call["name"] == "model.mapping_proposal"
    assert call["inputs"]["columns"] == [{"header": "Corporate Email", "redaction_class": "email"}]
    assert call["inputs"]["n_unresolved"] == 1 and call["inputs"]["target_schema_version"] == "employee.v2"
    assert call["outputs"]["proposals"][0]["proposed_target_field"] == "work_email"
    assert call["outputs"]["total_tokens"] == 1290 and call["outputs"]["prompt_tokens"] == 1200
    assert call["outputs"]["rate_limit_notes"]  # backoff note surfaced
    # persisted metric row too
    assert db.rows and db.rows[0]["total_tokens"] == 1290


def test_transform_span_has_batch_and_value_map():
    tr, db = _CapturingTracer(), _StubDB()
    record_model_call(db, tr, job_id="j1", kind="transform_proposal", table_id="t",
                      adapter_kind="groq", model_id="openai/gpt-oss-20b",
                      columns_summary=[{"header": "Identity Sex", "redaction_class": "enum"}],
                      meta=_meta(), status="ok", n_proposals=3,
                      input_extra={"target_field": "gender", "batch": 1, "batches_total": 1,
                                   "n_distinct_values": 3, "batch_size": 25},
                      output_extra={"value_map": {"Man": "male", "Woman": "female"},
                                    "accepted": 2, "rejected": 0, "taxonomy_deferred": 0})
    call = tr.calls[0]
    assert call["name"] == "model.transform_proposal"
    assert call["inputs"]["batch"] == 1 and call["inputs"]["batches_total"] == 1
    assert call["inputs"]["n_distinct_values"] == 3 and call["inputs"]["target_field"] == "gender"
    assert call["outputs"]["value_map"] == {"Man": "male", "Woman": "female"}
    assert call["outputs"]["accepted"] == 2


def test_span_carries_no_raw_pii_columns_are_header_and_class_only():
    tr, db = _CapturingTracer(), _StubDB()
    record_model_call(db, tr, job_id="j1", kind="mapping_proposal", table_id="t",
                      adapter_kind="groq", model_id="m",
                      columns_summary=[{"header": "Corporate Email", "redaction_class": "email"}],
                      meta=_meta(), status="ok", n_proposals=1)
    blob = f"{tr.calls[0]['inputs']} {tr.calls[0]['outputs']}"
    for pii in ("ava.reyes@northwind-demo.example", "Ava Reyes", "EMP-1001"):
        assert pii not in blob


def test_noop_tracer_emits_nothing_but_still_persists():
    db = _StubDB()
    record_model_call(db, ModelTracer(), job_id="j1", kind="mapping_proposal", table_id="t",
                      adapter_kind="fake", model_id="m",
                      columns_summary=[{"header": "X", "redaction_class": "text"}],
                      meta=_meta(), status="ok", n_proposals=0)
    assert db.rows  # metric persisted even with tracing off
