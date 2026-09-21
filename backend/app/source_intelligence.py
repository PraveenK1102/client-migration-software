"""Adaptive source intelligence orchestrator (M3C).

Turns full-column evidence into a persisted, declarative transformation plan, following one
discipline everywhere:

    PROPOSE (model, only where semantics help)  ->  PROVE (deterministic evidence + schema)  ->
    APPLY when defensible  OR  ESCALATE one scoped review when the file itself is ambiguous.

Two entry points, matching the mapping graph:

* :func:`analyze_relationships_for_table` runs INSIDE column mapping (after the deterministic rule
  pass, before any model call). It discovers code/display relationships and disposes a proven 1:1
  code column as REDUNDANT_REPRESENTATION so it is neither sent to the model nor proposed as a custom
  field (raw stays in provenance). It returns the redundant profile ids so mapping can exclude them.

* :func:`analyze_source` runs after mappings settle. It infers whole-column date conventions
  (resolving two-digit-year centuries from schema temporal constraints), builds column-level
  enum/boolean value maps (declared aliases first, then a validated model proposal), proves
  referential integrity for ID columns, and derives a manager employee id from an exact UNIQUE name
  match. It persists transformation plans; metrics are computed later from the persisted plans.

Deterministic except the bounded, re-validated value-map model call. Nothing here writes to the
target. Everything is persisted (transformation_plans, column_relationships) and audited.
"""
from __future__ import annotations

import json
from datetime import date

from .date_inference import infer_date_format, resolve_column_century
from .enum_inference import _fold, build_enum_map, validate_model_value_map
from .llm.base import ModelError, TransformProposalRequest
from .policy import POLICY_VERSION
from .profiling import profile_from_row
from .reference_integrity import derive_reference_by_name, normalize_name, validate_reference_domain
from .relationships import detect_code_display_pairs
from .schema_loader import TargetSchema
from .transform_plan import (
    AUTO_ACCEPTED,
    NEEDS_REVIEW,
    TransformPlan,
    boolean_map_plan,
    date_parse_plan,
    derive_reference_plan,
    enum_map_plan,
    plan_id,
    redundant_plan,
)

INTELLIGENCE_VERSION = "intel.v1"
_ACCEPTED = ("auto_accepted", "approved", "corrected")


def _value_columns(db, table_id: str) -> dict[int, list]:
    cols: dict[int, list] = {}
    for r in db.get_rows_for_table(table_id):
        cells = json.loads(r["cells"])
        for ci, c in enumerate(cells):
            cols.setdefault(ci, []).append(c.get("value"))
    return cols


def _accepted_targets(db, job_id: str, table_id: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for d in db.get_decisions(job_id):
        if d["table_id"] == table_id and d["target_field"] and d["status"] in _ACCEPTED:
            out[d["profile_id"]] = d["target_field"]
    return out


# ============================================================================================
# Phase A — structural relationships (runs inside mapping, before any model call)
# ============================================================================================
def analyze_relationships_for_table(db, job_id: str, table_id: str, profiles: list, schema: TargetSchema,
                                    *, value_columns: dict | None = None) -> set[str]:
    """Discover code/display pairs for one table; dispose proven-redundant code columns.

    Returns the set of profile ids now disposed as redundant (mapping excludes them from the model
    call and from custom-field proposals). ``profiles`` are ColumnProfile objects for the table.
    """
    value_columns = value_columns if value_columns is not None else _value_columns(db, table_id)
    accepted_target = _accepted_targets(db, job_id, table_id)
    redundant: set[str] = set()
    for pe in detect_code_display_pairs(value_columns, profiles):
        db.add_column_relationship(job_id, table_id=table_id, relationship=pe.relationship,
                                   code_profile_id=pe.code_profile_id,
                                   label_profile_id=pe.label_profile_id, evidence=pe.to_evidence())
        if pe.relationship != "one_to_one":
            continue
        label_target = accepted_target.get(pe.label_profile_id)
        code_target = accepted_target.get(pe.code_profile_id)
        if not label_target or code_target:
            continue  # redundancy applies only when the LABEL is mapped and the CODE is unmapped
        # Auto-retiring a column is a destructive decision, so it demands a genuine source-system
        # code (GenderID/MaritalStatusID-style: numeric or id-shaped), not merely the higher-scoring
        # of two textual attributes that happen to line up 1:1 on a small sample. Anything short of
        # that is kept and surfaced (it stays unresolved -> proposal/review), never silently dropped.
        if not pe.code_is_coded:
            continue
        if pe.matched_rows <= pe.distinct_code:
            continue  # the 1:1 map must be confirmed by repetition, not a trivial all-distinct bijection
        code_prof = next((p for p in profiles if p.profile_id == pe.code_profile_id), None)
        explanation = (f"{pe.code_header} was not migrated separately because it is a 1:1 source-system "
                       f"code for {pe.label_header}, which is mapped to '{label_target}'. Raw "
                       f"{pe.code_header} remains available in source provenance.")
        plan = redundant_plan(job_id, table_id, pe.code_profile_id, pe.code_header,
                              redundant_with=pe.label_profile_id,
                              evidence={**pe.to_evidence(), "label_target": label_target,
                                        "explanation": explanation},
                              affected_rows=(code_prof.non_empty_count if code_prof else 0))
        db.upsert_transformation_plan(plan)
        db.upsert_decision(job_id, profile_id=pe.code_profile_id, table_id=table_id,
                           source_header=pe.code_header, target_field=None, status="redundant",
                           actor="system", method="relationship", destination_kind="REDUNDANT",
                           reason=(f"Redundant coded representation of '{pe.label_header}' (perfect 1:1). "
                                   f"Raw value retained in source provenance."))
        db.add_audit(job_id, event_type="redundant_representation", actor="system",
                     source_ref={"profile_id": pe.code_profile_id, "header": pe.code_header,
                                 "table_id": table_id},
                     after=plan.evidence, reason=explanation, schema_version=schema.version,
                     policy_version=POLICY_VERSION)
        redundant.add(pe.code_profile_id)
    return redundant


# ============================================================================================
# Phase B — transform plans (runs after mappings settle)
# ============================================================================================
async def analyze_source(db, job_id: str, schema: TargetSchema, adapter, model_id: str, *,
                         reference_date: date | None = None, tracer=None,
                         enum_batch_size: int | None = None, enum_max_distinct: int | None = None) -> None:
    from .observability import ModelTracer
    tracer = tracer or ModelTracer()
    reference_date = reference_date or date.today()
    if enum_batch_size is None or enum_max_distinct is None:
        from .config import get_settings
        s = get_settings()
        enum_batch_size = enum_batch_size or s.llm_enum_batch_size
        enum_max_distinct = enum_max_distinct or s.llm_enum_max_distinct
    profiles_rows = db.get_profiles(job_id)
    profiles_by_table: dict[str, list] = {}
    profile_by_id: dict[str, dict] = {}
    for p in profiles_rows:
        profiles_by_table.setdefault(p["table_id"], []).append(p)
        profile_by_id[p["id"]] = p
    decisions = {d["profile_id"]: d for d in db.get_decisions(job_id)}
    employee_id_domain, name_to_ids = _employee_domains(db, job_id, schema, decisions, profile_by_id)
    kept: set[str] = set(_human_plan_ids(db, job_id))
    # REDUNDANT plans are produced by the relationship phase inside map_columns (which runs
    # immediately before this node) and are only valid while the column is still disposed redundant.
    # Keep the current ones so this node's cleanup does not delete plans it does not manage; drop any
    # whose decision is no longer 'redundant' (e.g. a human later re-mapped the label).
    redundant_disposed = {d["profile_id"] for d in decisions.values() if d["status"] == "redundant"}
    kept |= {r["id"] for r in db.get_transformation_plans(job_id)
             if r["kind"] == "redundant" and r["profile_id"] in redundant_disposed}

    for table_id, prows in profiles_by_table.items():
        profiles = [profile_from_row(p) for p in sorted(prows, key=lambda x: x["col_index"])]
        value_columns = _value_columns(db, table_id)
        accepted_target = _accepted_targets(db, job_id, table_id)

        for p in profiles:
            target = accepted_target.get(p.profile_id)
            if not target:
                continue
            tf = schema.get(target)
            if tf is None:
                continue
            if tf.value_type == "date":
                _date_plan(db, job_id, table_id, p, target, tf, value_columns, schema, reference_date, kept)
            elif tf.value_type == "boolean":
                _boolean_plan(db, job_id, table_id, p, target, tf, kept)
            elif tf.value_type in ("enum", "multiselect"):
                await _enum_plan(db, job_id, table_id, p, target, tf, adapter, model_id, kept, tracer,
                                 full_values=value_columns.get(p.col_index, []),
                                 batch_size=enum_batch_size, max_distinct=enum_max_distinct)
            elif target == "manager_employee_id":
                _reference_domain(db, job_id, table_id, p, target, value_columns, employee_id_domain,
                                  schema, kept)

        _manager_name_derivation(db, job_id, table_id, profiles, value_columns, accepted_target,
                                 name_to_ids, schema, kept)

    db.delete_transformation_plans_not_in(job_id, kept)
    db.add_audit(job_id, event_type="source_analyzed", actor="system",
                 after={"version": INTELLIGENCE_VERSION,
                        "plans": len(db.get_transformation_plans(job_id))},
                 schema_version=schema.version)


def _human_plan_ids(db, job_id: str) -> set[str]:
    return {r["id"] for r in db.get_transformation_plans(job_id) if r["status"] in ("approved", "rejected")}


def _employee_domains(db, job_id, schema, decisions, profile_by_id):
    """(set of source employee_id business keys, {normalized full_name -> {employee_ids}})."""
    from .prepare import normalize_field
    id_cols: list[tuple[str, int]] = []
    name_cols: dict[str, int] = {}
    for d in decisions.values():
        if d["status"] not in _ACCEPTED or not d["target_field"]:
            continue
        prof = profile_by_id.get(d["profile_id"])
        if not prof:
            continue
        if d["target_field"] == "employee_id":
            id_cols.append((prof["table_id"], prof["col_index"]))
        elif d["target_field"] == "full_name":
            name_cols[prof["table_id"]] = prof["col_index"]
    domain: set[str] = set()
    name_to_ids: dict[str, set[str]] = {}
    for table_id, ci in id_cols:
        name_ci = name_cols.get(table_id)
        for r in db.get_rows_for_table(table_id):
            cells = json.loads(r["cells"])
            raw_id = cells[ci]["value"] if ci < len(cells) else None
            fv = normalize_field("employee_id", raw_id, schema)
            key = fv.value if fv.status == "resolved" and fv.value else None
            if not key:
                continue
            domain.add(key)
            if name_ci is not None and name_ci < len(cells):
                nm = cells[name_ci]["value"]
                if nm and str(nm).strip():
                    name_to_ids.setdefault(normalize_name(nm), set()).add(key)
    return domain, name_to_ids


def _date_plan(db, job_id, table_id, p, target, tf, value_columns, schema, reference_date, kept):
    if _skip_if_auto(db, job_id, p.profile_id, target):
        kept.add(plan_id(job_id, p.profile_id, target))
        return
    values = value_columns.get(p.col_index, [])
    inf = infer_date_format(values)
    constraints = schema.temporal_constraints.get(target, {})
    century = (resolve_column_century(values, order=inf.order, constraints=constraints,
                                      reference_date=reference_date) if inf.two_digit_year else None)
    evidence = {"date_inference": inf.to_evidence(), "temporal_constraints": constraints,
                "century": century.to_evidence() if century else {"status": "not_needed"}}
    if inf.status in ("inferred", "single_format") and not (century and century.status == "needs_review"):
        plan = date_parse_plan(job_id, table_id, p.profile_id, p.header, target, order=inf.order,
                               status=AUTO_ACCEPTED, evidence=evidence, affected_rows=p.non_empty_count)
    elif inf.status in ("inferred", "single_format"):  # order known, century needs a pivot
        plan = date_parse_plan(job_id, table_id, p.profile_id, p.header, target, order=inf.order,
                               status=NEEDS_REVIEW, evidence=evidence, affected_rows=p.non_empty_count,
                               review_prompt=(f"Confirm the century for two-digit years in '{p.header}': "
                                              f"{century.ambiguous_values} value(s) have more than one plausible century."),
                               review_options=[{"action": "confirm_century_pivot"}])
    elif inf.status == "ambiguous_needs_review":
        plan = date_parse_plan(job_id, table_id, p.profile_id, p.header, target, order=None,
                               status=NEEDS_REVIEW, evidence=evidence, affected_rows=p.non_empty_count,
                               review_prompt=f"Confirm the date convention for '{p.header}' (MDY or DMY).",
                               review_options=[{"action": "confirm_convention", "choices": ["MDY", "DMY"]}])
    elif inf.status == "mixed_format":
        plan = date_parse_plan(job_id, table_id, p.profile_id, p.header, target, order=None,
                               status=NEEDS_REVIEW, evidence=evidence, affected_rows=p.non_empty_count,
                               review_prompt=(f"'{p.header}' has decisive evidence for BOTH MDY and DMY "
                                              f"(mixed formats)."),
                               review_options=[{"action": "confirm_convention", "choices": ["MDY", "DMY"]}])
    else:  # not_date: ISO / month-name / empty -> the strict parser handles it order-independently
        plan = date_parse_plan(job_id, table_id, p.profile_id, p.header, target, order=None,
                               status=AUTO_ACCEPTED, evidence=evidence, affected_rows=p.non_empty_count)
    db.upsert_transformation_plan(plan)
    kept.add(plan.id)


def _boolean_plan(db, job_id, table_id, p, target, tf, kept):
    if _skip_if_auto(db, job_id, p.profile_id, target):
        kept.add(plan_id(job_id, p.profile_id, target))
        return
    domain = [d["value"] for d in p.value_domain] or list(p.samples)
    m = build_enum_map(tf, domain)
    status = AUTO_ACCEPTED if m.status == "complete" else NEEDS_REVIEW
    plan = boolean_map_plan(job_id, table_id, p.profile_id, p.header, target, value_map=m.value_map,
                            status=status, evidence=m.to_evidence(), affected_rows=p.non_empty_count)
    db.upsert_transformation_plan(plan)
    kept.add(plan.id)


def _full_distinct_domain(full_values, fallback_domain) -> tuple[list[str], dict[str, int]]:
    """The COMPLETE distinct non-empty domain of the whole column (not the ~50-capped profile
    value_domain), ordered deterministically by (-frequency, value) with per-value counts. Falls back
    to the profiled domain/samples only when the raw column values were not supplied."""
    counts: dict[str, int] = {}
    source = full_values if full_values else (fallback_domain or [])
    for v in source:
        if v is None:
            continue
        sv = str(v)
        if sv.strip() == "":
            continue
        counts[sv] = counts.get(sv, 0) + 1
    ordered = sorted(counts, key=lambda k: (-counts[k], k))
    return ordered, counts


async def _enum_plan(db, job_id, table_id, p, target, tf, adapter, model_id, kept, tracer=None, *,
                     full_values=None, batch_size: int = 25, max_distinct: int = 2000):
    """Column-level enum normalization decoupled from EMPLOYEE ROW COUNT (M3E §9/§10, M3I autonomy).

    Inspect the FULL distinct source domain -> apply deterministic Tier-1 aliases -> collect only the
    still-unresolved DISTINCT values (folded to dedupe case/whitespace) -> send them to the model in
    bounded SEQUENTIAL batches of ``batch_size`` -> validate every proposal against the fixed target
    enum. **M3I:** when the first proposal merely DECLINED some values (returned null WITHOUT flagging
    ambiguity or a business-taxonomy relation) and there was no model/config error, make AT MOST ONE
    bounded semantic-clarification pass over ONLY those declined values, re-validate it the SAME way,
    and merge only validated non-taxonomy mappings. A model-declared ambiguity or taxonomy translation
    is NEVER revisited or overridden by that pass. Persist ONE reusable plan applied to all rows in
    preparation. Model-call count scales with unresolved distinct batches, never with row count. Every
    distinct value ends deterministically mapped, model-mapped+validated, or surfaced for ONE
    column-scoped human review (never silently lost).
    """
    from .observability import ModelTracer, record_model_call
    tracer = tracer or ModelTracer()
    if _skip_if_auto(db, job_id, p.profile_id, target):
        kept.add(plan_id(job_id, p.profile_id, target))
        return
    fallback = [d["value"] for d in p.value_domain] or sorted({s for s in p.samples})
    domain, counts = _full_distinct_domain(full_values, fallback)

    det = build_enum_map(tf, domain)
    value_map = dict(det.value_map)
    origin = "deterministic"
    taxonomy = False

    # Fold-dedupe the still-unresolved DISTINCT values so case/whitespace variants cost one slot; a
    # model decision for the representative applies to every raw variant in its fold group.
    fold_groups: dict[str, list[str]] = {}
    for v in det.unmapped:
        fold_groups.setdefault(_fold(v), []).append(v)
    fold_keys = sorted(fold_groups, key=lambda k: (-sum(counts.get(x, 0) for x in fold_groups[k]), k))
    rep_of = {k: sorted(fold_groups[k], key=lambda x: (-counts.get(x, 0), x))[0] for k in fold_keys}

    # Bounded distinct-domain budget: overflow is surfaced for review, NEVER silently truncated.
    budget_keys = fold_keys[:max_distinct]
    over_budget_keys = fold_keys[max_distinct:]

    stats = {"n_batches": 0, "n_model_accepted": 0, "clarify_batches": 0, "model_error": None}
    # Sticky per-value dispositions (fold keys). Values the model judged genuinely ambiguous or a
    # business-taxonomy translation are NEVER sent to the clarification pass and NEVER auto-applied.
    ambiguous_folds: set[str] = set()
    taxonomy_folds: set[str] = set()
    col_summary = [{"header": p.header, "redaction_class": "enum"}]

    async def _send(reps: list[str], *, clarify: bool, already_map: dict[str, str]) -> None:
        nonlocal origin, taxonomy
        batches = [reps[i:i + batch_size] for i in range(0, len(reps), batch_size)]
        total = len(batches)
        for bi, batch in enumerate(batches, start=1):
            # PII posture: these are the low-cardinality UNMAPPED labels of a column already mapped to a
            # target enum — a business taxonomy detached from any employee identity, which the order
            # permits for semantic transformation. It is not high-cardinality PII.
            in_extra = {"target_field": target, "batch": bi, "batches_total": total,
                        "n_distinct_values": len(batch), "batch_size": batch_size, "clarify": clarify}
            try:
                req = TransformProposalRequest(
                    profile_id=p.profile_id, source_header=p.header, target_field=target,
                    target_label=tf.label, target_description=tf.description,
                    allowed_values=list(tf.enum_values or ()), source_values=list(batch),
                    already_mapped=dict(already_map), table_ref={"table_id": table_id}, clarify=clarify)
                resp, _meta = await adapter.propose_transforms(request=req)
                stats["n_batches"] += 1
                if clarify:
                    stats["clarify_batches"] += 1
                proposals = [{"source_value": it.source_value, "target_value": it.target_value,
                              "ambiguous": it.ambiguous} for it in resp.mappings]
                validated = validate_model_value_map(tf, proposals, batch, already={})
                relation_by_src = {it.source_value: it.relation for it in resp.mappings}
                # A model-declared ambiguity is sticky: it can never be overridden by a later pass.
                for it in resp.mappings:
                    if it.ambiguous:
                        ambiguous_folds.add(_fold(it.source_value))
                batch_accepted: dict[str, str] = {}
                for src, tgt in validated.accepted.items():
                    f = _fold(src)
                    if f in ambiguous_folds:
                        continue                 # never override a model-declared ambiguity
                    if relation_by_src.get(src) == "taxonomy_translation":
                        taxonomy = True          # a company-specific taxonomy translation needs a human
                        taxonomy_folds.add(f)
                        continue
                    for raw in fold_groups.get(f, [src]):
                        if raw not in value_map:
                            value_map[raw] = tgt
                    batch_accepted[src] = tgt
                    stats["n_model_accepted"] += 1
                    origin = "model"
                record_model_call(
                    db, tracer, job_id=job_id, kind="transform_proposal", table_id=table_id,
                    adapter_kind=_meta.adapter_kind, model_id=_meta.model_id,
                    columns_summary=col_summary, meta=_meta, status="ok", n_proposals=len(resp.mappings),
                    input_extra=in_extra,
                    output_extra={"value_map": dict(list(batch_accepted.items())[:25]),
                                  "accepted": len(batch_accepted), "rejected": len(validated.rejected),
                                  "clarify": clarify,
                                  "taxonomy_deferred": sum(
                                      1 for s in validated.accepted
                                      if relation_by_src.get(s) == "taxonomy_translation")})
            except ModelError as e:
                stats["model_error"] = type(e).__name__
                record_model_call(db, tracer, job_id=job_id, kind="transform_proposal", table_id=table_id,
                                  adapter_kind=(adapter.kind if adapter else "none"), model_id=model_id,
                                  columns_summary=col_summary, meta=None, status="error",
                                  error_category=stats["model_error"], input_extra=in_extra)
                db.add_audit(job_id, event_type="model_transform_error", actor="system",
                             source_ref={"profile_id": p.profile_id, "header": p.header, "table_id": table_id},
                             reason=f"{stats['model_error']}: {e}", model_version=model_id)
                return  # conservative on the free tier: stop this pass on a model error

    if budget_keys and adapter is not None:
        await _send([rep_of[k] for k in budget_keys], clarify=False, already_map=det.value_map)
        # ONE bounded semantic-clarification pass for values the model merely DECLINED (not ambiguous,
        # not taxonomy, not already mapped). Skipped entirely on a model/config error (no unsafe
        # fallback) — those values simply remain for the existing column-scoped human review.
        if stats["model_error"] is None:
            covered_folds = {_fold(v) for v in value_map}
            retry_keys = [k for k in budget_keys if k not in covered_folds
                          and k not in ambiguous_folds and k not in taxonomy_folds]
            if retry_keys:
                await _send([rep_of[k] for k in retry_keys], clarify=True, already_map=value_map)

    covered = {str(v).strip() for v in value_map}
    remaining = [v for v in domain if str(v).strip() and str(v).strip() not in covered and v not in value_map]
    if not remaining:
        status = AUTO_ACCEPTED
        prompt, options = "", []
    else:
        status = NEEDS_REVIEW
        cap_note = (f" ({len(over_budget_keys)} distinct value(s) exceeded the model budget of "
                    f"{max_distinct} and were held for this review)" if over_budget_keys else "")
        prompt = (f"Confirm the value map for '{p.header}' -> {target}: {len(remaining)} value(s) need a "
                  f"business-taxonomy decision ({', '.join(map(str, remaining[:6]))}){cap_note}.")
        options = [{"action": "map_values", "target": target, "allowed_values": list(tf.enum_values or ()),
                    "values": remaining}]
    plan = enum_map_plan(job_id, table_id, p.profile_id, p.header, target, value_map=value_map,
                         status=status, origin=origin,
                         evidence={**det.to_evidence(), "origin": origin, "taxonomy_decision": taxonomy,
                                   "remaining": remaining,
                                   "domain_distinct": len(domain),
                                   "deterministic_mapped": len(det.value_map),
                                   "unresolved_distinct_folded": len(fold_keys),
                                   "model_batches": stats["n_batches"], "batch_size": batch_size,
                                   "model_accepted": stats["n_model_accepted"],
                                   "semantic_clarify_batches": stats["clarify_batches"],
                                   "over_budget_distinct": len(over_budget_keys),
                                   "model_error": stats["model_error"]},
                         affected_rows=p.non_empty_count, review_prompt=prompt, review_options=options)
    db.upsert_transformation_plan(plan)
    kept.add(plan.id)


def _reference_domain(db, job_id, table_id, p, target, value_columns, employee_id_domain, schema, kept):
    values = value_columns.get(p.col_index, [])
    cov = validate_reference_domain(values, employee_id_domain)
    if cov.verdict == "valid":
        plan = derive_reference_plan(job_id, table_id, p.profile_id, p.header, target, value_map={},
                                     basis="domain", source_header=p.header, status=AUTO_ACCEPTED,
                                     evidence={"coverage": cov.to_evidence()}, affected_rows=cov.non_null)
        db.upsert_transformation_plan(plan)
        kept.add(plan.id)
    elif cov.verdict == "poor":
        db.upsert_decision(job_id, profile_id=p.profile_id, table_id=table_id, source_header=p.header,
                           target_field=None, status="reference_hold", actor="system", method="reference",
                           destination_kind="UNMAPPED",
                           reason=(f"'{p.header}' header looks like a manager reference, but only "
                                   f"{int(cov.coverage * 100)}% of its values match the employee-key domain. "
                                   f"Held for review rather than blindly mapped to '{target}'."))
        iid = f"iss_ref_{p.profile_id}"
        if db.get_issue(iid) is None:
            db.upsert_issue(job_id, issue_id=iid, profile_id=p.profile_id, table_id=table_id,
                            source_header=p.header, issue_type="reference_integrity",
                            proposed_target_field=target, candidate_target_fields=[target],
                            evidence_summary={"reference_check": cov.to_evidence(),
                                              "policy_version": POLICY_VERSION,
                                              "affected_non_empty_rows": cov.non_null},
                            affected_non_empty_rows=cov.non_null)
            db.add_audit(job_id, event_type="reference_integrity_failed", actor="system", issue_id=iid,
                         source_ref={"profile_id": p.profile_id, "header": p.header, "table_id": table_id},
                         after=cov.to_evidence(), reason=cov.reason, schema_version=schema.version,
                         policy_version=POLICY_VERSION)


def _manager_name_derivation(db, job_id, table_id, profiles, value_columns, accepted_target,
                             name_to_ids, schema, kept):
    if not name_to_ids:
        return
    if any(t == "manager_employee_id" for t in accepted_target.values()):
        return  # already validly covered by an ID column
    mgr = None
    for p in profiles:
        if accepted_target.get(p.profile_id):
            continue
        h = p.header.lower()
        if ("manager" in h or "supervisor" in h or "reporting" in h or "reports to" in h) and "name" in h:
            mgr = p
            break
    if mgr is None:
        return
    deriv = derive_reference_by_name(value_columns.get(mgr.col_index, []), name_to_ids)
    if not deriv.resolved:
        return
    status = AUTO_ACCEPTED if not (deriv.ambiguous or deriv.unmatched) else NEEDS_REVIEW
    prompt = ("" if status == AUTO_ACCEPTED else
              (f"Derive manager_employee_id from '{mgr.header}': {len(deriv.resolved)} name(s) resolve "
               f"uniquely, {len(deriv.ambiguous)} ambiguous, {len(deriv.unmatched)} unmatched."))
    plan = derive_reference_plan(job_id, table_id, mgr.profile_id, mgr.header, "manager_employee_id",
                                 value_map=deriv.resolved, basis="name", source_header=mgr.header,
                                 status=status, evidence={"derivation": deriv.to_evidence()},
                                 affected_rows=deriv.derived_count, review_prompt=prompt,
                                 review_options=[{"action": "approve_derivation"}] if status == NEEDS_REVIEW else [])
    db.upsert_transformation_plan(plan)
    kept.add(plan.id)
    db.add_audit(job_id, event_type="reference_derived", actor="system",
                 source_ref={"profile_id": mgr.profile_id, "header": mgr.header, "table_id": table_id},
                 after=deriv.to_evidence(), reason=deriv.reason, schema_version=schema.version,
                 policy_version=POLICY_VERSION)


def _skip_if_auto(db, job_id, profile_id, target) -> bool:
    """Bound work + model calls: an already AUTO_ACCEPTED plan for this column is stable; keep it."""
    existing = db.get_transformation_plan(plan_id(job_id, profile_id, target))
    return bool(existing and existing["status"] == AUTO_ACCEPTED)


# ============================================================================================
# Metrics (computed from persisted plans + relationships; single source of truth)
# ============================================================================================
def intelligence_metrics(db, job_id: str) -> dict:
    plans = [TransformPlan.from_row(r) for r in db.get_transformation_plans(job_id)]
    rels = db.get_column_relationships(job_id)
    profiles = db.get_profiles(job_id)

    def by(kind, status=None):
        return [p for p in plans if p.kind == kind and (status is None or p.status == status)]

    def last_op(p):
        return p.operations[-1] if p.operations else {}

    enum_auto_values = sum(len(last_op(p).get("value_map", {})) for p in by("enum", AUTO_ACCEPTED))
    ref_failed = len([i for i in db.get_issues(job_id) if i["issue_type"] == "reference_integrity"])
    return {
        "profile_version": "profile.v2",
        "columns_profiled": len(profiles),
        "low_cardinality_domains": sum(1 for r in profiles
                                       if json.loads(r["format_indicators"]).get("likely_low_cardinality_category")),
        "date_columns_auto_convention": len([p for p in by("date", AUTO_ACCEPTED) if last_op(p).get("order")]),
        "date_columns_needing_review": len(by("date", NEEDS_REVIEW)),
        "enum_values_auto_normalized": enum_auto_values,
        "enum_columns_needing_review": len(by("enum", NEEDS_REVIEW)),
        "boolean_columns_auto": len(by("boolean", AUTO_ACCEPTED)),
        "redundant_representations": len([p for p in plans if p.kind == "redundant"]),
        "inconsistent_code_display": sum(1 for r in rels if r["relationship"] == "inconsistent"),
        "references_validated": len([p for p in by("reference") if (p.operations[0] if p.operations else {}).get("basis") == "domain"]),
        "references_derived": len([p for p in by("reference") if (p.operations[0] if p.operations else {}).get("basis") == "name"]),
        "references_failed": ref_failed,
        "transforms_deterministic": len([p for p in plans if p.origin == "deterministic"]),
        "transforms_model": len([p for p in plans if p.origin == "model"]),
        "transforms_human": len([p for p in plans if p.origin == "human"]),
        "transforms_needs_review": len([p for p in plans if p.status == NEEDS_REVIEW]),
    }
