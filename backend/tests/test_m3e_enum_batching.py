"""M3E enum value-domain batching (order §9/§10/§12).

Proves model-call count scales with UNRESOLVED DISTINCT batches, never with employee row count:
- a 10,000-row column with ~600 unresolved distinct labels -> ceil(600/25) sequential transform calls
  through the real ``analyze_source`` path (reads rows from the DB, one persisted plan applied to all
  rows);
- a ~100,000-row / 2,500-distinct domain planned OFFLINE (mocked model) where deterministic
  normalization resolves the bulk and only 150 unresolved distinct values reach the model in 6 batches;
- the configured batch size is honoured, no batch exceeds it, and an over-budget domain is surfaced
  for review rather than silently truncated.

No real Groq: a counting mock adapter records every batch. Fast (large columns are held in memory /
value lists; only the 10k case is persisted, once).
"""
from __future__ import annotations

import asyncio
import math
import time
import tracemalloc

import pytest

from app.llm.base import ModelAdapter, ProposalCallMeta
from app.llm.transform_schema import ValueMapItem, ValueMapResponse
from app.profiling import ColumnProfile
from app.schema_loader import get_target_schema
from app.source_records import RawCell, RawType, SourceRecord, SourceRef, SourceTable

DEPT = ["Engineering", "Sales", "Finance", "People Operations"]


class CountingTransformAdapter(ModelAdapter):
    """Records every propose_transforms batch and maps values via ``mapper`` (src -> (target|None,
    relation)). Never used for mappings. Reports non-zero latency/tokens so metrics look real."""

    def __init__(self, mapper):
        self._mapper = mapper
        self.batches: list[list[str]] = []

    @property
    def kind(self) -> str:
        return "mock"

    @property
    def model_id(self) -> str:
        return "mock/count-1"

    async def propose_mappings(self, *, schema_public, request):  # pragma: no cover - not used here
        raise NotImplementedError

    async def propose_transforms(self, *, request):
        self.batches.append(list(request.source_values))
        items = []
        for sv in request.source_values:
            tv, rel = self._mapper(sv)
            items.append(ValueMapItem(source_value=sv, target_value=tv, relation=rel,
                                      ambiguous=False, evidence=["mock"]))
        meta = ProposalCallMeta(adapter_kind="mock", model_id=self.model_id, attempts=1,
                                latency_ms=1.0, prompt_tokens=10, completion_tokens=5, total_tokens=15)
        return ValueMapResponse(mappings=items), meta


def _round_robin_mapper(sv: str):
    return DEPT[hash(sv) % len(DEPT)], "lexical_semantic_match"


def _case_variants(label: str, n: int) -> list[str]:
    """n DISTINCT raw strings that all canonicalize to ``label`` (alpha case toggles only)."""
    positions = [i for i, ch in enumerate(label) if ch.isalpha()]
    out, seen, i = [], set(), 0
    while len(out) < n and i < (1 << (len(positions) + 1)):
        chars = list(label)
        for b, pos in enumerate(positions):
            chars[pos] = chars[pos].upper() if (i >> b) & 1 else chars[pos].lower()
        s = "".join(chars)
        if s not in seen:
            seen.add(s)
            out.append(s)
        i += 1
    return out


# --------------------------------------------------------------------------- DB-backed helper
def _mk_db(tmp_path):
    from app.db import Database
    db = Database(tmp_path / "app.db", busy_timeout_ms=2000)
    job_id = db.create_job(schema_version="employee.v2", provider="mock",
                           model_id="mock/count-1", adapter_kind="mock")
    return db, job_id


def _seed_enum_column(db, job_id, values, *, header="Team Function", target="department"):
    """Persist a one-column source table + rows + profile + an accepted enum mapping decision."""
    from app.profiling import profile_table
    table = SourceTable(table_id="t_enum", file_id="f1", original_filename="teams.csv",
                        headers=[header], n_rows=len(values))
    db.add_source_table(job_id, table)
    recs = []
    for i, v in enumerate(values, start=1):
        ref = SourceRef(file_id="f1", original_filename="teams.csv", table_id="t_enum",
                        row_number=i, col_index=0, header=header)
        recs.append(SourceRecord(ref=ref, cells=[RawCell(col_index=0, header=header, value=v,
                                                          raw_type=RawType.TEXT, ref=ref)]))
    db.add_source_rows(job_id, recs)
    profiles = profile_table(table, recs)
    db.add_profiles(job_id, profiles)
    pid = profiles[0].profile_id
    db.upsert_decision(job_id, profile_id=pid, table_id="t_enum", source_header=header,
                       target_field=target, status="auto_accepted", actor="system", method="rule",
                       reason="seed")
    return pid


def _plan_for(db, job_id, target="department"):
    from app.transform_plan import TransformPlan
    for r in db.get_transformation_plans(job_id):
        p = TransformPlan.from_row(r)
        if p.kind == "enum" and p.target_field == target:
            return p, r
    return None, None


# =========================================================================== §12.A: 10k end-to-end
def test_10k_rows_600_distinct_calls_scale_with_distinct_not_rows(tmp_path):
    from app.source_intelligence import analyze_source
    n_distinct, n_rows, batch = 600, 10_000, 25
    labels = [f"raw-team-label-{i:04d}" for i in range(n_distinct)]  # non-canonical -> all unresolved
    values = [labels[i % n_distinct] for i in range(n_rows)]
    db, job_id = _mk_db(tmp_path)
    _seed_enum_column(db, job_id, values)
    adapter = CountingTransformAdapter(_round_robin_mapper)
    schema = get_target_schema()

    asyncio.run(analyze_source(db, job_id, schema, adapter, "mock/count-1",
                               enum_batch_size=batch, enum_max_distinct=5000))

    calls = adapter.batches
    assert len(calls) == math.ceil(n_distinct / batch) == 24, len(calls)
    assert all(len(b) <= batch for b in calls), [len(b) for b in calls]
    seen = [v for b in calls for v in b]
    assert len(seen) == n_distinct and set(seen) == set(labels)   # every distinct value, exactly once
    assert len(calls) < n_rows                                    # decoupled from row count

    plan, _ = _plan_for(db, job_id)
    assert plan is not None and plan.status == "auto_accepted"
    vm = plan.operations[-1]["value_map"]
    assert len(vm) == n_distinct
    # one persisted plan applies deterministically to ALL rows, in pure Python, no model call
    from app.prepare import _lookup_value_map
    assert all(_lookup_value_map(v, vm) in DEPT for v in values)


# =========================================================================== §12.B: 100k planner
def test_100k_domain_planner_model_calls_track_unresolved_distinct(tmp_path):
    n_rows, n_unresolved, batch = 100_000, 150, 25
    det_variants = (_case_variants("People Operations", 900) + _case_variants("Engineering", 900)
                    + _case_variants("Finance", 120) + _case_variants("Sales", 30))
    det_variants = det_variants[:2350]
    model_labels = [f"legacy-orgcode-{i:03d}" for i in range(n_unresolved)]  # never canonicalizes
    domain = det_variants + model_labels
    # 100k rows sampling the whole distinct domain (row count >> distinct >> unresolved)
    values = [domain[i % len(domain)] for i in range(n_rows)]

    db, job_id = _mk_db(tmp_path)
    adapter = CountingTransformAdapter(_round_robin_mapper)
    schema = get_target_schema()
    tf = schema.get("department")
    p = ColumnProfile(profile_id="col_planner", table_id="t", col_index=0, header="Division",
                      non_empty_count=n_rows, missing_count=0, distinct_count=len(domain),
                      observed_types={"text": n_rows}, format_indicators={}, samples=[], value_domain=[])
    kept: set[str] = set()
    from app.source_intelligence import _enum_plan

    tracemalloc.start()
    t0 = time.perf_counter()
    asyncio.run(_enum_plan(db, job_id, "t", p, "department", tf, adapter, "mock/count-1", kept,
                           full_values=values, batch_size=batch, max_distinct=2000))
    elapsed = time.perf_counter() - t0
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    peak_mb = peak / 1e6

    calls = adapter.batches
    # THE HEADLINE: calls track UNRESOLVED DISTINCT, not total distinct (2500) and not rows (100000)
    assert len(calls) == math.ceil(n_unresolved / batch) == 6, len(calls)
    assert all(len(b) <= batch for b in calls)
    seen = [v for b in calls for v in b]
    assert set(seen) == set(model_labels) and len(seen) == n_unresolved   # only the unresolved distinct
    assert len(calls) < len(domain) < n_rows

    plan, _ = _plan_for(db, job_id)
    assert plan.status == "auto_accepted"
    vm = plan.operations[-1]["value_map"]
    assert len(vm) == len(domain)          # every distinct value accounted (det + model), none lost
    from app.prepare import _lookup_value_map
    # spot-check row application across the whole 100k without any per-row model call
    assert all(_lookup_value_map(values[i], vm) in DEPT for i in range(0, n_rows, 137))
    print(f"[100k planner] rows={n_rows} distinct={len(domain)} unresolved={n_unresolved} "
          f"model_calls={len(calls)} time={elapsed:.2f}s peak_mem={peak_mb:.1f}MB")
    assert elapsed < 30.0 and peak_mb < 400.0   # generous bounds; records runtime/memory


# =========================================================================== §10: batch size + budget
def test_configured_batch_size_is_honoured(tmp_path):
    from app.source_intelligence import _enum_plan
    labels = [f"code-{i:03d}" for i in range(150)]
    db, job_id = _mk_db(tmp_path)
    adapter = CountingTransformAdapter(_round_robin_mapper)
    tf = get_target_schema().get("department")
    p = ColumnProfile(profile_id="c1", table_id="t", col_index=0, header="Div", non_empty_count=150,
                      missing_count=0, distinct_count=150, observed_types={}, format_indicators={},
                      samples=[], value_domain=[])
    asyncio.run(_enum_plan(db, job_id, "t", p, "department", tf, adapter, "m", set(),
                           full_values=labels, batch_size=10, max_distinct=1000))
    assert len(adapter.batches) == 15 and all(len(b) <= 10 for b in adapter.batches)


def test_over_budget_domain_is_reviewed_not_truncated(tmp_path):
    """A domain larger than the model budget is NOT silently truncated: the overflow becomes review."""
    from app.source_intelligence import _enum_plan
    labels = [f"code-{i:03d}" for i in range(150)]
    db, job_id = _mk_db(tmp_path)
    adapter = CountingTransformAdapter(_round_robin_mapper)
    tf = get_target_schema().get("department")
    p = ColumnProfile(profile_id="c2", table_id="t", col_index=0, header="Div", non_empty_count=150,
                      missing_count=0, distinct_count=150, observed_types={}, format_indicators={},
                      samples=[], value_domain=[])
    kept: set[str] = set()
    asyncio.run(_enum_plan(db, job_id, "t", p, "department", tf, adapter, "m", kept,
                           full_values=labels, batch_size=25, max_distinct=20))
    # only the 20-value budget was sent (1 batch); nothing beyond it was quietly dropped
    assert len(adapter.batches) == 1 and len(adapter.batches[0]) == 20
    plan, _ = _plan_for(db, job_id)
    assert plan.status == "needs_review"
    remaining = plan.evidence.get("remaining", [])
    assert len(remaining) == 130 and plan.evidence.get("over_budget_distinct") == 130
    # every distinct value is still accounted for: mapped (20) + remaining-for-review (130) == 150
    assert len(plan.operations[-1]["value_map"]) + len(remaining) == 150


def test_no_row_level_model_calls(tmp_path):
    """Sanity: with M rows and a tiny distinct domain, exactly ONE batch is sent (not M)."""
    from app.source_intelligence import _enum_plan
    values = ["Widget Team"] * 5000 + ["Gadget Team"] * 5000   # 10k rows, 2 distinct
    db, job_id = _mk_db(tmp_path)
    adapter = CountingTransformAdapter(_round_robin_mapper)
    tf = get_target_schema().get("department")
    p = ColumnProfile(profile_id="c3", table_id="t", col_index=0, header="Div", non_empty_count=10000,
                      missing_count=0, distinct_count=2, observed_types={}, format_indicators={},
                      samples=[], value_domain=[])
    asyncio.run(_enum_plan(db, job_id, "t", p, "department", tf, adapter, "m", set(),
                           full_values=values, batch_size=25, max_distinct=1000))
    assert len(adapter.batches) == 1 and len(adapter.batches[0]) == 2
