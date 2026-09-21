"""LIVE evaluation-mode smoke test — REAL Groq calls.

Skipped by default. Runs only when RUN_LIVE_GROQ=1 AND a Groq key is available (process env or
backend/.env). It proves the OPTIONAL live evaluation path: the bounded real-model probes run, are
validated by the SAME deterministic guardrails as the product, are clearly labelled LIVE, and never
break the unsafe_auto gate. It makes a small, bounded number of model calls.

    RUN_LIVE_GROQ=1 pytest tests/test_eval_harness_live.py -v -s
"""
from __future__ import annotations

import os

import pytest

from app.eval_harness import run_eval, unsafe_auto_gate_ok


def _live_enabled() -> bool:
    if os.getenv("RUN_LIVE_GROQ") != "1":
        return False
    if os.getenv("GROQ_API_KEY"):
        return True
    try:
        from app.config import get_settings
        return bool(get_settings().groq_key)
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _live_enabled(),
    reason="Live eval test: set RUN_LIVE_GROQ=1 and a GROQ_API_KEY (env or backend/.env).",
)


@pytest.mark.live
def test_live_eval_runs_bounded_probes_and_holds_the_gate():
    report = run_eval(mode="live")
    assert report["mode"] == "live"
    # Live appends bounded probes on top of the 32 deterministic golden cases.
    assert report["total_cases"] > 32
    live_cases = [c for c in report["cases"] if c["ai_involved"]]
    assert live_cases, "expected at least one AI-backed live probe"
    assert all(c["model_calls"] >= 1 for c in live_cases)
    # The critical invariant holds live too: no unsafe automatic action.
    assert report["unsafe_auto"] == 0, report["buckets"]
    assert unsafe_auto_gate_ok(report) is True
    # The deterministic golden core is still present and correct alongside the live probes.
    assert sum(1 for c in report["cases"] if not c["ai_involved"]) == 32
