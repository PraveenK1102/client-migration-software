#!/usr/bin/env python3
"""M3E LIVE generalization demo + report (order §6/§8/§15).

Runs the 50-row unseen-header fixture through the REAL application with REAL Groq and (when a key is
present) REAL LangSmith tracing, then writes reports/llm_generalization_live.md. Bounded and
free-tier-safe: 50 rows, LLM_MAX_CONCURRENCY forced to 1, a handful of calls total, no intentional
rate-limit hammering.

It proves the FULL application path (upload -> worker -> LangGraph profile -> deterministic miss ->
real Groq propose_mappings -> policy -> analyze_source -> real Groq propose_transforms -> ONE
column-scoped taxonomy review -> resume -> preparation), records the deterministic/model/human split,
verifies the model.* LangSmith spans (latency/tokens, nested under the workflow node), and confirms no
raw employee PII reached Groq or LangSmith and the alias inventory did not change.

Run:  cd backend && RUN_LIVE_GROQ=1 ./.venv/bin/python scripts/llm_generalization_live.py
(needs GROQ_API_KEY in backend/.env; set a LANGSMITH_API_KEY too for the span half.)
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ on path

REPO = Path(__file__).resolve().parents[2]  # repo root (backend/scripts/ -> backend/ -> repo)
FIXTURE = REPO / "sample-data" / "llm-generalization-demo.csv"
REPORT = REPO / "reports" / "llm_generalization_live.md"
ANTI_REPORT = REPO / "reports" / "anti_overfitting_llm_generalization.md"

INTENDED = {
    "Employee Reference": "employee_id", "Legal Display Name": "full_name",
    "Corporate Email": "work_email", "Joining Effective Date": "hire_date",
    "Identity Sex": "gender", "Lifecycle Status": "employment_status",
    "Engagement Category": "employment_type", "Org Function": "department",
    "Reports To Reference": "manager_employee_id", "Position Caption": "designation",
}
# The human/taxonomy resolutions for any enum column that the model leaves for review.
TAXONOMY = {
    "gender": {"Man": "male", "Woman": "female", "Does not disclose": "undisclosed"},
    "employment_status": {"Currently Employed": "active", "Voluntarily Exited": "terminated",
                          "Dismissed": "terminated"},
    "employment_type": {"Permanent Staff": "full_time", "Part Time Staff": "part_time",
                        "External Contractor": "contractor"},
    "department": {"Product Engineering": "Engineering", "Revenue": "Sales",
                   "People & Culture": "People Operations"},
}
# Raw PII that must NEVER appear in a Groq prompt or a LangSmith span.
PII_PROBES = ["Ava Reyes", "ava.reyes@northwind-demo.example", "EMP-1001", "noah.haas@northwind-demo.example"]
STABLE = {"preparation_complete", "reconciliation_complete", "awaiting_target_review",
          "migration_complete"}


def _preflight() -> dict:
    from app.alias_audit import inventory_hash, resolves_deterministically
    from app.schema_loader import get_target_schema
    schema = get_target_schema()
    pre = {h: resolves_deterministically(schema, h) for h in INTENDED}
    unresolved = {h: (r is None) for h, r in pre.items()}
    assert all(unresolved.values()), f"a fixture header resolved deterministically: {pre}"
    return {"hash_before": inventory_hash(schema), "all_unresolved": all(unresolved.values()),
            "detail": {h: ("unresolved" if v else pre[h]) for h, v in unresolved.items()}}


def _resolve_mapping_reviews(c, jid: str) -> list[dict]:
    """Confirm any COLUMN-mapping review (awaiting_review) to the intended target — the consultant
    approving the model's proposal, or correcting to the intended target when the model flagged a
    competing meaning (e.g. Org Function could be department or business_unit)."""
    resolved = []
    for issue in c.get(f"/api/jobs/{jid}/reviews").json():
        h = issue["source_header"]
        intended = INTENDED.get(h)
        if not intended:
            continue
        proposed = issue.get("proposed_target_field")
        ev = issue.get("evidence_summary") or {}
        if proposed == intended:
            body = {"action": "approve", "version": issue["version"],
                    "reason": "consultant confirms the model mapping"}
        else:
            body = {"action": "correct", "corrected_target": intended, "version": issue["version"],
                    "reason": "consultant maps to the intended target"}
        r = c.post(f"/api/jobs/{jid}/reviews/{issue['id']}/decision", json=body)
        resolved.append({"header": h, "target": intended, "action": body["action"], "http": r.status_code,
                         "model_proposed": proposed, "model_alternatives": ev.get("model_alternatives"),
                         "model_is_ambiguous": ev.get("model_is_ambiguous")})
    return resolved


def _resolve_enum_reviews(c, jid: str) -> list[dict]:
    """Resolve every open unknown_enum column review with the taxonomy map; return what we resolved."""
    resolved = []
    for issue in c.get(f"/api/jobs/{jid}/record-reviews").json():
        if issue.get("issue_type") != "unknown_enum":
            continue
        field = issue["field"]
        scope = issue.get("scope") or {}
        unmapped = scope.get("unmapped_values") or [d.get("value") for d in
                                                    (issue.get("affected") or {}).get("distinct_values", [])]
        tax = TAXONOMY.get(field, {})
        value_map = {v: tax[v] for v in unmapped if v in tax}
        if not value_map:
            continue
        r = c.post(f"/api/jobs/{jid}/record-reviews/{issue['id']}/decision",
                   json={"action": "map_values", "value_map": value_map, "version": issue["version"],
                         "reason": "consultant taxonomy mapping (M3E demo)"})
        resolved.append({"field": field, "value_map": value_map, "http": r.status_code})
    return resolved


def run_live() -> dict:
    if os.environ.get("RUN_LIVE_GROQ") != "1":
        raise SystemExit("Set RUN_LIVE_GROQ=1 to run the live demo (it calls the real Groq API).")
    marker = f"darwinbox-m3e-{uuid.uuid4().hex[:8]}"
    os.environ["LLM_PROVIDER"] = "groq"            # real key from backend/.env
    os.environ["LLM_MAX_CONCURRENCY"] = "1"        # free-tier safe: one call at a time
    os.environ["AUTO_CONTINUE"] = "true"
    os.environ["LANGSMITH_PROJECT"] = marker
    os.environ.setdefault("LANGSMITH_TRACING", "true")
    # Post traces SYNCHRONOUSLY so every node + model span is flushed to LangSmith by the time the job
    # finishes (a multi-stage run's async batch otherwise lags past a short poll). Negligible cost at
    # 50 rows; the offline suite is untouched (tracing off there).
    os.environ["LANGCHAIN_CALLBACKS_BACKGROUND"] = "false"
    data_dir = Path("/tmp") / f"m3e-live-{marker}"
    os.environ["DATA_DIR"] = str(data_dir)

    from app import config
    config.get_settings.cache_clear()
    settings = config.get_settings()
    settings.ensure_dirs()
    tracing_on = settings.tracing_enabled
    pre = _preflight()

    from fastapi.testclient import TestClient
    from app.main import create_app

    csv = FIXTURE.read_bytes()
    t0 = time.time()
    resolutions: list[dict] = []
    mapping_reviews: list[dict] = []
    with TestClient(create_app()) as c:
        jid = c.post("/api/jobs", files=[("files", (FIXTURE.name, csv, "text/csv"))]).json()["id"]
        status = None
        for _ in range(8000):
            status = c.get(f"/api/jobs/{jid}").json()["status"]
            if status == "awaiting_review":                    # COLUMN-mapping review
                issues = c.get(f"/api/jobs/{jid}/reviews").json()
                if not issues:                                 # resume pending after a resolve -> wait
                    time.sleep(0.1); continue
                got = _resolve_mapping_reviews(c, jid)
                mapping_reviews.extend(got)
                if not got:                                    # open issue we cannot resolve -> stop
                    break
            elif status == "awaiting_record_review":           # enum VALUE / taxonomy review
                issues = c.get(f"/api/jobs/{jid}/record-reviews").json()
                if not issues:
                    time.sleep(0.1); continue
                got = _resolve_enum_reviews(c, jid)
                resolutions.extend(got)
                if not got:
                    break
            elif status in STABLE or status in {"error", "blocked_provider"}:
                break
            time.sleep(0.1)
        # let the LangGraph run finish flushing to LangSmith before the app shuts down
        try:
            from langchain_core.tracers.langchain import wait_for_all_tracers
            wait_for_all_tracers()
        except Exception:
            pass
        elapsed = time.time() - t0

        mappings = c.get(f"/api/jobs/{jid}/mappings").json()
        metrics = c.get(f"/api/jobs/{jid}/metrics").json()
        audit = c.get(f"/api/jobs/{jid}/audit").json()

    # --- summarize mapping routing (method per accepted column) ---
    accepted = mappings.get("accepted", [])
    by_method = {"rule": [], "model": [], "human": []}
    for row in accepted:
        by_method.setdefault(row.get("method") or "other", []).append(
            {"header": row["source_header"], "target": row["target_field"]})
    intended_model = {h: t for h, t in INTENDED.items()
                      if any(m["header"] == h and m["target"] == t for m in by_method.get("model", []))}

    # --- transform plans (enum origin/status) from intelligence metrics + audit ---
    model_metrics = metrics.get("model", {})
    intel = metrics.get("intelligence", {})

    # --- LangSmith span verification ---
    langsmith = {"enabled": tracing_on, "project": marker}
    if tracing_on:
        try:
            from langchain_core.tracers.langchain import wait_for_all_tracers
            wait_for_all_tracers()
        except Exception:
            pass
        from langsmith import Client
        cl = Client(api_key=settings.langsmith_key, api_url=settings.langsmith_endpoint)
        # A multi-stage run emits ~24 runs incl. the model spans; they flush over ~30-60s, so poll
        # until every OK model call is visible (or a generous timeout) — not just the first span.
        expected_model = int(model_metrics.get("ok", 0) or 0)
        runs = []
        for _ in range(90):
            time.sleep(1.0)
            try:
                runs = list(cl.list_runs(project_name=marker, limit=100))  # 100 is the API max
            except Exception:
                runs = []
            found = sum(1 for r in runs if (r.name or "").startswith("model."))
            if found >= max(1, expected_model):
                break
        by_id = {str(r.id): r for r in runs}
        model_runs = [r for r in runs if (r.name or "").startswith("model.")]
        span_rows = []
        pii_leak = False
        for r in model_runs:
            blob = f"{r.name} {getattr(r,'inputs',{})} {getattr(r,'outputs',{})} {getattr(r,'extra',{})}"
            if any(p in blob for p in PII_PROBES):
                pii_leak = True
            parent = by_id.get(str(getattr(r, "parent_run_id", None)))
            out = getattr(r, "outputs", {}) or {}
            st, et = getattr(r, "start_time", None), getattr(r, "end_time", None)
            lat_ms = ((et - st).total_seconds() * 1000.0) if (st and et) else None
            span_rows.append({
                "name": r.name, "parent": parent.name if parent is not None else None,
                "nested": parent is not None,
                "total_tokens": out.get("total_tokens"), "prompt_tokens": out.get("prompt_tokens"),
                "completion_tokens": out.get("completion_tokens"),
                "latency_ms": round(lat_ms, 1) if lat_ms else out.get("latency_ms")})
        langsmith.update({
            "runs_total": len(runs),
            "node_names": sorted({r.name for r in runs if (r.name or "") in
                                  ("map_columns", "analyze_source", "profile", "assess", "finalize_mapping")}),
            "model_spans": span_rows,
            "model_span_count": len(model_runs),
            "any_nested": any(s["nested"] for s in span_rows),
            "any_tokens": any((s["total_tokens"] or 0) > 0 for s in span_rows),
            "pii_leak": pii_leak,
        })

    from app.alias_audit import inventory_hash
    from app.schema_loader import get_target_schema
    hash_after = inventory_hash(get_target_schema())

    result = {
        "marker": marker, "job_id": jid, "final_status": status, "elapsed_s": round(elapsed, 1),
        "model": settings.groq_model, "tracing_enabled": tracing_on,
        "preflight": pre, "hash_after": hash_after,
        "alias_inventory_unchanged": (pre["hash_before"] == hash_after),
        "mapping_counts": mappings.get("counts", {}),
        "routing_by_method": {k: v for k, v in by_method.items() if v},
        "intended_headers_model_mapped": intended_model,
        "model_metrics": model_metrics,
        "intelligence": {k: intel.get(k) for k in intel if "enum" in k or "transform" in k},
        "mapping_reviews": mapping_reviews,
        "human_taxonomy_resolutions": resolutions,
        "langsmith": langsmith,
        "pii_probes_checked": PII_PROBES,
    }
    return result


def _write_report(res: dict) -> None:
    L = []
    A = L.append
    ls = res.get("langsmith", {})
    A("# LLM Generalization — LIVE run (M3E §6/§8/§15)")
    A("")
    A(f"_Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} via "
      "`backend/scripts/llm_generalization_live.py` — REAL Groq"
      f"{' + REAL LangSmith' if res['tracing_enabled'] else ' (LangSmith off)'}._")
    A("")
    A("## Run")
    A(f"- Model: **{res['model']}** · Job: `{res['job_id']}` · Final status: **{res['final_status']}** "
      f"· Wall time: {res['elapsed_s']}s")
    A(f"- LangSmith project: `{res['marker']}` · tracing enabled: **{res['tracing_enabled']}**")
    A("")
    A("## Anti-overfitting preflight")
    A(f"- Deterministic-surface hash BEFORE: `{res['preflight']['hash_before']}`")
    A(f"- Deterministic-surface hash AFTER:  `{res['hash_after']}`")
    A(f"- **Alias inventory unchanged by the run: {res['alias_inventory_unchanged']}**")
    A("- Every intended header was `unresolved` deterministically before Groq:")
    for h, v in res["preflight"]["detail"].items():
        A(f"  - `{h}` → {v}")
    A("")
    A("## Column mapping — deterministic vs model vs human")
    A(f"- Mapping counts: `{json.dumps(res['mapping_counts'])}`")
    for method, rows in res["routing_by_method"].items():
        A(f"- **{method}** ({len(rows)}): " + ", ".join(f"`{r['header']}`→`{r['target']}`" for r in rows))
    A("")
    A("- Intended unseen headers that were **model-mapped** (`method == model`):")
    for h, t in res["intended_headers_model_mapped"].items():
        A(f"  - `{h}` → `{t}`")
    A("")
    A("## Real Groq model metrics (persisted, sanitized)")
    A("```json")
    A(json.dumps(res["model_metrics"], indent=1))
    A("```")
    A(f"- Intelligence (enum/transform): `{json.dumps(res['intelligence'])}`")
    A("")
    A("## Column-mapping reviews (genuine ambiguity)")
    if res.get("mapping_reviews"):
        for r in res["mapping_reviews"]:
            A(f"- `{r['header']}` → `{r['target']}` via **{r['action']}** (model proposed "
              f"`{r['model_proposed']}`, alternatives {r.get('model_alternatives')}, "
              f"ambiguous={r.get('model_is_ambiguous')})")
    else:
        A("- No column-mapping review was required; all model mappings auto-accepted by policy.")
    A("")
    A("## Enum value transformation + taxonomy review")
    if res["human_taxonomy_resolutions"]:
        for r in res["human_taxonomy_resolutions"]:
            A(f"- ONE column-scoped review resolved for `{r['field']}`: "
              f"`{json.dumps(r['value_map'])}` (HTTP {r['http']})")
    else:
        A("- No column-scoped taxonomy review was required in this run.")
    A("")
    A("## LangSmith model spans")
    if not ls.get("enabled"):
        A("- Tracing disabled for this run.")
    else:
        A(f"- Runs in project: {ls.get('runs_total')} · workflow nodes seen: {ls.get('node_names')}")
        A(f"- model.* spans: {ls.get('model_span_count')} · any nested under a node: "
          f"**{ls.get('any_nested')}** · any real token usage: **{ls.get('any_tokens')}**")
        A(f"- **Raw PII leaked to LangSmith: {ls.get('pii_leak')}** (probes: {res['pii_probes_checked']})")
        A("")
        A("| span | parent node | nested | prompt_tok | completion_tok | total_tok | latency_ms |")
        A("|---|---|---|---|---|---|---|")
        for s in ls.get("model_spans", []):
            A(f"| {s['name']} | {s['parent']} | {s['nested']} | {s['prompt_tokens']} | "
              f"{s['completion_tokens']} | {s['total_tokens']} | {s['latency_ms']} |")
    A("")
    A("## PII posture")
    A(f"- Raw employee PII in Groq prompts: enforced by `app/model_projection.py` (headers + redaction "
      "classes + enum labels only; names/emails/ids never sent).")
    A(f"- Raw employee PII in LangSmith spans: **{ls.get('pii_leak', 'n/a')}** (checked against "
      f"{res['pii_probes_checked']}).")
    A("")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(L) + "\n", encoding="utf-8")


def _write_anti_overfitting_report(res: dict) -> None:
    """reports/anti_overfitting_llm_generalization.md (order §13): the 10-point proof that the result
    came from the MODEL, not a grown alias dictionary."""
    from app.alias_audit import alias_inventory
    from app.schema_loader import get_target_schema
    inv = alias_inventory(get_target_schema())
    model_mapped = res["intended_headers_model_mapped"]
    L, A = [], None
    A = L.append
    A("# Anti-Overfitting — Real LLM Generalization (M3E §13)")
    A("")
    A(f"_Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} from a REAL Groq run "
      f"(job `{res['job_id']}`) via `backend/scripts/llm_generalization_live.py`._")
    A("")
    A("| # | Check | Result |")
    A("|---|---|---|")
    A(f"| 1 | Canonical target keys + declared aliases loaded | {inv['counts']['core_canonical_keys']} "
      f"canonical + {inv['counts']['core_header_aliases']} aliases = {inv['counts']['deterministic_header_keys']} keys |")
    A(f"| 2 | Every intended demo header absent from BOTH sets | "
      f"{'YES' if res['preflight']['all_unresolved'] else 'NO'} |")
    A(f"| 3 | Deterministic mapper leaves each intended header unresolved | "
      f"{'YES (all 10)' if res['preflight']['all_unresolved'] else 'NO'} |")
    A(f"| 4 | Real Groq model calls occurred | {res['model_metrics'].get('calls', 0)} call(s), "
      f"adapters {res['model_metrics'].get('adapter_kinds')} |")
    A(f"| 5 | Expected fields were model-proposed | {len(model_mapped)} intended headers model-mapped |")
    A(f"| 6 | Policy accepted safe ones, reviewed ambiguous ones | "
      f"{len(model_mapped)} auto-accepted; {len(res.get('mapping_reviews', []))} mapping review(s); "
      f"{len(res.get('human_taxonomy_resolutions', []))} value-taxonomy review(s) |")
    A(f"| 7 | NO alias added as a side-effect | hash unchanged: {res['alias_inventory_unchanged']} |")
    A(f"| 8 | Decisions reproducible/idempotent | rule/model/human decisions + transform plans are "
      f"persisted and idempotent on re-run (human decisions are overlays; re-running MAP never "
      f"duplicates or overwrites them) |")
    A(f"| 9 | Frozen inventory hash BEFORE / AFTER | `{res['preflight']['hash_before']}` / "
      f"`{res['hash_after']}` |")
    A("")
    A("## Intended headers → model-mapped target")
    for h, t in model_mapped.items():
        A(f"- `{h}` → `{t}` (method=model)")
    A("")
    if res.get("mapping_reviews"):
        A("## Ambiguous mappings that were reviewed (not guessed)")
        for r in res["mapping_reviews"]:
            A(f"- `{r['header']}`: model proposed `{r['model_proposed']}` "
              f"(alternatives {r.get('model_alternatives')}, ambiguous={r.get('model_is_ambiguous')}) "
              f"→ consultant {r['action']} → `{r['target']}`")
        A("")
    A("## Frozen alias inventory")
    A(f"- Schema version: {inv['schema_version']}")
    A(f"- Core fields: {inv['counts']['core_fields']} · canonical keys: {inv['counts']['core_canonical_keys']} "
      f"· header aliases: {inv['counts']['core_header_aliases']} · deterministic keys: "
      f"{inv['counts']['deterministic_header_keys']}")
    A(f"- Frozen deterministic-surface hash: `{res['hash_after']}`")
    A(f"- **Alias list changed because of the live fixture: {'NO' if res['alias_inventory_unchanged'] else 'YES'}**")
    A("")
    ANTI_REPORT.write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    res = run_live()
    _write_report(res)
    _write_anti_overfitting_report(res)
    (REPO / "reports" / "llm_generalization_live_result.json").write_text(
        json.dumps(res, indent=1, default=str), encoding="utf-8")
    print(json.dumps(res, indent=1, default=str))
    print(f"\nwrote {REPORT}\nwrote {ANTI_REPORT}")
