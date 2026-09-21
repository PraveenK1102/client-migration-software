"""Observability (M3D): optional LangSmith model-call tracing + product/engineering metrics.

Two independent concerns:

* **Engineering tracing** — when LangSmith is configured (key present AND tracing on):
  - LangGraph runs auto-emit node traces to the configured project (node timing), and
  - the workflow emits ONE explicit ``run_type="llm"`` span per model call (mapping proposal and
    transform proposal) via :class:`LangSmithTracer` — carrying model name, attempt count, latency,
    and input/output token counts when Groq returns usage. Because the Groq SDK is called directly
    (not through a LangChain chat model), these token/latency spans are created MANUALLY here rather
    than by auto-instrumentation, so the claim "model latency/tokens are observable" is actually true.
  It is strictly optional: the app runs identically with tracing off, and nothing here raises if the
  key is absent. PII-safe by construction — a span's inputs are the SANITIZED projection (headers +
  redaction classes + counts), NEVER the raw prompt, values, or the API key.

* **Product metrics** — a single, deterministic aggregation of what the migration actually did
  (per job and across jobs), read from already-persisted state, PLUS persisted per-call model metrics
  (count, attempts, latency, tokens, errors). This is the data the metrics dashboard renders.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import uuid

from .config import Settings
from .source_intelligence import intelligence_metrics

log = logging.getLogger("darwinbox.observability")


# ============================================================ LangSmith tracing (optional)
def configure_tracing(settings: Settings) -> dict:
    """Propagate LangSmith settings into the environment LangChain/LangGraph read, when enabled.

    Returns a non-secret status dict. Safe to call unconditionally at startup; a no-op (and never an
    error) when tracing is off or no key is present. When disabled it ACTIVELY writes the disable flag
    (not setdefault) so a stale ``true`` inherited from the environment or a prior run cannot leak
    tracing on — important so the offline test suite never reaches the network.
    """
    if not settings.tracing_enabled:
        os.environ["LANGSMITH_TRACING"] = "false"
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        return {"enabled": False, "reason": "tracing disabled or no LangSmith key",
                "project": settings.langsmith_project}
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGCHAIN_TRACING_V2"] = "true"          # legacy flag some versions still read
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_key
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    os.environ["LANGCHAIN_PROJECT"] = settings.langsmith_project
    os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
    return {"enabled": True, "project": settings.langsmith_project,
            "endpoint": settings.langsmith_endpoint}


def tracing_status(settings: Settings) -> dict:
    """Non-secret tracing diagnostics (never returns the key)."""
    return {
        "tracing_flag": settings.langsmith_tracing,
        "key_present": settings.langsmith_key is not None,
        "enabled": settings.tracing_enabled,
        "project": settings.langsmith_project,
        "endpoint": settings.langsmith_endpoint,
    }


# ============================================================ model-call tracer
class ModelTracer:
    """No-op tracer. ``model_call`` records nothing; used whenever tracing is disabled."""

    enabled = False

    def model_call(self, **_kwargs) -> None:
        return None


class LangSmithTracer(ModelTracer):
    """Emits ONE sanitized ``run_type="llm"`` span per model call to LangSmith.

    The span carries only safe metadata (model, kind, ids, header+redaction-class summary, counts) and
    safe outputs (attempts, latency_ms, token counts, status). It NEVER carries the raw prompt, raw
    source values, or the API key. Any failure to emit is swallowed — tracing never breaks a migration.
    """

    enabled = True

    def __init__(self, settings: Settings) -> None:
        from langsmith import Client
        self._client = Client(api_key=settings.langsmith_key, api_url=settings.langsmith_endpoint)
        self._project = settings.langsmith_project

    def model_call(self, *, name: str, inputs: dict, outputs: dict, metadata: dict,
                   tags: list[str], latency_ms: float, error: str | None = None) -> None:
        try:
            end = _dt.datetime.now(_dt.timezone.utc)
            start = end - _dt.timedelta(milliseconds=max(0.0, latency_ms))
            # Nest the model span UNDER the active LangGraph node run when one is on the context (the
            # LangChain tracer sets it during map_columns / analyze_source), so LangSmith shows
            # map_columns -> model.mapping_proposal and analyze_source -> model.transform_proposal
            # instead of unrelated root traces. Falls back to a root run when there is no active run
            # (e.g. a direct call outside the graph, or auto-tracing off for this run).
            parent = None
            try:
                from langsmith.run_helpers import get_current_run_tree
                parent = get_current_run_tree()
            except Exception:  # noqa: BLE001
                parent = None
            if parent is not None:
                try:
                    child = parent.create_child(
                        name=name, run_type="llm", inputs=inputs, outputs=outputs,
                        start_time=start, end_time=end, error=error, tags=tags,
                        extra={"metadata": metadata})
                    child.post()
                    return
                except Exception:  # noqa: BLE001 - fall back to a root run below
                    log.debug("LangSmith child span emit failed; falling back to root", exc_info=True)
            self._client.create_run(
                name=name, run_type="llm", inputs=inputs, outputs=outputs,
                extra={"metadata": metadata}, tags=tags, project_name=self._project,
                start_time=start, end_time=end, error=error, id=uuid.uuid4())
        except Exception:  # noqa: BLE001 - tracing must never affect the migration
            log.debug("LangSmith model_call emit failed (ignored)", exc_info=True)


import contextlib


@contextlib.contextmanager
def migration_trace(scenario: str, job_id: str, *, project_name: str, enabled: bool = True):
    """M3F §H: open ONE top-level LangSmith trace ``migration.<scenario>`` that groups every stage span
    (and the model spans nested under them) for one migration. A no-op when tracing is off or LangSmith
    is unavailable — grouping must never break the demo. Runs the workflow in-process so the run-tree
    contextvar propagates to the graph's node/model spans."""
    if not enabled:
        yield None
        return
    try:
        from langsmith.run_helpers import trace
    except Exception:  # noqa: BLE001
        yield None
        return
    try:
        with trace(name=f"migration.{scenario}", run_type="chain", project_name=project_name,
                   inputs={"scenario": scenario, "job_id": job_id},
                   metadata={"scenario": scenario, "job_id": job_id},
                   tags=[f"scenario:{scenario}", f"job:{job_id}", "migration"]) as rt:
            yield rt
    except Exception:  # noqa: BLE001
        log.debug("migration_trace failed (ignored)", exc_info=True)
        yield None


@contextlib.contextmanager
def stage_span(name: str, *, enabled: bool = True, inputs: dict | None = None, tags: list[str] | None = None):
    """A curated stage span nested under the active migration trace (M3F §H). Model spans emitted inside
    nest under it via ``get_current_run_tree``. No-op when tracing is off/unavailable."""
    if not enabled:
        yield None
        return
    try:
        from langsmith.run_helpers import trace
    except Exception:  # noqa: BLE001
        yield None
        return
    try:
        with trace(name=name, run_type="chain", inputs=inputs or {}, tags=tags or []) as rt:
            yield rt
    except Exception:  # noqa: BLE001
        log.debug("stage_span %s failed (ignored)", name, exc_info=True)
        yield None


def build_tracer(settings: Settings) -> ModelTracer:
    """A LangSmith tracer when tracing is enabled AND the client constructs; otherwise a no-op.
    Never raises and never imports LangSmith when tracing is off."""
    if not settings.tracing_enabled:
        return ModelTracer()
    try:
        return LangSmithTracer(settings)
    except Exception:  # noqa: BLE001
        log.warning("LangSmith tracing requested but the client could not be built; running untraced")
        return ModelTracer()


def record_model_call(db, tracer: ModelTracer, *, job_id: str, kind: str, table_id: str | None,
                      adapter_kind: str, model_id: str, columns_summary: list[dict], meta,
                      status: str, error_category: str | None = None,
                      n_proposals: int | None = None,
                      input_extra: dict | None = None, output_extra: dict | None = None) -> None:
    """Persist one model-call metric row AND (when tracing is on) emit one sanitized LangSmith span.

    ``columns_summary`` is already the SANITIZED per-column projection ([{header, redaction_class}]).
    ``meta`` is a :class:`ProposalCallMeta` (attempts, latency_ms, token counts). ``input_extra`` /
    ``output_extra`` add interview-useful, PII-checked span fields (batch index/total, sanitized
    header→target proposals, a bounded enum value map, rate-limit notes). Nothing raw is used.
    """
    latency_ms = float(getattr(meta, "latency_ms", 0.0) or 0.0)
    prompt_tokens = getattr(meta, "prompt_tokens", None)
    completion_tokens = getattr(meta, "completion_tokens", None)
    total_tokens = getattr(meta, "total_tokens", None)
    attempts = int(getattr(meta, "attempts", 0) or 0)
    notes = list(getattr(meta, "notes", []) or [])
    try:
        db.add_model_call(job_id, kind=kind, table_id=table_id, adapter_kind=adapter_kind,
                          model_id=model_id, attempts=attempts, latency_ms=latency_ms,
                          prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                          total_tokens=total_tokens, status=status, error_category=error_category,
                          n_columns=len(columns_summary), n_proposals=n_proposals)
    except Exception:  # noqa: BLE001 - metrics persistence must never break the migration
        log.debug("persisting model_call metric failed (ignored)", exc_info=True)
    if not tracer.enabled:
        return
    inputs = {"model": model_id, "kind": kind, "n_columns": len(columns_summary),
              "columns": columns_summary}          # headers + redaction classes only — no raw values
    if input_extra:
        inputs.update(input_extra)
    outputs = {"attempts": attempts, "latency_ms": round(latency_ms, 1), "status": status,
               "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
               "total_tokens": total_tokens, "adapter_kind": adapter_kind,
               "n_proposals": n_proposals, "error_category": error_category,
               "rate_limit_notes": [n for n in notes if "backoff" in n or "transient" in n or "server" in n]}
    # Standard LangChain/LangSmith usage shape so an ``llm`` run's token counts are aggregated by
    # LangSmith natively (its token column), not just visible inside the span outputs. Integer counts
    # only — never PII. Present only when the provider actually returned usage (the fake adapter omits).
    if total_tokens is not None:
        outputs["usage_metadata"] = {"input_tokens": prompt_tokens or 0,
                                     "output_tokens": completion_tokens or 0,
                                     "total_tokens": total_tokens}
    if output_extra:
        outputs.update(output_extra)
    tracer.model_call(name=f"model.{kind}", inputs=inputs, outputs=outputs,
                      metadata={"job_id": job_id, "table_id": table_id, "adapter_kind": adapter_kind},
                      tags=[f"job:{job_id}", f"kind:{kind}", f"adapter:{adapter_kind}"],
                      latency_ms=latency_ms,
                      error=(f"{error_category}" if status != "ok" else None))


# ============================================================ product metrics
def _loads(v) -> dict:
    if not v:
        return {}
    try:
        return json.loads(v) if isinstance(v, str) else dict(v)
    except (ValueError, TypeError):
        return {}


def job_metrics(db, job_id: str) -> dict:
    """One structured view of what a single migration did, from persisted state (no recomputation
    of side effects). Answers: automatic vs AI vs human, transformations, records, delivery, time."""
    job = db.get_job(job_id)
    if not job:
        return {}
    summary = _loads(job.get("summary"))
    prep = _loads(job.get("prep_summary"))
    recon = _loads(job.get("recon_summary"))
    routing = summary.get("routing_metrics", {}) if summary else {}
    intel = intelligence_metrics(db, job_id)

    # Delivery: aggregate operations by type + status, and attempts.
    ops = db.get_delivery_operations(job_id)
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for o in ops:
        by_status[o["status"]] = by_status.get(o["status"], 0) + 1
        by_type[o["op_type"]] = by_type.get(o["op_type"], 0) + 1
    attempts = 0
    for o in ops:
        try:
            attempts += len(db.get_delivery_attempts(o["id"]))
        except Exception:
            pass

    # Human-decision counts from the audit stream (category='human').
    audit = db.get_audit(job_id)
    human_decisions = len([a for a in audit if a.get("category") == "human"])

    model = model_call_metrics(db, job_id)

    return {
        "job_id": job_id,
        "tenant_id": db.job_tenant(job_id),
        "status": job.get("status"),
        "stage": job.get("stage"),
        "mapping": {
            "rule": routing.get("columns_resolved_by_rule", 0),
            "model": routing.get("columns_resolved_by_model", 0),
            "human": routing.get("columns_resolved_by_human", 0),
            "unmapped": routing.get("columns_unmapped", 0),
            "ignored": routing.get("columns_ignored", 0),
            "open_custom_field_proposals": routing.get("open_custom_field_proposals", 0),
            "model_proposal_requests": routing.get("model_proposal_requests", 0),
            "model_api_attempts": routing.get("model_api_attempts", 0),
            "unresolved_issues": routing.get("unresolved_issues", 0),
        },
        "intelligence": intel,
        "preparation": prep.get("counts", {}) if prep else {},
        "reconciliation": recon.get("counts", {}) if recon else {},
        "delivery": {"operations": len(ops), "by_status": by_status, "by_type": by_type,
                     "total_attempts": attempts},
        "human_decisions": human_decisions,
        "model": model,
        "timings": stage_timings(db, job_id),
    }


def _parse_ts(s):
    from datetime import datetime
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def stage_timings(db, job_id: str) -> dict:
    """M3F §I: per-stage COMPUTE time for the timing view, from persisted signals only.

    Stage compute (parse/mapping/preparation/reconcile) is the wall time measured AROUND each stage's
    compute call in the worker, so human-review waits (which occur between graph invocations) are
    excluded by construction. LLM mapping / value-mapping are the model-call latency subsets (a portion
    of the mapping / preparation stage). Delivery compute is summed from delivery-attempt timestamps.
    Human review wait is a separate, honest measure: the span from the first review issue opening to the
    last one being resolved, per review family (never labelled 'latency', never mixed into compute)."""
    stages: dict[str, float] = {"parse_stage": 0.0, "mapping": 0.0, "preparation": 0.0,
                                "reconcile": 0.0, "delivery": 0.0}
    for row in db.get_stage_timings(job_id):
        st = row.get("stage")
        if st in stages:
            stages[st] += float(row.get("duration_ms") or 0.0)

    # Delivery compute: sum of per-attempt (completed_at - started_at).
    try:
        for op in db.get_delivery_operations(job_id):
            for att in db.get_delivery_attempts(op["id"]):
                a, b = _parse_ts(att.get("started_at")), _parse_ts(att.get("completed_at"))
                if a and b and b >= a:
                    stages["delivery"] += (b - a).total_seconds() * 1000.0
    except Exception:
        pass

    # LLM subsets (a portion of the mapping / preparation stage compute).
    llm_mapping = llm_transforms = 0.0
    for c in db.get_model_calls(job_id):
        if c.get("status") == "ok":
            if c.get("kind") == "mapping_proposal":
                llm_mapping += float(c.get("latency_ms") or 0.0)
            elif c.get("kind") == "transform_proposal":
                llm_transforms += float(c.get("latency_ms") or 0.0)

    # Human review wait: per family, span from first issue opened to last resolved (honest turnaround).
    human_wait = 0.0
    measured = False
    families = [db.get_issues(job_id), db.get_record_issues(job_id), db.get_target_review_issues(job_id)]
    for issues in families:
        opened = [_parse_ts(i.get("created_at")) for i in issues]
        resolved = [_parse_ts(i.get("updated_at")) for i in issues if i.get("status") == "resolved"]
        opened = [t for t in opened if t]
        resolved = [t for t in resolved if t]
        if opened and resolved:
            span = (max(resolved) - min(opened)).total_seconds() * 1000.0
            if span > 0:
                human_wait += span
                measured = True

    total_compute = round(sum(stages.values()), 1)
    return {
        "stages": {k: round(v, 1) for k, v in stages.items()},
        "llm_mapping_ms": round(llm_mapping, 1),
        "llm_transforms_ms": round(llm_transforms, 1),
        "total_compute_ms": total_compute,
        "human_review_wait_ms": round(human_wait, 1) if measured else None,
        "human_review_wait_measured": measured,
    }


def model_call_metrics(db, job_id: str) -> dict:
    """Aggregate persisted per-call model metrics for one job: call/attempt counts, latency, token
    usage (when the provider reported it), and error counts. Sanitized — no prompt/PII is stored."""
    calls = db.get_model_calls(job_id)
    ok = [c for c in calls if c["status"] == "ok"]
    errors = [c for c in calls if c["status"] != "ok"]
    lat = [c["latency_ms"] for c in ok if c.get("latency_ms") is not None]

    def _sum(field: str) -> int:
        return sum(int(c[field]) for c in calls if c.get(field) is not None)

    errors_by_category: dict[str, int] = {}
    for c in errors:
        cat = c.get("error_category") or "unknown"
        errors_by_category[cat] = errors_by_category.get(cat, 0) + 1
    by_kind: dict[str, int] = {}
    for c in calls:
        by_kind[c["kind"]] = by_kind.get(c["kind"], 0) + 1
    adapters = sorted({c["adapter_kind"] for c in calls})
    model_ids = sorted({c["model_id"] for c in calls if c.get("model_id")})
    # Estimated AI cost — computed from CONFIGURABLE per-deployment pricing (never a hard-coded UI
    # number). Real usage tokens * $/1M. Zero when no provider tokens were reported (e.g. no AI calls
    # at all, or the fake adapter which never reports usage).
    from .config import get_settings
    s = get_settings()
    prompt_tok, completion_tok = _sum("prompt_tokens"), _sum("completion_tokens")
    estimated_cost_usd = round(
        prompt_tok / 1_000_000 * s.llm_price_input_per_1m
        + completion_tok / 1_000_000 * s.llm_price_output_per_1m, 6)
    # Per-call rows for the interview UI (sanitized: kind/model/status/attempts/latency/tokens/counts
    # only — never a prompt, value, or key). Bounded to the most recent 100.
    detail = [{
        "kind": c["kind"], "model_id": c["model_id"], "adapter_kind": c["adapter_kind"],
        "status": c["status"], "error_category": c.get("error_category"),
        "attempts": c["attempts"], "latency_ms": round(float(c["latency_ms"] or 0.0), 1),
        "prompt_tokens": c.get("prompt_tokens"), "completion_tokens": c.get("completion_tokens"),
        "total_tokens": c.get("total_tokens"), "n_columns": c.get("n_columns"),
        "n_proposals": c.get("n_proposals"), "created_at": c.get("created_at"),
    } for c in calls[-100:]]
    return {
        "calls": len(calls),
        "ok": len(ok),
        "errors": len(errors),
        "by_kind": by_kind,
        "adapter_kinds": adapters,
        "model_ids": model_ids,
        "total_attempts": _sum("attempts"),
        "latency_ms_total": round(sum(lat), 1),
        "latency_ms_avg": round(sum(lat) / len(lat), 1) if lat else 0.0,
        "prompt_tokens": prompt_tok,
        "completion_tokens": completion_tok,
        "total_tokens": _sum("total_tokens"),
        "estimated_cost_usd": estimated_cost_usd,
        "price_input_per_1m": s.llm_price_input_per_1m,
        "price_output_per_1m": s.llm_price_output_per_1m,
        "errors_by_category": errors_by_category,
        "calls_detail": detail,
    }


def aggregate_metrics(db, *, limit: int = 500) -> dict:
    """Portfolio-level rollup across recent jobs — the numbers the product dashboard leads with."""
    jobs = db.list_jobs(limit=limit)
    agg = {
        "jobs_total": len(jobs),
        "jobs_by_status": {},
        "mapping": {"rule": 0, "model": 0, "human": 0, "unmapped": 0, "ignored": 0, "proposals": 0},
        "intelligence": {"date_columns_auto_convention": 0, "date_columns_needing_review": 0,
                         "enum_values_auto_normalized": 0, "enum_columns_needing_review": 0,
                         "redundant_representations": 0, "inconsistent_code_display": 0,
                         "references_validated": 0, "references_derived": 0, "references_failed": 0,
                         "transforms_deterministic": 0, "transforms_model": 0, "transforms_human": 0},
        "records": {"eligible": 0, "blocked": 0, "excluded": 0, "candidate_employees": 0},
        "delivery": {"operations": 0, "by_status": {}, "by_type": {}, "total_attempts": 0},
        "human_decisions": 0,
    }
    for j in jobs:
        agg["jobs_by_status"][j["status"]] = agg["jobs_by_status"].get(j["status"], 0) + 1
        m = job_metrics(db, j["id"])
        for k in agg["mapping"]:
            src = "open_custom_field_proposals" if k == "proposals" else k
            agg["mapping"][k] += m.get("mapping", {}).get(src, 0)
        for k in agg["intelligence"]:
            agg["intelligence"][k] += m.get("intelligence", {}).get(k, 0)
        for k in ("eligible", "blocked", "excluded", "candidate_employees"):
            agg["records"][k] += m.get("preparation", {}).get(k, 0)
        d = m.get("delivery", {})
        agg["delivery"]["operations"] += d.get("operations", 0)
        agg["delivery"]["total_attempts"] += d.get("total_attempts", 0)
        for st, n in d.get("by_status", {}).items():
            agg["delivery"]["by_status"][st] = agg["delivery"]["by_status"].get(st, 0) + n
        for ty, n in d.get("by_type", {}).items():
            agg["delivery"]["by_type"][ty] = agg["delivery"]["by_type"].get(ty, 0) + n
        agg["human_decisions"] += m.get("human_decisions", 0)
    return agg
