"""Tenant custom-field proposals (M3A.2, Part D).

A source column with no core / collection / existing-custom destination is never silently
discarded and never auto-creates a target field. Instead the system records a *proposal*:
a deterministic suggestion (key, label, type, observed values) that an implementation
consultant must approve (create the tenant definition), map to an existing custom field, map
to a target path, or explicitly ignore. All of that is persisted and audited.

Everything here is deterministic and bounded (no model call):
- ``suggest_definition`` infers a stable key + a conservative type from the header and the
  bounded observed values (boolean / number / date / enum / multiselect / string).
- ``proposal_id`` is deterministic per (job, column) so replaying mapping creates no duplicate.
"""
from __future__ import annotations

import hashlib
import re

from .schema_loader import CUSTOM_PATH_PREFIX, TargetSchema
from .validators import interpret_date, is_boolean_like, is_number_like

MAX_OBSERVED = 20
_BOOL = {"true", "false", "yes", "no", "y", "n", "1", "0"}
_RESERVED_KEYS = {"custom_attributes", "collections", "id", "record"}


def slugify_key(text: str, *, max_len: int = 48) -> str:
    """Stable machine key from a header: lowercase, [a-z0-9_], starts with a letter."""
    t = (text or "").lower()
    # "T-Shirt" / "E-Mail": a single-letter token joined by a hyphen is one word (tshirt, email).
    t = re.sub(r"\b([a-z])-(?=[a-z])", r"\1", t)
    s = re.sub(r"[^a-z0-9]+", "_", t).strip("_")
    s = re.sub(r"_+", "_", s)
    if not s:
        s = "field"
    if not s[0].isalpha():
        s = "f_" + s
    return s[:max_len].rstrip("_") or "field"


def proposal_id(job_id: str, profile_id: str) -> str:
    return f"cfp_{hashlib.sha1(f'{job_id}|{profile_id}'.encode()).hexdigest()[:12]}"


def _unique_key(base: str, schema: TargetSchema, taken: set[str]) -> str:
    core = set(schema.field_names) | {c.key for c in schema.collections} | _RESERVED_KEYS
    existing = {d.name for d in schema.custom_definitions} | taken
    key = base
    n = 2
    while key in core or key in existing:
        key = f"{base}_{n}"
        n += 1
    return key


def infer_type(values: list[str]) -> tuple[str, list[str] | None, bool]:
    """(type, options, multi_value) from bounded non-empty observed values. Conservative:
    enum only for a small, repeated label set; multiselect only with explicit ';' delimiters."""
    vals = [v.strip() for v in values if v is not None and str(v).strip() != ""]
    if not vals:
        return "string", None, False
    if all(v.lower() in _BOOL for v in vals) and len({v.lower() for v in vals}) <= 2 and all(is_boolean_like(v) for v in vals):
        return "boolean", None, False
    if all(is_number_like(v) for v in vals):
        return "number", None, False
    if all(interpret_date(v).status in ("valid", "ambiguous") for v in vals):
        return "date", None, False
    if all(";" in v for v in vals):
        parts = sorted({p.strip() for v in vals for p in v.split(";") if p.strip()})
        if parts and all(len(p) <= 40 for p in parts):
            return "multiselect", parts, True
    distinct = sorted(set(vals))
    if len(distinct) <= 8 and all(len(v) <= 40 for v in distinct) and len(distinct) < max(2, len(vals)):
        return "enum", distinct, False
    return "string", None, False


def suggest_definition(header: str, samples: list[str], schema: TargetSchema,
                       taken_keys: set[str] | None = None) -> dict:
    vtype, options, multi = infer_type(samples)
    label = re.sub(r"\s+", " ", (header or "").strip()) or "Custom field"
    key = _unique_key(slugify_key(label), schema, taken_keys or set())
    return {"key": key, "label": label, "type": vtype, "options": options,
            "multi_value": multi, "required": False,
            "path": f"{CUSTOM_PATH_PREFIX}{key}"}


def validate_definition_input(schema: TargetSchema, key: str, label: str, vtype: str,
                              options: list | None, multi_value: bool) -> str | None:
    """Return an error message if the requested definition is invalid, else None."""
    contract = schema.custom_contract
    if not key or not re.match(contract.key_pattern, key):
        return f"key must match {contract.key_pattern}"
    if key in set(schema.field_names) or key in {c.key for c in schema.collections} or key in _RESERVED_KEYS:
        return f"key '{key}' collides with a core field or collection"
    if not (label or "").strip():
        return "label is required"
    if vtype not in contract.allowed_types:
        return f"type must be one of {list(contract.allowed_types)}"
    if vtype in ("enum", "multiselect"):
        opts = [str(o).strip() for o in (options or []) if str(o).strip()]
        if not opts:
            return f"'{vtype}' requires at least one option"
        if len(set(opts)) != len(opts):
            return "options must be distinct"
    if multi_value and vtype not in ("multiselect", "string"):
        return "multi_value is only supported for multiselect / string custom fields"
    return None
