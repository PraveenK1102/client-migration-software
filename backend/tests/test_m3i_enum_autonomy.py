"""M3I enum autonomy — bounded semantic-clarification retry before HITL (order §A).

The enum value-map path already: applies deterministic Tier-1 aliases; sends only unresolved DISTINCT
values to the model; validates every proposal against the fixed target enum; defers taxonomy
translations; and surfaces anything left as ONE column-scoped review. This round adds ONE bounded
semantic-clarification pass so a hesitant first model response no longer forces a human review for a
plainly-synonymous value (Man -> male, Woman -> female, Does not disclose -> undisclosed) — WITHOUT
weakening any safety boundary.

These tests drive :func:`app.source_intelligence._enum_plan` directly with a scripted two-pass adapter
so the retry semantics are provable offline (no network, no real Groq). The deterministic validator
(:func:`app.enum_inference.validate_model_value_map`) is unchanged and still the sole guardrail.
"""
from __future__ import annotations

import asyncio

import pytest

from app.eval_harness import run_eval
from app.llm.base import ModelAdapter, ProposalCallMeta, RecoverableModelError
from app.llm.transform_schema import ValueMapItem, ValueMapResponse
from app.profiling import ColumnProfile
from app.schema_loader import get_target_schema
from app.transform_plan import TransformPlan

GENDER_ENUM = ("female", "male", "non_binary", "undisclosed")


# --------------------------------------------------------------------------- scripted adapter
class TwoPassAdapter(ModelAdapter):
    """Scripted transform adapter. ``first`` answers the initial pass, ``second`` the clarification
    pass (request.clarify=True). Each mapper: source_value -> (target|None, relation, ambiguous).
    Records every batch as {clarify, values} so retry semantics are directly assertable. Optionally
    raises ``error`` on the first pass to prove a model error causes no unsafe fallback."""

    def __init__(self, first, second=None, *, error: Exception | None = None):
        self._first = first
        self._second = second or first
        self._error = error
        self.calls: list[dict] = []

    @property
    def kind(self) -> str:
        return "mock"

    @property
    def model_id(self) -> str:
        return "mock/two-pass-1"

    async def propose_mappings(self, *, schema_public, request):  # pragma: no cover - unused
        raise NotImplementedError

    async def propose_transforms(self, *, request):
        clarify = bool(getattr(request, "clarify", False))
        self.calls.append({"clarify": clarify, "values": list(request.source_values)})
        if self._error is not None and not clarify:
            raise self._error
        mapper = self._second if clarify else self._first
        items = []
        for sv in request.source_values:
            tv, rel, amb = mapper(sv)
            items.append(ValueMapItem(source_value=sv, target_value=tv, relation=rel,
                                      ambiguous=amb, evidence=["mock"]))
        meta = ProposalCallMeta(adapter_kind="mock", model_id=self.model_id, attempts=1,
                                latency_ms=1.0, prompt_tokens=10, completion_tokens=5, total_tokens=15)
        return ValueMapResponse(mappings=items), meta

    @property
    def clarify_calls(self) -> list[dict]:
        return [c for c in self.calls if c["clarify"]]


# --------------------------------------------------------------------------- helpers
def _mk_db(tmp_path):
    from app.db import Database
    db = Database(tmp_path / "app.db", busy_timeout_ms=2000)
    job_id = db.create_job(schema_version="employee.v2", provider="mock",
                           model_id="mock/two-pass-1", adapter_kind="mock")
    return db, job_id


def _gender_profile(distinct_count=3):
    return ColumnProfile(profile_id="c_gender", table_id="t", col_index=0, header="Gender Identity",
                         non_empty_count=distinct_count, missing_count=0, distinct_count=distinct_count,
                         observed_types={"text": distinct_count}, format_indicators={}, samples=[],
                         value_domain=[])


def _run_enum(db, job_id, adapter, values, *, target="gender", batch_size=25, max_distinct=2000):
    tf = get_target_schema().get(target)
    from app.source_intelligence import _enum_plan
    asyncio.run(_enum_plan(db, job_id, "t", _gender_profile(len(set(values))), target, tf, adapter,
                           adapter.model_id, set(), full_values=values,
                           batch_size=batch_size, max_distinct=max_distinct))
    for r in db.get_transformation_plans(job_id):
        p = TransformPlan.from_row(r)
        if p.kind == "enum" and p.target_field == target:
            return p
    return None


DECLINE = (None, "no_match", False)
SEMANTIC = {"Man": ("male", "lexical_semantic_match", False),
            "Woman": ("female", "lexical_semantic_match", False),
            "Does not disclose": ("undisclosed", "lexical_semantic_match", False)}


# =========================================================================== §A tests
def test_1_first_declines_second_resolves_auto_accepts(tmp_path):
    """A model that FIRST declines obvious semantic values and SECOND resolves them -> AUTO_ACCEPTED,
    no unknown_enum review."""
    values = ["Man", "Woman", "Does not disclose"]
    adapter = TwoPassAdapter(first=lambda sv: DECLINE, second=lambda sv: SEMANTIC[sv])
    db, job_id = _mk_db(tmp_path)
    plan = _run_enum(db, job_id, adapter, values * 4)

    assert plan is not None and plan.status == "auto_accepted"
    assert plan.evidence.get("remaining") == []
    vm = plan.operations[-1]["value_map"]
    assert vm == {"Man": "male", "Woman": "female", "Does not disclose": "undisclosed"}
    # exactly two passes happened: one initial, one clarification.
    assert len(adapter.clarify_calls) == 1
    # and no enum column is left needing review.
    from app.source_intelligence import intelligence_metrics
    assert intelligence_metrics(db, job_id)["enum_columns_needing_review"] == 0


def test_2_semantic_retry_is_exactly_once(tmp_path):
    """Even when the clarification pass does not fully resolve, there is NO third pass; the leftover
    goes to review."""
    values = ["Man", "Woman"]
    # first declines both; second resolves only 'Man', still declines 'Woman'.
    second = lambda sv: SEMANTIC["Man"] if sv == "Man" else DECLINE
    adapter = TwoPassAdapter(first=lambda sv: DECLINE, second=second)
    db, job_id = _mk_db(tmp_path)
    plan = _run_enum(db, job_id, adapter, values)

    assert len(adapter.clarify_calls) == 1                       # exactly one clarification pass
    assert len(adapter.calls) == 2                               # 1 initial batch + 1 clarify batch
    vm = plan.operations[-1]["value_map"]
    assert vm == {"Man": "male"}
    assert plan.status == "needs_review" and plan.evidence.get("remaining") == ["Woman"]


def test_3_all_accepted_targets_are_enum_members(tmp_path):
    values = ["Man", "Woman", "Does not disclose"]
    adapter = TwoPassAdapter(first=lambda sv: DECLINE, second=lambda sv: SEMANTIC[sv])
    db, job_id = _mk_db(tmp_path)
    plan = _run_enum(db, job_id, adapter, values)
    for src, tgt in plan.operations[-1]["value_map"].items():
        assert tgt in GENDER_ENUM, (src, tgt)


def test_4_taxonomy_translation_stays_human_review(tmp_path):
    """A taxonomy_translation is never auto-applied and is never sent to the clarification pass."""
    # T = taxonomy (valid target but org-specific); B = a plain decline that the retry can resolve.
    def first(sv):
        return ("non_binary", "taxonomy_translation", False) if sv == "Third Gender Category" else DECLINE
    second = lambda sv: SEMANTIC.get(sv, DECLINE)
    adapter = TwoPassAdapter(first=first, second=second)
    db, job_id = _mk_db(tmp_path)
    plan = _run_enum(db, job_id, adapter, ["Third Gender Category", "Man"])

    # the taxonomy value is NEVER in a clarification batch, and never auto-applied.
    for c in adapter.clarify_calls:
        assert "Third Gender Category" not in c["values"]
    vm = plan.operations[-1]["value_map"]
    assert "Third Gender Category" not in vm and vm.get("Man") == "male"
    assert plan.status == "needs_review"
    assert "Third Gender Category" in plan.evidence.get("remaining", [])
    assert plan.evidence.get("taxonomy_decision") is True


def test_5_ambiguous_true_cannot_be_overridden_by_retry(tmp_path):
    """A value the model flags ambiguous on the first pass is sticky: never retried, never overridden,
    even though the (unused) second mapper WOULD resolve it."""
    def first(sv):
        return (None, "ambiguous", True) if sv == "Ambi" else DECLINE
    # a permissive second pass that would map EVERYTHING (including 'Ambi') if it were ever asked.
    second = lambda sv: ("male", "lexical_semantic_match", False)
    adapter = TwoPassAdapter(first=first, second=second)
    db, job_id = _mk_db(tmp_path)
    plan = _run_enum(db, job_id, adapter, ["Ambi", "Man"])

    for c in adapter.clarify_calls:
        assert "Ambi" not in c["values"]                        # ambiguous never re-sent
    vm = plan.operations[-1]["value_map"]
    assert "Ambi" not in vm                                     # ambiguous never auto-applied
    assert vm.get("Man") == "male"                              # the plain decline still resolves
    assert plan.status == "needs_review" and "Ambi" in plan.evidence.get("remaining", [])


def test_6_out_of_enum_target_is_rejected_and_reviewed(tmp_path):
    """A model target outside the fixed enum is rejected by the deterministic validator on BOTH passes
    and left for review — never applied."""
    bad = lambda sv: ("director", "lexical_semantic_match", False)   # 'director' not in gender enum
    adapter = TwoPassAdapter(first=bad, second=bad)
    db, job_id = _mk_db(tmp_path)
    plan = _run_enum(db, job_id, adapter, ["Weird Value"])
    vm = plan.operations[-1]["value_map"]
    assert "Weird Value" not in vm
    assert plan.status == "needs_review" and plan.evidence.get("remaining") == ["Weird Value"]


def test_7_model_error_first_pass_no_unsafe_fallback_no_retry(tmp_path):
    """A model error on the first pass: no clarification pass is attempted and nothing is auto-applied;
    the values stay for the existing column-scoped review."""
    adapter = TwoPassAdapter(first=lambda sv: SEMANTIC[sv],
                             error=RecoverableModelError("simulated 429 storm"))
    db, job_id = _mk_db(tmp_path)
    plan = _run_enum(db, job_id, adapter, ["Man", "Woman", "Does not disclose"])

    assert adapter.clarify_calls == []                          # no retry after a model error
    assert plan.operations[-1]["value_map"] == {}              # nothing auto-applied
    assert plan.status == "needs_review"
    assert plan.evidence.get("model_error") == "RecoverableModelError"


def test_8_call_count_scales_with_distinct_not_rows(tmp_path):
    """6,000 rows, 3 distinct values: exactly 2 model calls total (1 initial + 1 clarification)."""
    distinct = ["Man", "Woman", "Does not disclose"]
    values = [distinct[i % 3] for i in range(6000)]
    adapter = TwoPassAdapter(first=lambda sv: DECLINE, second=lambda sv: SEMANTIC[sv])
    db, job_id = _mk_db(tmp_path)
    plan = _run_enum(db, job_id, adapter, values)

    assert len(adapter.calls) == 2 and len(adapter.calls) < len(values)
    # the clarification batch carried only the 3 distinct declined values, never 6,000 rows.
    assert set(adapter.clarify_calls[0]["values"]) == set(distinct)
    assert plan.status == "auto_accepted"


def test_8b_max_one_clarification_per_batch(tmp_path):
    """With more distinct declines than one batch holds, each still gets EXACTLY one clarification
    (clarify batches == initial batches), never a second retry of the same value."""
    values = [f"syn-{i:02d}" for i in range(30)]                 # 30 distinct -> 2 batches at size 20
    resolve = lambda sv: ("male", "lexical_semantic_match", False)
    adapter = TwoPassAdapter(first=lambda sv: DECLINE, second=resolve)
    db, job_id = _mk_db(tmp_path)
    plan = _run_enum(db, job_id, adapter, values, batch_size=20)

    initial = [c for c in adapter.calls if not c["clarify"]]
    clarify = adapter.clarify_calls
    assert len(initial) == 2 and len(clarify) == 2              # one clarify per initial batch, no more
    # every declined distinct value appears in exactly one clarification batch.
    seen = [v for c in clarify for v in c["values"]]
    assert sorted(seen) == sorted(values)
    assert plan.status == "auto_accepted"


def test_9_eval_hard_gate_unsafe_auto_zero(tmp_path):
    """The repository decision-quality gate stays green: the autonomy change never auto-resolves to a
    wrong answer (unsafe_auto == 0), including the enum/enum_lexical dimensions."""
    report = run_eval()
    assert report["unsafe_auto"] == 0, report["buckets"]
    dims = {c["dimension"] for c in report["cases"]}
    assert {"enum", "enum_lexical"} <= dims
