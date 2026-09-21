"""Deterministic-first mapping against the EFFECTIVE target contract (rules.v2).

Resolves a source column WITHOUT the model only when, after harmless case/space/underscore/
hyphen normalization, its header is exactly one canonical target key or one explicitly-declared
unambiguous alias in the effective contract — with no within-table collision. Destinations are
paths (see schema_loader): a core scalar, a collection item field, or a tenant custom field.

Table context (schema-configured, no model):
- A table / sheet whose name is a declared collection alias (e.g. sheet "Vehicles") is a CHILD
  table: its columns resolve against the employee key + THAT collection's item fields only.
- Any other table is an EMPLOYEE table: columns resolve against core fields, the tenant's custom
  fields, and the two declared legacy flattened shapes of a collection —
    indexed columns   "Vehicle 1", "Vehicle 2" (or a single "Vehicle Number")   -> item + index
    delimited column  "Vehicle Numbers" = "TN01AA1111; TN02BB2222"           -> multi_value
  Nothing else is grouped heuristically; anything unmatched stays UNRESOLVED for the bounded
  model path / human decision. Never strips semantic words (personal_email !-> work_email),
  never treats a generic token (id/name/date/start) alone as sufficient, and never concludes a
  field's role from value format. A syntactic outlier under an explicit canonical header is a
  data-quality flag only (mapping retained); per-row validation happens in preparation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .profiling import ColumnProfile
from .schema_loader import Collection, TargetSchema

RULE_VERSION = "rules.v2"

# Generic tokens that are never sufficient on their own to identify a target.
_GENERIC = {"id", "name", "contact", "date", "start", "code", "no", "number", "email", "mail"}


def _compact(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (header or "").lower())


def table_context_name(original_filename: str | None, sheet_name: str | None) -> str:
    """The name used to recognise a child table: the XLSX sheet name, else the CSV file stem
    without a leading numbering prefix ("03_vehicles.csv" -> "vehicles")."""
    if sheet_name:
        return sheet_name
    stem = PurePosixPath(original_filename or "").stem
    return re.sub(r"^[0-9_\-\s.]+", "", stem)


@dataclass
class RuleMapping:
    source_column_id: str
    header: str
    target: str | None                # destination PATH
    resolved: bool
    method: str                       # "rule" | "unresolved"
    rule_id: str | None
    evidence: list[str] = field(default_factory=list)
    reason: str = ""
    data_quality_flags: list[str] = field(default_factory=list)
    destination_kind: str = "UNMAPPED"   # CORE_FIELD | COLLECTION_FIELD | CUSTOM_FIELD | UNMAPPED
    path_meta: dict | None = None        # {"index": n} | {"multi_value": True, "delimiters": [...]} | {"single_item": True}


def _add(lookup: dict[str, str], ambiguous: set[str], key: str, path: str, *, canonical: bool) -> None:
    if not key:
        return
    if key in ambiguous:
        return
    prev = lookup.get(key)
    if prev is None:
        lookup[key] = path
    elif prev != path:
        if canonical:
            lookup[key] = path      # a canonical key beats an alias claim on the same token
        else:
            # Two different destinations declare the same alias -> ambiguous, never auto-resolve.
            ambiguous.add(key)
            del lookup[key]


def _build_employee_lookup(schema: TargetSchema) -> tuple[dict[str, str], set[str]]:
    lookup: dict[str, str] = {}
    ambiguous: set[str] = set()
    for f in schema.fields:
        _add(lookup, ambiguous, _compact(f.name), f.path, canonical=True)
    for f in schema.fields:
        for alias in f.aliases:
            _add(lookup, ambiguous, _compact(alias), f.path, canonical=False)
    for d in schema.custom_definitions:
        _add(lookup, ambiguous, _compact(d.name), d.path, canonical=True)
    for d in schema.custom_definitions:
        for alias in d.aliases:
            _add(lookup, ambiguous, _compact(alias), d.path, canonical=False)
    return lookup, ambiguous


def _build_child_lookup(schema: TargetSchema, coll: Collection) -> tuple[dict[str, str], set[str]]:
    lookup: dict[str, str] = {}
    ambiguous: set[str] = set()
    key_field = schema.get("employee_id")
    if key_field is not None:
        _add(lookup, ambiguous, _compact(key_field.name), key_field.path, canonical=True)
        for alias in key_field.aliases:
            _add(lookup, ambiguous, _compact(alias), key_field.path, canonical=False)
    for f in coll.fields:
        _add(lookup, ambiguous, _compact(f.name), f.path, canonical=True)
    for f in coll.fields:
        for alias in f.aliases:
            _add(lookup, ambiguous, _compact(alias), f.path, canonical=False)
    return lookup, ambiguous


def _legacy_collection_match(schema: TargetSchema, ch: str) -> tuple[str, dict, str] | None:
    """Schema-configured legacy flattened shapes in an EMPLOYEE table. Returns
    (path, path_meta, rule_suffix) or None."""
    for c in schema.collections:
        if c.multi_value_column:
            for alias in c.multi_value_column.aliases:
                if ch == _compact(alias):
                    return (f"{c.key}[].{c.multi_value_column.field}",
                            {"multi_value": True, "delimiters": list(c.multi_value_column.delimiters)},
                            "collection_multi_value_column")
        if c.indexed_column:
            for alias in c.indexed_column.aliases:
                a = _compact(alias)
                if ch == a:
                    return (f"{c.key}[].{c.indexed_column.field}", {"single_item": True},
                            "collection_single_column")
                if ch.startswith(a) and ch[len(a):].isdigit():
                    return (f"{c.key}[].{c.indexed_column.field}", {"index": int(ch[len(a):])},
                            "collection_indexed_column")
    return None


def _type_corroboration(target_value_type: str, profile: ColumnProfile) -> list[str]:
    """Soft data-quality flags; the canonical/alias header remains authoritative for MAPPING."""
    ind = profile.format_indicators
    flags: list[str] = []
    if profile.non_empty_count == 0:
        return flags
    if target_value_type == "email" and float(ind.get("email_ratio", 0)) < 0.5:
        flags.append("some values do not look like email addresses (per-row validation in preparation)")
    if target_value_type == "date" and not ind.get("looks_date_like"):
        flags.append("some values do not look like dates (per-row validation in preparation)")
    if target_value_type == "number" and profile.observed_types.get("number", 0) < profile.non_empty_count:
        flags.append("some values are not numeric (per-row validation in preparation)")
    return flags


def map_table_deterministic(profiles: list[ColumnProfile], schema: TargetSchema, *,
                            table_name: str | None = None) -> list[RuleMapping]:
    coll = schema.collection_for_table_name(table_name) if table_name else None
    if coll is not None:
        lookup, ambiguous = _build_child_lookup(schema, coll)
        ctx = f"child table of collection '{coll.key}'"
    else:
        lookup, ambiguous = _build_employee_lookup(schema)
        ctx = "employee table"

    tentative: dict[tuple, RuleMapping] = {}
    results: list[RuleMapping] = []

    for p in profiles:
        ch = _compact(p.header)
        target = lookup.get(ch)
        meta: dict | None = None
        rule_suffix = "canonical_or_alias"
        if not target and coll is None:
            legacy = _legacy_collection_match(schema, ch)
            if legacy:
                target, meta, rule_suffix = legacy
        if not target:
            why = ("header is a declared alias of more than one destination; escalated"
                   if ch in ambiguous else
                   f"normalized header '{ch}' did not match a canonical key or declared alias ({ctx})")
            results.append(RuleMapping(
                source_column_id=p.profile_id, header=p.header, target=None, resolved=False,
                method="unresolved", rule_id=None,
                reason="Header is not an exact canonical target key or declared alias; needs semantic interpretation.",
                evidence=[why]))
            continue

        tf = schema.get(target)
        dq = _type_corroboration(tf.value_type if tf else "string", p)
        rm = RuleMapping(
            source_column_id=p.profile_id, header=p.header, target=target, resolved=True,
            method="rule", rule_id=f"{RULE_VERSION}:{rule_suffix}",
            evidence=[f"header normalizes to canonical key/alias of '{target}' ({ctx})"],
            reason=f"Deterministic rule: header maps to '{target}'.",
            data_quality_flags=dq, destination_kind=schema.destination_kind(target), path_meta=meta)
        results.append(rm)

        # Within-table collision detection on (path, shape, index): indexed columns are distinct
        # items, and a delimited column is a different declared shape from a single-item column.
        shape = ("index" if (meta or {}).get("index") is not None else
                 "multi" if (meta or {}).get("multi_value") else
                 "single" if (meta or {}).get("single_item") else "scalar")
        ckey = (target, shape, (meta or {}).get("index"))
        if ckey in tentative:
            other = tentative[ckey]
            for victim in (rm, other):
                victim.resolved = False
                victim.method = "unresolved"
                victim.target = target  # keep the intended target for review context
                victim.rule_id = None
                victim.destination_kind = "UNMAPPED"
                victim.reason = (
                    f"Within-table header collision: multiple columns normalize to '{target}'. "
                    f"Escalated so a mapping is not silently overwritten.")
        else:
            tentative[ckey] = rm

    return results
