"""Frozen alias/target inventory + deterministic-header audit (M3E anti-overfitting).

The generalization proof rests on one fact: the deterministic mapper resolves a header WITHOUT a
model call only when its ``_compact`` form is exactly a canonical target key or an explicitly-declared
``aliases:`` synonym (see :func:`app.mapping_rules.map_table_deterministic`). ``disambiguation_keywords``
and ``value_aliases`` are NOT part of that set — keywords only help the *policy* validate a model
proposal, and value_aliases only normalize enum VALUES, neither bypasses the model.

This module exposes that authoritative set (so a test can assert an unseen header is not in it),
plus a stable inventory hash (so a run can prove the alias list did not grow as a side effect), and a
full inventory for the human-readable schema/alias audit report. Pure and deterministic; no I/O.
"""
from __future__ import annotations

import hashlib
import json

from .mapping_rules import _build_employee_lookup, _compact
from .schema_loader import TargetField, TargetSchema


def deterministic_header_keys(schema: TargetSchema) -> set[str]:
    """The exact set of ``_compact`` header strings the deterministic rule will resolve in an EMPLOYEE
    table — canonical field/custom keys + their declared header aliases, minus any that collide into
    ambiguity. This is the authoritative overfitting surface: a header whose compact form is here is
    hard-coded; anything else must go through the model."""
    lookup, _ambiguous = _build_employee_lookup(schema)
    return set(lookup.keys())


def resolves_deterministically(schema: TargetSchema, header: str) -> str | None:
    """The target path the deterministic rule would assign to ``header`` in an employee table, or None
    if it stays unresolved. (Legacy collection-shape columns are intentionally out of scope here.)"""
    lookup, _ambiguous = _build_employee_lookup(schema)
    return lookup.get(_compact(header))


def _field_inventory(f: TargetField) -> dict:
    return {
        "key": f.name,
        "path": f.path,
        "type": f.value_type,
        "required": f.required_in_final,
        "canonical_compact": _compact(f.name),
        "header_aliases": list(f.aliases),
        "header_aliases_compact": sorted({_compact(a) for a in f.aliases}),
        "value_aliases": {canon: list(al) for canon, al in f.value_aliases},
        "enum_values": list(f.enum_values) if f.enum_values else None,
        "disambiguation_keywords": list(f.disambiguation_keywords),
    }


def alias_inventory(schema: TargetSchema) -> dict:
    """A complete, ordered inventory of every deterministic mapping anchor in the schema.

    ``header_alias_compact_set`` / ``canonical_compact_set`` are the deterministic resolution surface;
    ``disambiguation_keywords`` and ``value_aliases`` are reported but are NOT part of that surface.
    """
    core = [_field_inventory(f) for f in schema.fields]
    collections = [
        {
            "key": c.key,
            "table_aliases": list(c.table_aliases),
            "fields": [_field_inventory(f) for f in c.fields],
        }
        for c in schema.collections
    ]
    canonical = sorted({_compact(f.name) for f in schema.fields})
    header_aliases = sorted({_compact(a) for f in schema.fields for a in f.aliases})
    return {
        "schema_version": schema.version,
        "core_fields": core,
        "collections": collections,
        "canonical_compact_set": canonical,
        "header_alias_compact_set": header_aliases,
        "deterministic_header_keys": sorted(deterministic_header_keys(schema)),
        "counts": {
            "core_fields": len(schema.fields),
            "core_canonical_keys": len(canonical),
            "core_header_aliases": len(header_aliases),
            "deterministic_header_keys": len(deterministic_header_keys(schema)),
            "collections": len(schema.collections),
        },
    }


def inventory_hash(schema: TargetSchema) -> str:
    """Stable SHA-256 over the deterministic resolution surface (canonical keys + header aliases,
    core + collections). Used to prove no alias was added/removed across a run."""
    inv = alias_inventory(schema)
    surface = {
        "schema_version": inv["schema_version"],
        "canonical_compact_set": inv["canonical_compact_set"],
        "header_alias_compact_set": inv["header_alias_compact_set"],
        "deterministic_header_keys": inv["deterministic_header_keys"],
        "collections": [
            {"key": c["key"],
             "table_aliases": sorted(_compact(a) for a in c["table_aliases"]),
             "fields": sorted(
                 {ac for fld in c["fields"] for ac in ([fld["canonical_compact"]] + fld["header_aliases_compact"])})}
            for c in inv["collections"]
        ],
    }
    blob = json.dumps(surface, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()
