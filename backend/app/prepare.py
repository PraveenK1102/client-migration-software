"""Milestone 2 deterministic data preparation (+ M3A.2 collections and tenant custom attributes).

    accepted mappings (paths) + raw source rows + resolved human decisions (overlays)
        -> normalize (registered rules; no LLM)
        -> reconcile by employee_id (duplicate collapse, complementary merge, conflicts)
        -> attach structured child items by employee key (dedupe / merge / conflict per item identity)
        -> cross-record checks (shared email)
        -> validate (requiredness after merge, enum, dates, email syntax + uniqueness, item fields)
        -> candidates with eligibility + record issues

Pure and reproducible: :func:`prepare` is a function of persisted inputs only, so it can be
recomputed after any human decision. Record-issue ids are DETERMINISTIC so a resolved decision
keeps applying across recomputes. NO model calls occur here.

Destinations are paths from the effective contract:
  core scalar            employee_id                 -> candidate.record[field]
  collection item field  vehicles[].registration_number -> candidate.collections[coll][i].fields[key]
  tenant custom field    custom_attributes.tshirt_size  -> candidate.custom_attributes[]
Child rows come from relational child tables / sheets (attached by the employee key column) or
from the two schema-declared legacy flattened shapes (indexed columns, delimited multi-values).
Every child row keeps its provenance (file, sheet, row, header, raw); nothing is silently lost:
exact duplicates collapse (provenance merged), same-identity items with conflicting non-empty
values become a review issue, orphan child rows become a review issue.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date

from .date_inference import apply_date_value
from .schema_loader import CUSTOM_PATH_PREFIX, Collection, TargetField, TargetSchema
from .transform_plan import TransformPlan, resolved_params
from .validators import (
    canonicalize_department,
    canonicalize_enum,
    interpret_date,
    is_nonempty_string,
    normalize_boolean,
    normalize_number,
    split_multi_value,
    trim_preserving,
    validate_phone,
    validate_work_email,
)

NORMALIZATION_VERSION = "normalize.v2"
_ACCEPTED = ("auto_accepted", "approved", "corrected")


def _rid(job_id: str, issue_type: str, field: str | None, key: str | None) -> str:
    h = hashlib.sha1(f"{job_id}|{issue_type}|{field}|{key}".encode()).hexdigest()[:12]
    return f"ri_{h}"


def _vkey(v) -> str:
    """Comparison key for a normalized value (lists — multiselect — compare canonically)."""
    if isinstance(v, (list, dict)):
        return json.dumps(v, sort_keys=True, ensure_ascii=False)
    return str(v)


@dataclass
class RowRecord:
    table_id: str
    row_number: int
    original_filename: str
    sheet_name: str | None
    raw_id: str | None
    fields: dict[str, dict]                       # scalar/custom PATH -> {"raw","header","col_index"}
    kind: str = "employee"                        # employee | child
    child_items: dict[str, list[dict]] = field(default_factory=dict)  # collection -> [{"fields":{k:{raw,header,col_index}}, "origin", "index"}]


@dataclass
class FieldValue:
    value: object = None
    status: str = "missing"   # resolved|missing|invalid|unresolved|conflict
    rule: str | None = None
    reason: str | None = None
    provenance: list = field(default_factory=list)
    comparison_key: str | None = None   # for email uniqueness
    candidates: list = field(default_factory=list)  # e.g. ISO candidates for an ambiguous date


@dataclass
class CollectionItem:
    collection: str
    identity_key: str
    fields: dict[str, FieldValue]
    sources: list                       # row refs [{table_id,row_number,original_filename,sheet_name}]
    status: str = "resolved"            # resolved | conflict | invalid | dropped
    duplicates_collapsed: int = 0
    reason: str | None = None
    variants: list = field(default_factory=list)   # when conflict: [{variant_key, values, sources}]


@dataclass
class Candidate:
    id: str
    business_key: str | None
    fields: dict[str, FieldValue]                 # core scalars + custom paths
    source_refs: list
    eligibility: str = "blocked"
    issue_ids: list = field(default_factory=list)
    exclude_reason: str | None = None
    collections: dict[str, list[CollectionItem]] = field(default_factory=dict)


# --- build raw contributions from accepted mappings -----------------------
def build_row_records(db, job_id: str, schema: TargetSchema) -> list[RowRecord]:
    decisions = [d for d in db.get_decisions(job_id)
                 if d["target_field"] and d["status"] in _ACCEPTED]
    profiles = {p["id"]: p for p in db.get_profiles(job_id)}
    # table_id -> list of (col_index, path, header, path_meta)
    cols_by_table: dict[str, list[tuple[int, str, str, dict]]] = {}
    id_col_by_table: dict[str, int] = {}
    for d in decisions:
        prof = profiles.get(d["profile_id"])
        if not prof:
            continue
        meta = json.loads(d["path_meta"]) if d.get("path_meta") else {}
        cols_by_table.setdefault(prof["table_id"], []).append(
            (prof["col_index"], d["target_field"], prof["header"], meta))
        if d["target_field"] == "employee_id":
            id_col_by_table[prof["table_id"]] = prof["col_index"]

    rows: list[RowRecord] = []
    tables = {t["id"]: t for t in db.get_tables(job_id)}
    for table_id, cols in cols_by_table.items():
        t = tables.get(table_id, {})
        scalar_cols = [(ci, p, h, m) for ci, p, h, m in cols if schema_kind(schema, p) in ("scalar", "custom")]
        coll_cols = [(ci, p, h, m) for ci, p, h, m in cols if schema_kind(schema, p) == "collection_item"]
        # A CHILD table maps the employee key + collection item fields and nothing else.
        is_child = bool(coll_cols) and all(p == "employee_id" for _, p, _, _ in scalar_cols)
        for r in db.get_rows_for_table(table_id):
            cells = json.loads(r["cells"])

            def cell_val(ci):
                return cells[ci]["value"] if ci < len(cells) else None

            fields: dict[str, dict] = {}
            for ci, path, header, _ in scalar_cols:
                if is_child and path != "employee_id":
                    continue
                fields[path] = {"raw": cell_val(ci), "header": header, "col_index": ci}
            raw_id = cell_val(id_col_by_table[table_id]) if table_id in id_col_by_table else None
            rr = RowRecord(table_id=table_id, row_number=r["row_number"],
                           original_filename=t.get("original_filename", ""), sheet_name=t.get("sheet_name"),
                           raw_id=raw_id, fields=fields, kind="child" if is_child else "employee")
            _collect_child_items(rr, coll_cols, cell_val, is_child)
            rows.append(rr)
    return rows


def schema_kind(schema: TargetSchema, path: str) -> str:
    tf = schema.get(path)
    return tf.kind if tf else "unknown"


def _collect_child_items(rr: RowRecord, coll_cols, cell_val, is_child: bool) -> None:
    """Group a row's collection-mapped cells into raw child items.

    child table      -> exactly one item per collection per row (all mapped item fields)
    indexed columns  -> one item per (collection, index) built from every column carrying that index
    single column    -> one item (the flattened single-value shape)
    multi-value      -> one item per delimited part (split only on the declared delimiters)
    """
    groups: dict[tuple, dict] = {}
    for ci, path, header, meta in coll_cols:
        coll, item_key = path.split("[].", 1)
        raw = cell_val(ci)
        if is_child:
            gkey = (coll, "row", None)
            origin = "child_table"
        elif meta.get("index") is not None:
            gkey = (coll, "index", int(meta["index"]))
            origin = "indexed_column"
        elif meta.get("multi_value"):
            parts = split_multi_value(raw or "", tuple(meta.get("delimiters") or (";", "|")))
            for n, part in enumerate(parts):
                g = groups.setdefault((coll, "multi", ci, n), {"fields": {}, "origin": "multi_value_column",
                                                                 "index": n + 1})
                g["fields"][item_key] = {"raw": part, "header": header, "col_index": ci, "part": n + 1,
                                         "raw_cell": raw}
            continue
        else:
            gkey = (coll, "single", ci)
            origin = "single_column"
        g = groups.setdefault(gkey, {"fields": {}, "origin": origin, "index": gkey[2] if gkey[1] == "index" else None})
        g["fields"][item_key] = {"raw": raw, "header": header, "col_index": ci}
    for (coll, *_), g in groups.items():
        rr.child_items.setdefault(coll, []).append(g)


# --- transformation-plan helpers (declarative plan -> deterministic execution) ---------------
def _fold(s: str) -> str:
    return re.sub(r"[\s_\-]+", " ", str(s or "").strip().casefold())


def _lookup_value_map(raw: str, value_map: dict) -> str | None:
    """Apply a persisted enum/boolean value map to a raw value (exact, then case/space-folded)."""
    if not value_map:
        return None
    s = str(raw).strip()
    if s in value_map:
        return value_map[s]
    folded = {_fold(k): v for k, v in value_map.items()}
    return folded.get(_fold(s))


# --- normalization (registered, deterministic) ----------------------------
def normalize_value(tf: TargetField | None, raw, *, convention: str | None = None,
                    transform: dict | None = None, reference_date: date | None = None) -> FieldValue:
    """Normalize one raw cell against a target field's declared type. Never guesses.

    ``transform`` carries the resolved parameters of an accepted transformation plan for this column
    (a date order + optional century pivot, or an enum/boolean value map). It is applied
    deterministically; ``convention`` is a human date-convention overlay that overrides a plan order.
    """
    transform = transform or {}
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return FieldValue(value=None, status="missing", rule=NORMALIZATION_VERSION)
    raw_s = raw if isinstance(raw, str) else str(raw)
    vt = tf.value_type if tf else "string"

    if vt == "email":
        res = validate_work_email(raw_s)
        if res.status == "valid":
            return FieldValue(value=res.normalized, status="resolved", rule="email.normalize",
                              comparison_key=res.comparison_key)
        return FieldValue(value=trim_preserving(raw_s), status="invalid", rule="email.normalize", reason=res.reason)

    if vt == "date":
        order = convention or transform.get("order")     # human convention overrides plan order
        pivot = transform.get("pivot")
        if order is not None or pivot is not None:
            r = apply_date_value(raw_s, order=order, constraints=transform.get("constraints") or {},
                                 pivot=pivot, reference_date=reference_date)
            if r.status == "valid":
                return FieldValue(value=r.iso, status="resolved", rule="date.transform")
            if r.status == "ambiguous_century":
                return FieldValue(value=trim_preserving(raw_s), status="unresolved", rule="date.transform",
                                  reason=r.reason,
                                  candidates=[{"iso": c, "meaning": "century"} for c in r.century_candidates])
            return FieldValue(value=trim_preserving(raw_s), status="invalid", rule="date.transform", reason=r.reason)
        res = interpret_date(raw_s, convention=convention)
        if res.status == "valid":
            return FieldValue(value=res.iso, status="resolved", rule="date.interpret")
        if res.status == "ambiguous":
            return FieldValue(value=trim_preserving(raw_s), status="unresolved", rule="date.interpret",
                              reason=res.reason, candidates=res.candidates)
        return FieldValue(value=trim_preserving(raw_s), status="invalid", rule="date.interpret", reason=res.reason)

    if vt == "enum":
        mapped = _lookup_value_map(raw_s, transform.get("value_map"))
        if mapped is not None:
            return FieldValue(value=mapped, status="resolved", rule="enum.map")
        opts = list(tf.enum_values or ()) if tf else []
        canon, st = canonicalize_enum(raw_s, opts)
        if st == "canonical":
            return FieldValue(value=canon, status="resolved", rule="enum.canonicalize")
        return FieldValue(value=trim_preserving(raw_s), status="unresolved", rule="enum.canonicalize",
                          reason=f"value is not one of the allowed labels {opts}")

    if vt == "multiselect":
        opts = list(tf.enum_values or ()) if tf else []
        vmap = transform.get("value_map") or {}
        parts = split_multi_value(raw_s)
        canon_parts: list[str] = []
        for p in parts:
            mp = _lookup_value_map(p, vmap)
            if mp is not None:
                canon_parts.append(mp)
                continue
            if opts:
                c, st = canonicalize_enum(p, opts)
                if st != "canonical":
                    return FieldValue(value=trim_preserving(raw_s), status="unresolved", rule="multiselect.canonicalize",
                                      reason=f"'{p}' is not one of the allowed options {opts}")
                canon_parts.append(c)
            else:
                canon_parts.append(p)
        return FieldValue(value=canon_parts, status="resolved", rule="multiselect.split")

    if vt == "boolean" and transform.get("value_map"):
        mapped = _lookup_value_map(raw_s, transform.get("value_map"))
        if mapped is not None:
            return FieldValue(value=mapped, status="resolved", rule="boolean.map")

    if vt == "phone":
        res = validate_phone(raw_s)
        if res.status == "valid":
            return FieldValue(value=res.normalized, status="resolved", rule="phone.shape")
        return FieldValue(value=trim_preserving(raw_s), status="invalid", rule="phone.shape", reason=res.reason)

    if vt == "number":
        val, st = normalize_number(raw_s)
        if st == "valid":
            return FieldValue(value=val, status="resolved", rule="number.parse")
        return FieldValue(value=trim_preserving(raw_s), status="invalid", rule="number.parse",
                          reason="value is not a number")

    if vt == "boolean":
        val, st = normalize_boolean(raw_s)
        if st == "valid":
            return FieldValue(value=val, status="resolved", rule="boolean.parse")
        return FieldValue(value=trim_preserving(raw_s), status="invalid", rule="boolean.parse",
                          reason="value is not a recognised boolean (true/false/yes/no)")

    # string / identity: preserve verbatim except outer trim; never int-cast an id.
    trimmed = trim_preserving(raw_s)
    if not is_nonempty_string(trimmed):
        return FieldValue(value=None, status="missing", rule="string.trim")
    return FieldValue(value=trimmed, status="resolved", rule="string.trim")


def normalize_field(target: str, raw, schema: TargetSchema, *, convention: str | None = None,
                    transform: dict | None = None, reference_date: date | None = None) -> FieldValue:
    """Normalize by destination PATH (core scalar, custom attribute, or collection item field)."""
    tf = schema.get(target)
    transform = transform or {}
    if tf is not None and tf.value_type == "enum" and target == "department":
        # Department keeps its original label-match behaviour, extended with an accepted value map
        # (a confirmed business-taxonomy decision) applied at column level.
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            return FieldValue(value=None, status="missing", rule=NORMALIZATION_VERSION)
        mapped = _lookup_value_map(str(raw), transform.get("value_map"))
        if mapped is not None:
            return FieldValue(value=mapped, status="resolved", rule="enum.map")
        canon, st = canonicalize_department(str(raw), list(tf.enum_values or ()))
        if st == "canonical":
            return FieldValue(value=canon, status="resolved", rule="enum.canonicalize")
        return FieldValue(value=trim_preserving(str(raw)), status="unresolved", rule="enum.canonicalize",
                          reason="value is not a known department label")
    return normalize_value(tf, raw, convention=convention, transform=transform, reference_date=reference_date)


# --- reconciliation -------------------------------------------------------
def _prov(rr: RowRecord, header: str, raw, col_index: int | None = None, extra: dict | None = None) -> dict:
    p = {"table_id": rr.table_id, "row_number": rr.row_number,
         "original_filename": rr.original_filename, "sheet_name": rr.sheet_name,
         "header": header, "raw": raw, "col_index": col_index}
    if extra:
        p.update(extra)
    return p


def _row_ref(rr: RowRecord) -> dict:
    return {"table_id": rr.table_id, "row_number": rr.row_number,
            "original_filename": rr.original_filename, "sheet_name": rr.sheet_name}


def _load_plans(db, job_id: str, schema: TargetSchema) -> dict:
    """Accepted transformation-plan parameters, keyed for the preparation executor.

    ``transforms[(table_id, path)]`` = date order/pivot/constraints or an enum/boolean value map.
    ``derived_refs[(table_id, row_number)]`` = {"manager_employee_id": id} from a name-derivation plan.
    Only auto-accepted / human-approved plans are applied (needs_review plans are not executed).
    """
    transforms: dict[tuple[str, str], dict] = {}
    derived_refs: dict[tuple[str, int], dict] = {}
    for row in db.get_transformation_plans(job_id):
        if row["status"] not in ("auto_accepted", "approved"):
            continue
        plan = TransformPlan.from_row(row)
        params = resolved_params(row)
        if plan.kind == "date" and plan.target_field:
            transforms[(plan.table_id, plan.target_field)] = {
                "order": params.get("order"), "pivot": params.get("pivot"),
                "constraints": schema.temporal_constraints.get(plan.target_field, {})}
        elif plan.kind in ("enum", "boolean") and plan.target_field:
            transforms[(plan.table_id, plan.target_field)] = {"value_map": params.get("value_map") or {}}
        elif plan.kind == "reference" and (params.get("derive_reference") or {}).get("basis") == "name":
            dr = params["derive_reference"]
            vm = dr.get("value_map") or {}
            prof = db.get_profile(plan.profile_id)
            if not vm or not prof:
                continue
            ci = prof["col_index"]
            folded = {_fold(k): v for k, v in vm.items()}
            for r in db.get_rows_for_table(plan.table_id):
                cells = json.loads(r["cells"])
                raw = cells[ci]["value"] if ci < len(cells) else None
                if raw is None or str(raw).strip() == "":
                    continue
                emp = vm.get(str(raw).strip()) or folded.get(_fold(raw))
                if emp:
                    derived_refs.setdefault((plan.table_id, r["row_number"]), {})["manager_employee_id"] = emp
    return {"transforms": transforms, "derived_refs": derived_refs}


def prepare(db, job_id: str, schema: TargetSchema, *, reference_date: date | None = None) -> dict:
    """Recompute candidates + record issues from persisted inputs + resolved decisions + accepted
    transformation plans.

    Returns {"candidates":[...], "issues":[...], "metrics":{...}}. Idempotent. NO model calls.
    """
    reference_date = reference_date or date.today()
    overlays = _load_overlays(db, job_id)
    plans = _load_plans(db, job_id, schema)
    rows = build_row_records(db, job_id, schema)
    scalar_paths = [f.name for f in schema.fields] + [d.path for d in schema.custom_definitions]

    def _transform_for(table_id: str, path: str) -> dict:
        t = dict(plans["transforms"].get((table_id, path), {}))
        hv = overlays["enum_value_map"].get((table_id, path))
        if hv:                                    # human column-level value-map decision merges in
            vm = dict(t.get("value_map") or {})
            vm.update(hv)
            t["value_map"] = vm
        hp = overlays["century_pivot"].get((table_id, path))
        if hp is not None:
            t["pivot"] = hp
        return t

    # Normalize each employee row's scalar fields (plan transforms + human overlays, scoped by table+field).
    norm_rows: list[tuple[RowRecord, dict[str, FieldValue]]] = []
    child_rows: list[tuple[RowRecord, str | None]] = []       # (row, normalized employee key)
    for rr in rows:
        idinfo = rr.fields.get("employee_id")
        idfv = normalize_field("employee_id", idinfo["raw"], schema) if idinfo else FieldValue()
        key = idfv.value if idfv.status == "resolved" and idfv.value else None
        if rr.kind == "child":
            child_rows.append((rr, key))
            continue
        nf: dict[str, FieldValue] = {}
        for target, info in rr.fields.items():
            conv = overlays["date_convention"].get((rr.table_id, target))
            fv = normalize_field(target, info["raw"], schema, convention=conv,
                                 transform=_transform_for(rr.table_id, target), reference_date=reference_date)
            fv.provenance.append(_prov(rr, info["header"], info["raw"], info.get("col_index")))
            nf[target] = fv
        # M3C: inject a manager_employee_id DERIVED from an exact unique name match (never fuzzy),
        # only when it is not otherwise populated by a direct, validated column mapping.
        derived = plans["derived_refs"].get((rr.table_id, rr.row_number))
        if derived and "manager_employee_id" in derived:
            cur = nf.get("manager_employee_id")
            if cur is None or cur.status != "resolved" or not cur.value:
                dv = normalize_field("manager_employee_id", derived["manager_employee_id"], schema)
                dv.rule = "reference.derive_name"
                nf["manager_employee_id"] = dv
        norm_rows.append((rr, nf))
        if rr.child_items:
            child_rows.append((rr, key))

    # Group by trusted business key (normalized employee_id). Missing/blank id -> unique candidate.
    groups: dict[str, list[tuple[RowRecord, dict[str, FieldValue]]]] = {}
    singletons: list[tuple[RowRecord, dict[str, FieldValue]]] = []
    for rr, nf in norm_rows:
        idfv = nf.get("employee_id")
        key = idfv.value if idfv and idfv.status == "resolved" and idfv.value else None
        if key:
            groups.setdefault(key, []).append((rr, nf))
        else:
            singletons.append((rr, nf))

    issues: list[dict] = []
    candidates: list[Candidate] = []

    def merge_group(business_key, members) -> Candidate:
        cand_id = f"cand_{hashlib.sha1(f'{job_id}|{business_key}'.encode()).hexdigest()[:12]}"
        merged: dict[str, FieldValue] = {}
        src_refs = [_row_ref(rr) for rr, _ in members]
        for fld in scalar_paths:
            observed = [nf[fld] for _, nf in members if fld in nf]
            merged[fld] = _merge_field(business_key, fld, observed, issues, job_id)
        return Candidate(id=cand_id, business_key=business_key, fields=merged, source_refs=src_refs)

    for key, members in groups.items():
        candidates.append(merge_group(key, members))
    for rr, nf in singletons:
        cand_id = f"cand_{hashlib.sha1(f'{job_id}|nokey|{rr.table_id}|{rr.row_number}'.encode()).hexdigest()[:12]}"
        candidates.append(Candidate(id=cand_id, business_key=None, fields=nf, source_refs=[_row_ref(rr)]))

    # Attach structured child items by employee key (dedupe / merge / conflict per item identity).
    _attach_collections(candidates, child_rows, schema, overlays, issues, job_id)

    # Apply persisted human overlays (exclusions, value/null/candidate-scoped corrections)
    # BEFORE any cross-record constraint, so uniqueness is evaluated on EFFECTIVE values.
    for cand in candidates:
        _apply_overlays(cand, overlays, schema, issues, job_id, scalar_paths)

    # Cross-record: shared work_email across DIFFERENT business keys, on the post-overlay
    # effective values, ignoring excluded candidates. Recomputed every pass.
    _shared_email_checks(candidates, issues, job_id)

    # Validate every candidate against effective values + cross-record results. Ambiguous-date /
    # unknown-enum unresolved values are accumulated per column and emitted as ONE issue each.
    column_issues: dict = {}
    for cand in candidates:
        _validate_candidate(cand, schema, overlays, issues, job_id, column_issues)
    _emit_column_issues(job_id, column_issues, issues)
    # Now that all issues (incl. column-scoped) exist, attach the blocking-issue ids per candidate.
    for cand in candidates:
        if cand.eligibility != "excluded":
            cand.issue_ids = _candidate_issue_ids(cand, issues)

    metrics = _metrics(rows, candidates, issues)
    issue_dicts = _dedupe_issues(issues)
    return {"candidates": [_cand_to_dict(c, schema) for c in candidates],
            "issues": issue_dicts, "metrics": metrics}


def _merge_field(business_key, fld, observed: list[FieldValue], issues: list[dict], job_id: str) -> FieldValue:
    # Collapse provenance across duplicates.
    prov = [p for fv in observed for p in fv.provenance]
    non_null = [fv for fv in observed if fv.status == "resolved" and fv.value is not None]
    invalids = [fv for fv in observed if fv.status in ("invalid", "unresolved")]
    distinct: dict[str, object] = {}
    for fv in non_null:
        distinct.setdefault(_vkey(fv.value), fv.value)
    distinct_keys = sorted(distinct)

    if len(distinct_keys) > 1:
        # Per-value provenance so a reviewer sees WHICH source produced each option.
        by_value: dict[str, list] = {}
        for fv in non_null:
            by_value.setdefault(_vkey(fv.value), []).extend(fv.provenance)
        values = [distinct[k] for k in distinct_keys]
        options_detail = [{"value": distinct[k], "sources": by_value.get(k, [])} for k in distinct_keys]
        issues.append(_conflict_issue(job_id, business_key, fld, values, prov, options_detail))
        return FieldValue(value=None, status="conflict", rule="reconcile",
                          reason="different non-null values for the same employee id", provenance=prov)
    if distinct_keys:
        chosen = next(fv for fv in non_null if _vkey(fv.value) == distinct_keys[0])
        return FieldValue(value=chosen.value, status="resolved", rule=chosen.rule,
                          comparison_key=chosen.comparison_key, provenance=prov)
    if invalids:
        first = invalids[0]
        return FieldValue(value=first.value, status=first.status, rule=first.rule,
                          reason=first.reason, provenance=prov, candidates=first.candidates)
    return FieldValue(value=None, status="missing", rule="reconcile", provenance=prov)


def _conflict_issue(job_id, business_key, fld, values, prov, options_detail=None) -> dict:
    return {"id": _rid(job_id, "value_conflict", fld, business_key), "candidate_key": business_key,
            "field": fld, "issue_type": "value_conflict",
            "reason": f"Two or more source records disagree on '{fld}' for employee {business_key}.",
            "options": values,
            "affected": {"provenance": prov, "options_detail": options_detail or []},
            "scope": None, "_blocking": True}


# --- structured collections ----------------------------------------------
def _identity_key(coll: Collection, fields: dict[str, FieldValue]) -> str:
    parts = []
    for k in coll.item_identity:
        fv = fields.get(k)
        parts.append(_vkey(fv.value).strip().lower() if fv and fv.value not in (None, "") else "")
    if any(parts):
        return "|".join(parts)
    # No identity value at all: fall back to the full value tuple so exact duplicates still collapse.
    return "~" + hashlib.sha1(json.dumps({k: _vkey(v.value) for k, v in sorted(fields.items())},
                                         sort_keys=True).encode()).hexdigest()[:10]


def _item_values(item_fields: dict[str, FieldValue]) -> dict:
    return {k: fv.value for k, fv in sorted(item_fields.items())}


def _attach_collections(candidates: list[Candidate], child_rows, schema: TargetSchema, overlays: dict,
                        issues: list[dict], job_id: str) -> None:
    by_key: dict[str, Candidate] = {c.business_key: c for c in candidates if c.business_key}
    # Raw items per (business_key, collection)
    raw_items: dict[tuple[str, str], list[CollectionItem]] = {}
    orphans: dict[str, list[dict]] = {}      # table_id -> [{row_number, employee_id_raw, collection, item_values}]
    for rr, key in child_rows:
        for coll_key, items in rr.child_items.items():
            coll = schema.get_collection(coll_key)
            if coll is None:
                continue
            for raw_item in items:
                if (rr.table_id, rr.row_number) in overlays["excluded_rows"]:
                    continue  # explicitly excluded orphan row (audited human decision)
                fields: dict[str, FieldValue] = {}
                any_value = False
                for item_key, info in raw_item["fields"].items():
                    tf = coll.get(item_key)
                    fv = normalize_value(tf, info["raw"])
                    extra = {"part": info["part"], "raw_cell": info["raw_cell"]} if "part" in info else None
                    fv.provenance.append(_prov(rr, info["header"], info["raw"], info.get("col_index"), extra))
                    fields[item_key] = fv
                    if fv.value not in (None, "") or fv.status in ("invalid", "unresolved"):
                        any_value = True
                if not any_value:
                    continue  # an entirely blank flattened slot (e.g. empty "Vehicle 2") is not an item
                item = CollectionItem(collection=coll_key, identity_key=_identity_key(coll, fields),
                                      fields=fields, sources=[_row_ref(rr)])
                if key and key in by_key:
                    raw_items.setdefault((key, coll_key), []).append(item)
                else:
                    orphans.setdefault(rr.table_id, []).append({
                        "row_number": rr.row_number, "employee_id_raw": rr.raw_id, "collection": coll_key,
                        "values": _item_values(fields), "original_filename": rr.original_filename,
                        "sheet_name": rr.sheet_name})

    for (key, coll_key), items in raw_items.items():
        coll = schema.get_collection(coll_key)
        cand = by_key[key]
        effective = _reconcile_items(job_id, key, coll, items, overlays, issues)
        cand.collections[coll_key] = effective

    for table_id, rows in orphans.items():
        rows = sorted(rows, key=lambda r: r["row_number"])
        fn = rows[0]["original_filename"]
        sheet = rows[0]["sheet_name"]
        label = f"{fn} · {sheet}" if sheet else fn
        issues.append({
            "id": _rid(job_id, "orphan_child_row", table_id, "table"), "candidate_key": None, "field": None,
            "issue_type": "orphan_child_row",
            "reason": (f"{len(rows)} child row(s) in {label} reference an employee key that does not exist "
                       f"in any employee table (or have no key). They cannot be attached and will not be "
                       f"silently dropped."),
            "options": ["exclude"],
            "affected": {"table_id": table_id, "original_filename": fn, "sheet_name": sheet,
                         "rows": rows[:50], "row_count": len(rows),
                         "provenance": [{"table_id": table_id, "row_number": r["row_number"],
                                         "original_filename": fn, "sheet_name": sheet,
                                         "header": None, "raw": r["employee_id_raw"]} for r in rows[:50]]},
            "scope": {"table_id": table_id, "rows": [r["row_number"] for r in rows]}, "_blocking": True})


def _reconcile_items(job_id: str, key: str, coll: Collection, items: list[CollectionItem], overlays: dict,
                     issues: list[dict]) -> list[CollectionItem]:
    """Per item identity: collapse exact duplicates, merge complementary rows, escalate conflicts."""
    by_identity: dict[str, list[CollectionItem]] = {}
    for it in items:
        by_identity.setdefault(it.identity_key, []).append(it)
    out: list[CollectionItem] = []
    for identity, group in sorted(by_identity.items()):
        if (key, coll.key, identity) in overlays["dropped_items"]:
            continue
        merged = CollectionItem(collection=coll.key, identity_key=identity, fields={}, sources=[])
        merged.sources = _dedupe_refs([s for it in group for s in it.sources])
        all_keys = sorted({k for it in group for k in it.fields})
        variants: dict[str, dict] = {}
        for it in group:
            vk = hashlib.sha1(json.dumps({k: _vkey(v.value) for k, v in sorted(it.fields.items())},
                                         sort_keys=True).encode()).hexdigest()[:10]
            v = variants.setdefault(vk, {"variant_key": vk, "values": _item_values(it.fields), "sources": [],
                                         "provenance": []})
            v["sources"].extend(it.sources)
            v["provenance"].extend(p for fv in it.fields.values() for p in fv.provenance)
        merged.duplicates_collapsed = max(0, len(group) - len(variants))
        chosen_vk = overlays["variant_choice"].get((key, coll.key, identity))
        conflict_fields: list[str] = []
        for k in all_keys:
            observed = [it.fields[k] for it in group if k in it.fields]
            non_null = [fv for fv in observed if fv.status == "resolved" and fv.value not in (None, "")]
            distinct = {_vkey(fv.value) for fv in non_null}
            prov = [p for fv in observed for p in fv.provenance]
            if len(distinct) > 1 and chosen_vk is None:
                conflict_fields.append(k)
                merged.fields[k] = FieldValue(value=None, status="conflict", rule="reconcile.item",
                                              reason="conflicting values for the same item", provenance=prov)
            elif chosen_vk is not None and chosen_vk in variants:
                val = variants[chosen_vk]["values"].get(k)
                merged.fields[k] = FieldValue(value=val, status="resolved" if val not in (None, "") else "missing",
                                              rule="human.select", provenance=prov)
            elif non_null:
                first = non_null[0]
                merged.fields[k] = FieldValue(value=first.value, status="resolved", rule=first.rule, provenance=prov)
            else:
                inv = [fv for fv in observed if fv.status in ("invalid", "unresolved")]
                if inv:
                    merged.fields[k] = FieldValue(value=inv[0].value, status=inv[0].status, rule=inv[0].rule,
                                                  reason=inv[0].reason, provenance=prov, candidates=inv[0].candidates)
                else:
                    merged.fields[k] = FieldValue(value=None, status="missing", rule="reconcile.item", provenance=prov)
        # Human item-field corrections.
        for k in list(merged.fields) + [f.name for f in coll.fields]:
            corr = overlays["item_correction"].get((key, coll.key, identity, k))
            if corr is not None:
                fv = normalize_value(coll.get(k), corr)
                fv.rule = "human.correction"
                fv.provenance = merged.fields.get(k, FieldValue()).provenance
                merged.fields[k] = fv
        if conflict_fields:
            merged.status = "conflict"
            merged.reason = f"same {coll.label.lower()[:-1] if coll.label.endswith('s') else coll.label.lower()} item appears with different values for {conflict_fields}"
            merged.variants = list(variants.values())
            issues.append({
                "id": _rid(job_id, "collection_conflict", f"{coll.key}|{identity}", key),
                "candidate_key": key, "field": f"{coll.key}[]", "issue_type": "collection_conflict",
                "reason": (f"The same {coll.label.lower()} item ({', '.join(f'{i}={v!s}' for i, v in zip(coll.item_identity, identity.split('|')) if v)}) "
                           f"appears in {len(group)} source rows with different values for {conflict_fields}."),
                "options": [{"variant_key": v["variant_key"], "values": v["values"], "sources": v["sources"]}
                            for v in variants.values()],
                "affected": {"collection": coll.key, "identity_key": identity, "conflict_fields": conflict_fields,
                             "options_detail": [{"value": v["variant_key"], "values": v["values"],
                                                 "sources": v["provenance"]} for v in variants.values()],
                             "provenance": [p for v in variants.values() for p in v["provenance"]]},
                "scope": {"collection": coll.key, "identity_key": identity}, "_blocking": True})
        out.append(merged)
    return out


def _dedupe_refs(refs: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in refs:
        k = (r.get("table_id"), r.get("row_number"))
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _shared_email_checks(candidates: list[Candidate], issues: list[dict], job_id: str) -> None:
    by_email: dict[str, list[Candidate]] = {}
    for c in candidates:
        if c.eligibility == "excluded":
            continue  # an excluded candidate cannot participate in a collision
        fv = c.fields.get("work_email")
        if fv and fv.status == "resolved" and fv.comparison_key:
            by_email.setdefault(fv.comparison_key, []).append(c)
    for email_key, group in by_email.items():
        keys = {c.business_key for c in group}
        if len(group) > 1 and len(keys) > 1:  # same email, different employee ids
            issue_id = _rid(job_id, "shared_email", "work_email", email_key)
            affected = [{"candidate_id": c.id, "business_key": c.business_key} for c in group]
            issues.append({"id": issue_id, "candidate_key": None, "field": "work_email",
                           "issue_type": "shared_email",
                           "reason": f"Work email is shared across different employee ids {sorted(keys)}",
                           "options": [], "affected": {"candidates": affected}, "scope": None,
                           "_blocking": True, "_affects": [c.id for c in group]})


def _apply_overlays(cand: Candidate, overlays: dict, schema: TargetSchema, issues, job_id, scalar_paths) -> None:
    # Exclusions.
    if cand.business_key in overlays["excluded_keys"] or cand.id in overlays["excluded_ids"]:
        cand.eligibility = "excluded"
        cand.exclude_reason = overlays["exclude_reason"].get(cand.business_key) or \
            overlays["exclude_reason"].get(cand.id) or "excluded by reviewer"
        return
    # Field value overrides (correct/select), scoped to (business_key, field) OR, for
    # cross-record issues like shared_email, to (candidate_id, field). Iterate declared paths
    # so a correction can also fill a currently-missing field.
    for fld in scalar_paths:
        ov = overlays["value_override"].get((cand.business_key, fld))
        if ov is None:
            ov = overlays["value_override_by_id"].get((cand.id, fld))
        if ov is not None:
            prev = cand.fields.get(fld)
            fv = normalize_field(fld, ov, schema)  # human input must pass syntactic checks
            fv.rule = "human.correction"
            fv.provenance = prev.provenance if prev else []
            cand.fields[fld] = fv
        if (cand.business_key, fld) in overlays["nulled"] or (cand.id, fld) in overlays["nulled_ids"]:
            tf = schema.get(fld)
            if tf and not tf.required_in_final:   # never null a required field (defense in depth)
                cand.fields[fld] = FieldValue(value=None, status="resolved", rule="human.null",
                                              reason="explicitly set null by reviewer")


def _validate_candidate(cand: Candidate, schema: TargetSchema, overlays, issues, job_id,
                        column_issues: dict | None = None) -> None:
    if cand.eligibility == "excluded":
        return
    column_issues = column_issues if column_issues is not None else {}
    blocking = False
    targets = list(schema.fields) + list(schema.custom_definitions)
    for f in targets:
        path = f.path
        fv = cand.fields.get(path) or FieldValue()
        cand.fields.setdefault(path, fv)
        if path == "work_email" and fv.status == "resolved" and fv.value and not fv.comparison_key:
            fv.comparison_key = validate_work_email(fv.value).comparison_key
        # Unresolved / invalid / conflict values are handled by their own branches first.
        if fv.status == "conflict":
            blocking = True  # conflict issue already recorded in merge
            continue
        if fv.status == "unresolved":
            # An unresolved non-empty value (ambiguous date, unknown enum) can never silently pass —
            # even for an optional field. M3C: these are COLLAPSED to ONE column-scoped review per
            # (source table, field) so a single decision (a date convention or a value map) resolves
            # every affected row, instead of fanning out into hundreds of identical row issues.
            itype = "ambiguous_date" if f.value_type == "date" else (
                "unknown_enum" if f.value_type in ("enum", "multiselect") else "invalid_value")
            table_id = (fv.provenance[0].get("table_id") if fv.provenance else None) or "unknown"
            ckey = (table_id, path, itype)
            agg = column_issues.setdefault(ckey, {
                "table_id": table_id, "field": path, "issue_type": itype,
                "value_type": f.value_type, "enum_values": list(f.enum_values or ()),
                "values": {}, "candidate_ids": [], "date_candidates": []})
            raw_val = fv.value
            agg["values"][str(raw_val)] = agg["values"].get(str(raw_val), 0) + 1
            if cand.id not in agg["candidate_ids"]:
                agg["candidate_ids"].append(cand.id)
            if f.value_type == "date" and fv.candidates and not agg["date_candidates"]:
                agg["date_candidates"] = fv.candidates
            blocking = True
            continue
        if fv.status == "invalid":
            # An invalid non-empty value cannot silently disappear (required or optional).
            issues.append({"id": _rid(job_id, "invalid_value", path, cand.business_key or cand.id),
                           "candidate_key": cand.business_key, "field": path,
                           "issue_type": "invalid_value",
                           "reason": fv.reason or f"'{path}' has an invalid value.",
                           "options": [], "affected": {"candidate_id": cand.id, "raw": fv.value,
                                                       "provenance": fv.provenance},
                           "scope": None, "_blocking": True})
            blocking = True
            continue

        # status is 'resolved' or 'missing' here. A required field whose FINAL value is None or
        # empty is blocking regardless of status metadata (this catches a resolved-null too).
        effective_empty = fv.value is None or (isinstance(fv.value, str) and fv.value.strip() == "") \
            or (isinstance(fv.value, list) and not fv.value)
        if f.required_in_final and effective_empty:
            issues.append({"id": _rid(job_id, "missing_required", path, cand.business_key or cand.id),
                           "candidate_key": cand.business_key, "field": path,
                           "issue_type": "missing_required",
                           "reason": f"Required field '{path}' has no value after reconciliation "
                                     f"(a required field can never be a resolved null).",
                           "options": [], "affected": {"candidate_id": cand.id}, "scope": None,
                           "_blocking": True})
            blocking = True
            continue
        # optional missing / resolved value -> acceptable

    # Structured collection items: conflicts (already recorded) and item-field validity.
    for coll_key, items in cand.collections.items():
        coll = schema.get_collection(coll_key)
        if coll is None:
            continue
        for it in items:
            if it.status == "conflict":
                blocking = True
                continue
            bad: list[dict] = []
            for f in coll.fields:
                fv = it.fields.get(f.name) or FieldValue()
                empty = fv.value in (None, "") or (isinstance(fv.value, list) and not fv.value)
                if fv.status in ("invalid", "unresolved"):
                    bad.append({"field": f.name, "reason": fv.reason, "raw": fv.value,
                                "candidates": fv.candidates, "allowed": list(f.enum_values or ()) or None})
                elif f.required_in_final and empty and fv.status != "conflict":
                    bad.append({"field": f.name, "reason": f"required item field '{f.name}' is empty", "raw": None})
            if bad:
                it.status = "invalid"
                it.reason = "; ".join(f"{b['field']}: {b['reason']}" for b in bad)
                issues.append({
                    "id": _rid(job_id, "collection_item_invalid", f"{coll_key}|{it.identity_key}",
                               cand.business_key or cand.id),
                    "candidate_key": cand.business_key, "field": f"{coll_key}[]",
                    "issue_type": "collection_item_invalid",
                    "reason": (f"A {coll.label.lower()} item for employee {cand.business_key} has invalid or "
                               f"missing required values: {it.reason}."),
                    "options": [{"field": b["field"], "candidates": b.get("candidates") or [],
                                 "allowed": b.get("allowed")} for b in bad],
                    "affected": {"candidate_id": cand.id, "collection": coll_key, "identity_key": it.identity_key,
                                 "item": _item_values(it.fields), "problems": bad,
                                 "provenance": [p for fv in it.fields.values() for p in fv.provenance]},
                    "scope": {"collection": coll_key, "identity_key": it.identity_key}, "_blocking": True})
                blocking = True

    # shared-email blocking (issue recorded separately)
    if any(cand.id in iss.get("_affects", []) for iss in issues if iss["issue_type"] == "shared_email"):
        blocking = True

    # eligibility is finalised here; issue_ids are recomputed after column-scoped issues are emitted.
    cand.eligibility = "blocked" if blocking else "eligible"


def _candidate_issue_ids(cand: Candidate, issues: list[dict]) -> list[str]:
    return [iss["id"] for iss in issues
            if (iss.get("candidate_key") == cand.business_key and iss.get("candidate_key") is not None)
            or cand.id in iss.get("_affects", [])
            or iss.get("affected", {}).get("candidate_id") == cand.id]


def _emit_column_issues(job_id: str, column_issues: dict, issues: list[dict]) -> None:
    """Turn the per-column accumulator into ONE record issue per (table, field) so a single decision
    resolves every affected row (a date convention, or a value map for unknown enum values)."""
    for (table_id, field, itype), agg in column_issues.items():
        distinct = sorted(agg["values"], key=lambda v: (-agg["values"][v], v))
        n_rows = sum(agg["values"].values())
        if itype == "ambiguous_date":
            reason = (f"'{field}' has {len(distinct)} distinct value(s) across {n_rows} row(s) whose "
                      f"date convention is ambiguous; confirm the convention once for the whole column.")
            options = agg.get("date_candidates") or []
            scope = {"table_id": table_id, "field": field, "column_scoped": True}
        elif itype == "unknown_enum":
            reason = (f"'{field}' has {len(distinct)} value(s) not in the target set "
                      f"({', '.join(distinct[:8])}); map them once for the whole column.")
            options = list(agg.get("enum_values") or [])
            scope = {"table_id": table_id, "field": field, "column_scoped": True,
                     "unmapped_values": distinct}
        else:
            reason = f"'{field}' has unresolved value(s) across {n_rows} row(s)."
            options = []
            scope = {"table_id": table_id, "field": field, "column_scoped": True}
        issues.append({
            "id": _rid(job_id, itype, f"{table_id}|{field}", "column"),
            "candidate_key": None, "field": field, "issue_type": itype, "reason": reason,
            "options": options,
            "affected": {"table_id": table_id, "column_scoped": True, "row_count": n_rows,
                         "distinct_values": [{"value": v, "count": agg["values"][v]} for v in distinct[:50]],
                         "candidate_ids": agg["candidate_ids"][:200]},
            "scope": scope, "_blocking": True, "_affects": agg["candidate_ids"]})


def _load_overlays(db, job_id: str) -> dict:
    overlays = {"date_convention": {}, "value_override": {}, "value_override_by_id": {},
                "nulled": set(), "nulled_ids": set(),
                "excluded_keys": set(), "excluded_ids": set(), "exclude_reason": {},
                # M3A.2 collections
                "variant_choice": {}, "dropped_items": set(), "item_correction": {}, "excluded_rows": set(),
                # M3C column-scoped transform resolutions
                "enum_value_map": {}, "century_pivot": {}}
    for issue_id, res in db.get_resolved_record_decisions(job_id).items():
        iss = db.get_record_issue(issue_id)
        if not iss:
            continue
        action = res.get("action")
        key = iss["candidate_key"]
        fld = iss["field"]
        cand_id = res.get("candidate_id")
        aff = json.loads(iss["affected"]) if iss["affected"] else {}
        iss_scope = json.loads(iss["scope"]) if iss["scope"] else {}
        itype = iss["issue_type"]

        def _scoped_table_id() -> str | None:
            """Authoritative table for a column-scoped resolution. The scope lives on the ISSUE, not
            on the client's decision payload (which only carries version/action/convention/note), so
            derive it from the issue's scope/affected and only let an explicit res.scope override."""
            return ((res.get("scope") or {}).get("table_id")
                    or iss_scope.get("table_id") or aff.get("table_id"))

        if itype == "orphan_child_row":
            if action == "exclude":
                scope = json.loads(iss["scope"]) if iss["scope"] else {}
                for rn in scope.get("rows", []):
                    overlays["excluded_rows"].add((scope.get("table_id"), rn))
            continue
        if itype in ("collection_conflict", "collection_item_invalid"):
            coll, ident = aff.get("collection"), aff.get("identity_key")
            if action == "select" and res.get("value") is not None and key:
                overlays["variant_choice"][(key, coll, ident)] = res["value"]
            elif action == "drop_item" and key:
                overlays["dropped_items"].add((key, coll, ident))
            elif action == "correct" and res.get("value") is not None and key:
                item_field = (res.get("scope") or {}).get("item_field")
                if item_field:
                    overlays["item_correction"][(key, coll, ident, item_field)] = res["value"]
            elif action == "exclude":
                if key:
                    overlays["excluded_keys"].add(key)
                    overlays["exclude_reason"][key] = res.get("reason") or res.get("note")
            continue

        if action == "exclude":
            if key:
                overlays["excluded_keys"].add(key)
                overlays["exclude_reason"][key] = res.get("reason")
            # also allow excluding a specific candidate id (keyless singleton / one of a pair)
            target_cid = cand_id or aff.get("candidate_id")
            if target_cid:
                overlays["excluded_ids"].add(target_cid)
                overlays["exclude_reason"][target_cid] = res.get("reason")
        elif action in ("correct", "select") and res.get("value") is not None and fld:
            if key:                                   # candidate-key-scoped field correction
                overlays["value_override"][(key, fld)] = res["value"]
            elif cand_id:                             # cross-record (e.g. shared_email) correction
                overlays["value_override_by_id"][(cand_id, fld)] = res["value"]
        elif action == "null" and fld:
            if key:
                overlays["nulled"].add((key, fld))
            else:   # column-scoped null: drop an unknown OPTIONAL value for every affected candidate
                for cid in aff.get("candidate_ids", []):
                    overlays["nulled_ids"].add((cid, fld))
        elif action == "confirm_convention" and res.get("convention") and fld:
            tid = _scoped_table_id()
            if tid:
                overlays["date_convention"][(tid, fld)] = res["convention"]
        elif action == "confirm_century_pivot" and res.get("pivot") is not None and fld:
            tid = _scoped_table_id()
            if tid:
                overlays["century_pivot"][(tid, fld)] = int(res["pivot"])
        elif action == "map_values" and res.get("value_map") and fld:
            tid = _scoped_table_id()
            if tid:
                overlays["enum_value_map"].setdefault((tid, fld), {}).update(res["value_map"])
    return overlays


def finalize_invariant_error(candidates: list[dict], open_issue_count: int) -> str | None:
    """preparation_complete implies blocked == 0.

    A candidate still 'blocked' at finalize (reached only when the router saw 0 open record
    issues) has no reviewable issue — an inconsistent state that must NOT be finalized.
    Returns an error message when the invariant is violated, else None.
    """
    blocked = [c for c in candidates if c.get("eligibility") == "blocked"]
    if blocked:
        return (f"Preparation invariant failed: {len(blocked)} candidate(s) remain blocked with "
                f"{open_issue_count} open review issue(s). A blocked candidate must always have an "
                f"open reviewable issue; refusing to finalize.")
    return None


def _metrics(rows, candidates, issues) -> dict:
    n_items = sum(len(items) for c in candidates for items in c.collections.values())
    return {
        "source_rows_processed": len(rows),
        "employee_rows": len([r for r in rows if r.kind == "employee"]),
        "child_rows": len([r for r in rows if r.kind == "child"]),
        "candidate_employees": len(candidates),
        "collection_items": n_items,
        "eligible": len([c for c in candidates if c.eligibility == "eligible"]),
        "blocked": len([c for c in candidates if c.eligibility == "blocked"]),
        "excluded": len([c for c in candidates if c.eligibility == "excluded"]),
        "open_issue_estimate": len({i["id"] for i in issues if i.get("_blocking")}),
        "normalization_version": NORMALIZATION_VERSION,
    }


def _dedupe_issues(issues: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for i in issues:
        seen[i["id"]] = i  # last write wins; deterministic ids keep them stable
    return list(seen.values())


def _fv_dict(fv: FieldValue) -> dict:
    return {"value": fv.value, "status": fv.status, "rule": fv.rule, "reason": fv.reason, "provenance": fv.provenance}


def _cand_to_dict(c: Candidate, schema: TargetSchema) -> dict:
    core = {f.name for f in schema.fields}
    record = {fld: _fv_dict(fv) for fld, fv in c.fields.items() if fld in core}
    custom = []
    for d in schema.custom_definitions:
        fv = c.fields.get(d.path)
        if fv is None:
            continue
        custom.append({"definition_id": d.custom_definition_id, "key": d.name, "label": d.label,
                       "type": d.value_type, **_fv_dict(fv)})
    collections = {}
    for coll_key, items in c.collections.items():
        collections[coll_key] = [{
            "identity_key": it.identity_key, "status": it.status, "reason": it.reason,
            "duplicates_collapsed": it.duplicates_collapsed,
            "fields": {k: _fv_dict(fv) for k, fv in sorted(it.fields.items())},
            "sources": it.sources, "variants": it.variants,
        } for it in sorted(items, key=lambda x: x.identity_key)]
    return {"id": c.id, "business_key": c.business_key, "eligibility": c.eligibility,
            "exclude_reason": c.exclude_reason, "issue_ids": c.issue_ids,
            "source_refs": c.source_refs, "record": record,
            "collections": collections, "custom_attributes": custom}


# --- shared snapshot helpers (used by reconciliation + versions + prepared dataset) ----------
def effective_snapshot_from_candidate(cand_row: dict, schema: TargetSchema) -> dict:
    """The complete effective employee object: core scalars + collections (plain item values, sorted
    by identity) + custom attributes [{definition_id,key,value}] sorted by key. Excludes unresolved
    values (they never reach a snapshot)."""
    rec = json.loads(cand_row["record"]) if isinstance(cand_row.get("record"), str) else (cand_row.get("record") or {})
    colls = cand_row.get("collections") or {}
    if isinstance(colls, str):
        colls = json.loads(colls) if colls else {}
    custom = cand_row.get("custom_attributes") or []
    if isinstance(custom, str):
        custom = json.loads(custom) if custom else []
    out: dict = {}
    for f in schema.field_names:
        fv = rec.get(f) or {}
        out[f] = fv.get("value") if fv.get("status") == "resolved" else None
    out["collections"] = {}
    for c in schema.collections:
        items = []
        for it in colls.get(c.key, []) or []:
            if it.get("status") not in ("resolved",):
                continue
            items.append({k: (v or {}).get("value") for k, v in (it.get("fields") or {}).items()})
        out["collections"][c.key] = sorted(items, key=lambda x: json.dumps(x, sort_keys=True, default=str))
    out["custom_attributes"] = sorted(
        [{"definition_id": ca.get("definition_id"), "key": ca.get("key"), "value": ca.get("value")}
         for ca in custom if ca.get("status") == "resolved" and ca.get("value") not in (None, "", [])],
        key=lambda x: x["key"] or "")
    return out


def item_identity_key(coll: Collection, item: dict) -> str:
    parts = [str(item.get(k) if item.get(k) is not None else "").strip().lower() for k in coll.item_identity]
    if any(parts):
        return "|".join(parts)
    return "~" + hashlib.sha1(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()[:10]


__all__ = ["prepare", "build_row_records", "normalize_field", "normalize_value", "finalize_invariant_error",
           "effective_snapshot_from_candidate", "item_identity_key", "CUSTOM_PATH_PREFIX", "NORMALIZATION_VERSION"]
