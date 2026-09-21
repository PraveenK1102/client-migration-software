"""M2 + deterministic-route integration (graph-level, offline).

Uses a SpyAdapter that FAILS if the model is called, to prove zero model calls on
canonical tables and throughout preparation. Also covers record review resume,
order-invariance, restart-while-paused, and provider-block -> retry.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from langgraph.types import Command

from app.ingest import parse_file
from app.llm.base import ModelAdapter, ProposalCallMeta
from app.llm.fake_adapter import FakeModelAdapter
from app.runtime import AppContext

REPO = Path(__file__).resolve().parent.parent.parent
SAMPLE = REPO / "sample-data"
CFG = lambda tid: {"configurable": {"thread_id": tid}}


class SpyAdapter(ModelAdapter):
    """Fails loudly if the model is ever called."""
    def __init__(self):
        self.calls = 0

    @property
    def kind(self):
        return "spy"

    @property
    def model_id(self):
        return "spy/never-call"

    async def propose_mappings(self, *, schema_public, request):
        self.calls += 1
        raise AssertionError("model was called but the table should be fully deterministic")


def _ingest_bytes(ctx, name, data: bytes) -> str:
    fid = f"file_{uuid.uuid4().hex[:12]}"
    pf = parse_file(filename=name, data=data, stored_name=f"{fid}.csv",
                    max_bytes=ctx.settings.max_upload_bytes, max_rows=ctx.settings.max_rows_per_table,
                    file_id=fid)
    job_id = ctx.db.create_job(schema_version=ctx.schema.version, provider=ctx.provider,
                               model_id=ctx.model_id, adapter_kind=ctx.adapter_kind)
    d = ctx.settings.uploads_dir / job_id
    d.mkdir(parents=True, exist_ok=True)
    (d / pf.stored_name).write_bytes(data)
    ctx.db.add_source_file(job_id, file_id=pf.file_id, original_filename=pf.original_filename,
                           stored_name=pf.stored_name, content_type=pf.content_type, size_bytes=pf.size_bytes)
    for t in pf.tables:
        ctx.db.add_source_table(job_id, t)
    ctx.db.add_source_rows(job_id, pf.records)
    ctx.db.add_parsing_issues(job_id, pf.issues)
    ctx.db.set_job_stage(job_id, status="processing", stage="ingested")
    return job_id


def _ingest(ctx, name) -> str:
    return _ingest_bytes(ctx, name, (SAMPLE / name).read_bytes())


async def _map(ctx, jid):
    await ctx.mapping_graph.ainvoke({"job_id": jid, "schema_version": ctx.schema.version}, CFG(jid))


async def _prep(ctx, jid):
    ctx.db.set_job_threads(jid, prep_thread=f"{jid}:prep")
    await ctx.preparation_graph.ainvoke({"job_id": jid}, CFG(f"{jid}:prep"))


async def _resume_prep(ctx, jid, payload):
    await ctx.preparation_graph.ainvoke(Command(resume=payload), CFG(f"{jid}:prep"))


async def _spy_ctx(temp_settings):
    return await AppContext.create(temp_settings, adapter_override=SpyAdapter(), use_override=True)


async def test_canonical_table_zero_model_calls(temp_settings):
    ctx = await _spy_ctx(temp_settings)
    try:
        jid = _ingest(ctx, "canonical_clean.csv")
        await _map(ctx, jid)
        assert ctx.db.get_job(jid)["status"] == "mapping_complete"
        assert {d["method"] for d in ctx.db.get_decisions(jid)} == {"rule"}
        assert ctx.adapter.calls == 0
        await _prep(ctx, jid)
        job = ctx.db.get_job(jid)
        assert job["status"] == "preparation_complete"
        cands = ctx.db.get_candidates(jid)
        assert len(cands) == 6 and all(c["eligibility"] == "eligible" for c in cands)
        assert ctx.adapter.calls == 0   # zero model calls in mapping AND preparation
    finally:
        await ctx.aclose()


async def test_messy_record_issues_zero_model_then_resolve(temp_settings):
    ctx = await _spy_ctx(temp_settings)
    try:
        jid = _ingest(ctx, "canonical_messy.csv")
        await _map(ctx, jid)
        await _prep(ctx, jid)
        assert ctx.db.get_job(jid)["status"] == "awaiting_record_review"
        types = {i["issue_type"] for i in ctx.db.get_record_issues(jid, status="open")}
        # M3C auto-infers the column date convention (decisive DMY evidence: 13/04/2024, 31/02/2024),
        # so the individually-ambiguous 03/04/2024 is resolved automatically and NO ambiguous_date
        # review is raised. Only the genuinely impossible 31/02/2024 (Feb 31) remains — as invalid_value.
        assert "ambiguous_date" not in types
        assert {"value_conflict", "shared_email", "invalid_value",
                "missing_required", "unknown_enum"} <= types
        assert ctx.adapter.calls == 0

        # Resolve every open issue deterministically (no model), then resume.
        for iss in ctx.db.get_record_issues(jid, status="open"):
            import json as _j
            t, opts = iss["issue_type"], _j.loads(iss["options"])
            if t == "ambiguous_date":                       # M3C: one column-scoped convention decision
                res = {"action": "confirm_convention", "convention": opts[0]["meaning"]}
            elif t == "invalid_value":
                res = {"action": "correct", "value": "2020-01-01"}
            elif t == "missing_required":
                res = {"action": "correct", "value": "gina.fixed@acme.com"}
            elif t == "unknown_enum":                       # M3C: one column-scoped value-map decision
                _um = [d["value"] for d in _j.loads(iss["affected"]).get("distinct_values", [])]
                res = {"action": "map_values", "value_map": {v: opts[0] for v in _um}}
            elif t == "value_conflict":
                res = {"action": "select", "value": opts[0]}
            elif t == "shared_email":
                import json as _j
                victim = _j.loads(iss["affected"])["candidates"][1]["candidate_id"]
                res = {"action": "exclude", "candidate_id": victim, "reason": "duplicate person"}
            else:
                res = {"action": "exclude"}
            res["actor"] = "human"
            assert ctx.db.resolve_record_issue_if_current(iss["id"], expected_version=iss["version"],
                                                          resolution=res) == "resolved"
        await _resume_prep(ctx, jid, {"applied": True})
        job = ctx.db.get_job(jid)
        assert job["status"] == "preparation_complete"
        assert ctx.adapter.calls == 0
        elig = [c for c in ctx.db.get_candidates(jid) if c["eligibility"] == "eligible"]
        assert len(elig) >= 1
    finally:
        await ctx.aclose()


async def test_idempotent_repeat_preparation_no_model(temp_settings):
    ctx = await _spy_ctx(temp_settings)
    try:
        jid = _ingest(ctx, "canonical_clean.csv")
        await _map(ctx, jid)
        await _prep(ctx, jid)
        n1 = len(ctx.db.get_candidates(jid))
        audit1 = len(ctx.db.get_audit(jid))
        # Re-run preparation on a fresh prep thread: candidates replaced, not duplicated.
        ctx.db.set_job_threads(jid, prep_thread=f"{jid}:prep2")
        await ctx.preparation_graph.ainvoke({"job_id": jid}, CFG(f"{jid}:prep2"))
        assert len(ctx.db.get_candidates(jid)) == n1   # replaced, not doubled
        assert ctx.adapter.calls == 0
        assert len(ctx.db.get_audit(jid)) >= audit1
    finally:
        await ctx.aclose()


async def test_large_table_error_beyond_sample_window(temp_settings):
    ctx = await _spy_ctx(temp_settings)
    try:
        jid = _ingest(ctx, "canonical_large.csv")
        await _map(ctx, jid)
        await _prep(ctx, jid)
        issues = ctx.db.get_record_issues(jid, status="open")
        # The impossible date at row 25 (id 0125) is flagged despite being past the 5-value sample.
        assert any(i["issue_type"] == "invalid_value" and i["candidate_key"] == "0125" for i in issues)
        assert ctx.adapter.calls == 0
    finally:
        await ctx.aclose()


async def test_order_invariance_of_conflict(temp_settings):
    ctx = await _spy_ctx(temp_settings)
    try:
        raw = (SAMPLE / "canonical_messy.csv").read_text().splitlines()
        header, rows = raw[0], raw[1:]
        forward = ("\n".join([header] + rows) + "\n").encode()
        reversed_ = ("\n".join([header] + list(reversed(rows))) + "\n").encode()

        async def run(data):
            jid = _ingest_bytes(ctx, "m.csv", data)
            await _map(ctx, jid)
            await _prep(ctx, jid)
            import json as _j
            for i in ctx.db.get_record_issues(jid, status="open"):
                if i["issue_type"] == "value_conflict" and i["candidate_key"] == "019":
                    return sorted(_j.loads(i["options"]))
            return None

        a = await run(forward)
        b = await run(reversed_)
        assert a is not None and a == b   # same conflict options regardless of row order
    finally:
        await ctx.aclose()


async def test_restart_while_paused_record_review(temp_settings):
    ctx = await _spy_ctx(temp_settings)
    jid = _ingest(ctx, "canonical_messy.csv")
    await _map(ctx, jid)
    await _prep(ctx, jid)
    assert ctx.db.get_job(jid)["status"] == "awaiting_record_review"
    open_before = {i["id"] for i in ctx.db.get_record_issues(jid, status="open")}
    await ctx.aclose()

    ctx2 = await _spy_ctx(temp_settings)
    try:
        again = {i["id"] for i in ctx2.db.get_record_issues(jid, status="open")}
        assert again == open_before and ctx2.db.get_job(jid)["status"] == "awaiting_record_review"
        # Resolve everything and resume on the fresh context (checkpoint reloaded).
        for iss in ctx2.db.get_record_issues(jid, status="open"):
            import json as _j
            t, opts = iss["issue_type"], _j.loads(iss["options"])
            if t == "ambiguous_date":                       # M3C: one column-scoped convention decision
                res = {"action": "confirm_convention", "convention": opts[0]["meaning"]}
            elif t == "invalid_value":
                res = {"action": "correct", "value": "2020-01-01"}
            elif t == "missing_required":
                res = {"action": "correct", "value": "gina2@acme.com"}
            elif t == "unknown_enum":                       # M3C: one column-scoped value-map decision
                _um = [d["value"] for d in _j.loads(iss["affected"]).get("distinct_values", [])]
                res = {"action": "map_values", "value_map": {v: opts[0] for v in _um}}
            elif t == "value_conflict":
                res = {"action": "select", "value": opts[0]}
            elif t == "shared_email":
                victim = _j.loads(iss["affected"])["candidates"][1]["candidate_id"]
                res = {"action": "exclude", "candidate_id": victim}
            else:
                res = {"action": "exclude"}
            res["actor"] = "human"
            ctx2.db.resolve_record_issue_if_current(iss["id"], expected_version=iss["version"], resolution=res)
        await ctx2.preparation_graph.ainvoke(Command(resume={"applied": True}), CFG(f"{jid}:prep"))
        assert ctx2.db.get_job(jid)["status"] == "preparation_complete"
    finally:
        await ctx2.aclose()


async def test_provider_blocked_then_retry_preserves_rules(temp_settings):
    # No provider: unresolved columns are blocked; rule decisions preserved.
    ctx = await AppContext.create(temp_settings, adapter_override=None, use_override=True)
    jid = _ingest(ctx, "legacy_hr.csv")
    await _map(ctx, jid)
    job = ctx.db.get_job(jid)
    assert job["status"] == "blocked_provider"
    rule_before = {d["source_header"]: d["target_field"] for d in ctx.db.get_decisions(jid)
                   if d["method"] == "rule"}
    assert rule_before and "Full Name" in rule_before
    rule_audit_before = len([a for a in ctx.db.get_audit(jid)
                             if a["event_type"] == "mapping_rule_accepted"])
    await ctx.aclose()

    # Provider restored (fake): retry on a fresh thread preserves rule decisions, no dup audit.
    ctx2 = await AppContext.create(temp_settings, adapter_override=FakeModelAdapter(), use_override=True)
    try:
        ctx2.db.set_job_threads(jid, map_thread=f"{jid}:m1")
        await ctx2.mapping_graph.ainvoke({"job_id": jid, "schema_version": ctx2.schema.version},
                                         CFG(f"{jid}:m1"))
        job2 = ctx2.db.get_job(jid)
        assert job2["status"] in ("mapping_complete", "awaiting_review")
        rule_after = {d["source_header"]: d["target_field"] for d in ctx2.db.get_decisions(jid)
                      if d["method"] == "rule"}
        assert rule_after == rule_before   # preserved, unchanged
        # No duplicate rule audit events from the retry.
        rule_audit_after = len([a for a in ctx2.db.get_audit(jid)
                                if a["event_type"] == "mapping_rule_accepted"])
        assert rule_audit_after == rule_audit_before
        # No duplicate decisions (one per profile id).
        decs = ctx2.db.get_decisions(jid)
        assert len({d["profile_id"] for d in decs}) == len(decs)
    finally:
        await ctx2.aclose()
