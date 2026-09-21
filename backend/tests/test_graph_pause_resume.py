"""Mapping acceptance proof (deterministic-first + fake adapter):

  rule/model automatic mapping -> genuine escalation -> human correction
  -> resume the SAME persisted thread -> "Mapping complete"

Plus interrupt-in-checkpoint, durable restart-while-paused, idempotent/stale decisions,
and instruction-text-as-data. These drive the mapping graph directly (thread_id=job_id)
to isolate mapping from the M2 auto-continuation.
"""
from __future__ import annotations

from langgraph.types import Command

from app.runtime import AppContext
from tests.conftest import ingest_sample_job

CFG = lambda job_id: {"configurable": {"thread_id": job_id}}


async def _run_map(ctx, job_id):
    await ctx.mapping_graph.ainvoke({"job_id": job_id, "schema_version": ctx.schema.version}, CFG(job_id))


async def _resume_map(ctx, job_id, payload):
    await ctx.mapping_graph.ainvoke(Command(resume=payload), CFG(job_id))


def _open_start_issue(ctx, job_id):
    issues = ctx.db.get_issues(job_id, status="open")
    start = [i for i in issues if i["source_header"] == "Start"]
    assert start, f"expected a 'Start' escalation, got {[i['source_header'] for i in issues]}"
    return start[0]


async def test_autonomous_mapping_then_genuine_interrupt(ctx, sample_files):
    job_id = ingest_sample_job(ctx, sample_files)
    await _run_map(ctx, job_id)

    assert ctx.db.get_job(job_id)["status"] == "awaiting_review"
    decisions = ctx.db.get_decisions(job_id)
    assert len([d for d in decisions if d["status"] == "auto_accepted"]) >= 10
    # Deterministic-first: some columns resolved by rule with NO model call.
    assert any(d["method"] == "rule" for d in decisions)
    assert any(d["method"] == "model" for d in decisions)

    open_issues = ctx.db.get_issues(job_id, status="open")
    assert len(open_issues) == 1 and open_issues[0]["source_header"] == "Start"

    snap = await ctx.mapping_graph.aget_state(CFG(job_id))
    assert "await_review" in snap.next
    assert snap.values.get("job_id") == job_id


async def test_human_correction_resumes_same_thread_and_changes_result(ctx, sample_files):
    job_id = ingest_sample_job(ctx, sample_files)
    await _run_map(ctx, job_id)
    issue = _open_start_issue(ctx, job_id)

    assert ctx.db.resolve_issue_if_current(
        issue["id"], expected_version=issue["version"],
        resolution={"action": "correct", "corrected_target": "contract_start_date",
                    "reason": "contract export", "actor": "human"}) == "resolved"
    await _resume_map(ctx, job_id, {"applied": True})

    job = ctx.db.get_job(job_id)
    assert job["status"] == "mapping_complete" and job["thread_id"] == job_id
    dec = {d["profile_id"]: d for d in ctx.db.get_decisions(job_id)}[issue["profile_id"]]
    assert dec["target_field"] == "contract_start_date" and dec["status"] == "corrected"


async def test_refresh_and_backend_restart_while_paused(ctx, sample_files, temp_settings):
    job_id = ingest_sample_job(ctx, sample_files)
    await _run_map(ctx, job_id)
    assert ctx.db.get_job(job_id)["status"] == "awaiting_review"
    issue = _open_start_issue(ctx, job_id)

    await ctx.aclose()
    ctx2 = await AppContext.create(temp_settings)
    try:
        again = ctx2.db.get_issues(job_id, status="open")
        assert len(again) == 1 and again[0]["id"] == issue["id"]
        assert ctx2.db.get_job(job_id)["status"] == "awaiting_review"
        ctx2.db.resolve_issue_if_current(
            issue["id"], expected_version=issue["version"],
            resolution={"action": "approve", "corrected_target": None, "actor": "human"})
        await ctx2.mapping_graph.ainvoke(Command(resume={"applied": True}), CFG(job_id))
        assert ctx2.db.get_job(job_id)["status"] == "mapping_complete"
    finally:
        await ctx2.aclose()


async def test_duplicate_and_stale_decisions_are_safe(ctx, sample_files):
    job_id = ingest_sample_job(ctx, sample_files)
    await _run_map(ctx, job_id)
    issue = _open_start_issue(ctx, job_id)
    res = {"action": "correct", "corrected_target": "contract_start_date", "actor": "human"}

    assert ctx.db.resolve_issue_if_current(issue["id"], expected_version=issue["version"], resolution=res) == "resolved"
    assert ctx.db.resolve_issue_if_current(issue["id"], expected_version=issue["version"], resolution=res) == "noop"
    assert ctx.db.resolve_issue_if_current(
        issue["id"], expected_version=issue["version"],
        resolution={"action": "approve", "corrected_target": None, "actor": "human"}) == "stale"

    await _resume_map(ctx, job_id, {"applied": True})
    audit1 = [a for a in ctx.db.get_audit(job_id) if a["event_type"] == "issue_resolved"]
    await _resume_map(ctx, job_id, {"applied": True})
    audit2 = [a for a in ctx.db.get_audit(job_id) if a["event_type"] == "issue_resolved"]
    assert len(audit1) == len(audit2) == 1
    decs = [d for d in ctx.db.get_decisions(job_id) if d["profile_id"] == issue["profile_id"]]
    assert len(decs) == 1


async def test_embedded_instruction_text_is_data_not_command(ctx, sample_files):
    fields_before = tuple(ctx.schema.field_names)
    job_id = ingest_sample_job(ctx, sample_files)
    found = any("Ignore previous instructions" in r["cells"]
               for t in ctx.db.get_tables(job_id) for r in ctx.db.get_rows_for_table(t["id"]))
    assert found
    await _run_map(ctx, job_id)
    assert tuple(ctx.schema.field_names) == fields_before
    full_name = [d for d in ctx.db.get_decisions(job_id) if d["source_header"] == "Full Name"]
    assert full_name and full_name[0]["target_field"] == "full_name"
