"""M3E gated LIVE generalization + LangSmith proof (order §6/§15/§16).

Runs the 50-row unseen-header fixture through the REAL application with REAL Groq (and REAL LangSmith
when a key is present) exactly once per module, then asserts the interview claims on the result. Uses
the same driver as ``scripts/llm_generalization_live.py`` so there is one implementation. Skipped
unless RUN_LIVE_GROQ=1 and a Groq key is configured, so normal CI stays offline/deterministic.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "llm_generalization_live.py"


def _live_enabled() -> bool:
    if os.environ.get("RUN_LIVE_GROQ") != "1":
        return False
    from app import config
    config.get_settings.cache_clear()
    return config.get_settings().groq_key is not None


pytestmark = pytest.mark.skipif(
    not _live_enabled(),
    reason="Live generalization test: set RUN_LIVE_GROQ=1 and a GROQ_API_KEY (env or backend/.env).")


def _load_driver():
    spec = importlib.util.spec_from_file_location("m3e_live_driver", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def live_result():
    os.environ["RUN_LIVE_GROQ"] = "1"
    return _load_driver().run_live()


STABLE = {"preparation_complete", "reconciliation_complete", "awaiting_target_review", "migration_complete"}


def test_preflight_headers_were_not_deterministic_aliases(live_result):
    assert live_result["preflight"]["all_unresolved"] is True


def test_alias_inventory_unchanged_by_run(live_result):
    assert live_result["alias_inventory_unchanged"] is True


def test_reached_a_stable_stage(live_result):
    assert live_result["final_status"] in STABLE, live_result["final_status"]


def test_several_unseen_headers_are_model_mapped(live_result):
    model_mapped = live_result["intended_headers_model_mapped"]
    # structurally-anchored, semantically clear headers should reliably be model-mapped
    for h in ("Employee Reference", "Corporate Email", "Joining Effective Date", "Identity Sex"):
        assert h in model_mapped, (h, model_mapped)
    assert len(model_mapped) >= 6, model_mapped


def test_real_groq_calls_with_tokens(live_result):
    mm = live_result["model_metrics"]
    assert mm["calls"] >= 1 and "groq" in mm["adapter_kinds"], mm
    assert (mm["total_tokens"] or 0) > 0, mm


def test_two_enum_value_maps_from_real_groq(live_result):
    """§15: at least two enum value maps come from REAL Groq (gender/status/type via propose_transforms)."""
    mm = live_result["model_metrics"]
    intel = live_result["intelligence"]
    transform_calls = mm.get("by_kind", {}).get("transform_proposal", 0)
    assert transform_calls >= 2 or (intel.get("transforms_model") or 0) >= 2, (transform_calls, intel)


def test_a_business_taxonomy_decision_became_one_review(live_result):
    """§15: at least one business-taxonomy decision becomes ONE human review (department mapping or
    the department VALUE taxonomy)."""
    got_dept_map_review = any(r["header"] == "Org Function" for r in live_result.get("mapping_reviews", []))
    got_value_review = bool(live_result.get("human_taxonomy_resolutions"))
    assert got_dept_map_review or got_value_review, live_result


def test_no_raw_pii_leaked(live_result):
    ls = live_result.get("langsmith", {})
    # PII is kept out of Groq by construction (model_projection) and checked in LangSmith spans here.
    assert ls.get("pii_leak") in (False, None), ls


@pytest.mark.skipif(os.environ.get("RUN_LIVE_LANGSMITH") != "1",
                    reason="LangSmith span assertions: set RUN_LIVE_LANGSMITH=1 + a LANGSMITH_API_KEY.")
def test_langsmith_model_spans_visible_with_tokens(live_result):
    ls = live_result["langsmith"]
    if not ls.get("enabled"):
        pytest.skip("tracing not enabled for this run")
    assert ls.get("model_span_count", 0) >= 1, ls
    assert ls.get("any_tokens") is True, ls
    assert ls.get("pii_leak") is False, ls
