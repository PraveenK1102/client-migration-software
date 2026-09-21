"""Golden evaluation harness gate (order Phase F).

Runs the repository-owned decision-quality evaluation and enforces the two invariants that matter:
zero UNSAFE automatic actions (the system never silently does the wrong thing) and high overall
decision accuracy. Deterministic — no model, no network.
"""
from __future__ import annotations

from app.eval_harness import run_eval


def test_eval_harness_no_unsafe_auto_and_high_accuracy():
    report = run_eval()
    assert report["total_cases"] >= 30
    # The single most important guarantee: never auto-resolve to a wrong answer.
    assert report["unsafe_auto"] == 0, report["buckets"]
    # And it should not be needlessly conservative either.
    assert report["unnecessary_escalate"] == 0, report["buckets"]
    assert report["accuracy"] >= 0.95, report


def test_eval_harness_covers_all_dimensions():
    dims = {c["dimension"] for c in run_eval()["cases"]}
    assert {"date", "date_century", "enum", "enum_lexical", "relationship", "reference",
            "required_field", "safety"} <= dims


def test_eval_harness_safety_dimension_present():
    names = {c["name"] for c in run_eval()["cases"]}
    # The safety net must include the M3D P0 invariants (no row/column loss, PII/injection safety).
    assert {"no_silent_row_loss", "row_limit_overflow_rejects", "unknown_field_proposed_not_dropped",
            "pii_redaction_no_raw_to_model", "prompt_injection_masked"} <= names
