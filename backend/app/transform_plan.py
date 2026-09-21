"""Declarative, versioned transformation plans (transform.v1, M3C).

The central M3C architecture:

    the model PROPOSES semantics  ->  deterministic evidence PROVES the plan  ->
    a small whitelist of REGISTERED operations EXECUTES it  ->  humans intervene only when the
    file itself lacks defensible evidence.

A transformation plan is DATA, never code. The model can never emit executable Python/regex/SQL/
Jinja/shell or an arbitrary target value: it may only propose values that this module validates
against the whitelist and the effective target schema. Deterministic preparation (:mod:`prepare`)
executes only these registered operations, so a plan is reproducible and idempotent.

Allowed operations (the ONLY ones preparation will execute):
    trim · preserve_string · date_parse · enum_map · boolean_map · number_parse ·
    split_declared_multi_value · derive_exact_reference · redundant_representation
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .schema_loader import TargetField

TRANSFORM_VERSION = "transform.v1"

ALLOWED_OPS = frozenset({
    "trim", "preserve_string", "date_parse", "enum_map", "boolean_map", "number_parse",
    "split_declared_multi_value", "derive_exact_reference", "redundant_representation",
})

# Plan lifecycle.
AUTO_ACCEPTED = "auto_accepted"
NEEDS_REVIEW = "needs_review"
APPROVED = "approved"
REJECTED = "rejected"


def plan_id(job_id: str, profile_id: str, target_field: str | None) -> str:
    h = hashlib.sha1(f"{job_id}|{profile_id}|{target_field or ''}".encode()).hexdigest()[:12]
    return f"tp_{h}"


@dataclass
class TransformPlan:
    id: str
    job_id: str
    table_id: str
    profile_id: str
    source_header: str
    target_field: str | None
    kind: str                       # date | enum | boolean | reference | redundant | number | string
    operations: list[dict]
    origin: str                     # deterministic | model | human
    status: str                     # auto_accepted | needs_review | approved | rejected
    evidence: dict = field(default_factory=dict)
    affected_rows: int = 0
    review_prompt: str = ""
    review_options: list = field(default_factory=list)
    resolution: dict | None = None

    def to_row(self) -> dict:
        return {"id": self.id, "job_id": self.job_id, "table_id": self.table_id,
                "profile_id": self.profile_id, "source_header": self.source_header,
                "target_field": self.target_field, "kind": self.kind,
                "operations": self.operations, "origin": self.origin, "status": self.status,
                "evidence": self.evidence, "affected_rows": self.affected_rows,
                "review_prompt": self.review_prompt, "review_options": self.review_options,
                "resolution": self.resolution}

    @staticmethod
    def from_row(row: dict) -> "TransformPlan":
        def _load(v, default):
            if v is None:
                return default
            return json.loads(v) if isinstance(v, str) else v
        return TransformPlan(
            id=row["id"], job_id=row["job_id"], table_id=row["table_id"], profile_id=row["profile_id"],
            source_header=row["source_header"], target_field=row["target_field"], kind=row["kind"],
            operations=_load(row.get("operations"), []), origin=row["origin"], status=row["status"],
            evidence=_load(row.get("evidence"), {}), affected_rows=row.get("affected_rows") or 0,
            review_prompt=row.get("review_prompt") or "", review_options=_load(row.get("review_options"), []),
            resolution=_load(row.get("resolution"), None))


# --- resolved parameters for the preparation executor -----------------------------------------
def resolved_params(plan_row: dict) -> dict:
    """Flatten a plan's operations into the parameters preparation needs to execute it."""
    ops = plan_row.get("operations")
    if isinstance(ops, str):
        ops = json.loads(ops) if ops else []
    out: dict = {"kind": plan_row.get("kind")}
    for op in ops or []:
        name = op.get("op")
        if name == "date_parse":
            out["order"] = op.get("order")
            out["pivot"] = (op.get("century") or {}).get("pivot")
        elif name in ("enum_map", "boolean_map"):
            out["value_map"] = dict(op.get("value_map") or {})
        elif name == "derive_exact_reference":
            out["derive_reference"] = {"value_map": dict(op.get("value_map") or {}),
                                       "basis": op.get("basis"), "source_header": op.get("source_header")}
        elif name == "redundant_representation":
            out["redundant"] = True
    return out


# --- operation constructors (deterministic evidence -> plan) ----------------------------------
def date_parse_plan(job_id, table_id, profile_id, header, target, *, order, century_method="constraint",
                    pivot=None, status, evidence, affected_rows=0, origin="deterministic",
                    review_prompt="", review_options=None) -> TransformPlan:
    ops = [{"op": "trim"}, {"op": "date_parse", "order": order,
                            "century": {"method": century_method, "pivot": pivot}}]
    return TransformPlan(plan_id(job_id, profile_id, target), job_id, table_id, profile_id, header,
                         target, "date", ops, origin, status, evidence, affected_rows,
                         review_prompt, review_options or [])


def enum_map_plan(job_id, table_id, profile_id, header, target, *, value_map, status, evidence,
                  affected_rows=0, origin="deterministic", review_prompt="", review_options=None) -> TransformPlan:
    ops = [{"op": "trim"}, {"op": "enum_map", "value_map": dict(value_map), "target": target}]
    return TransformPlan(plan_id(job_id, profile_id, target), job_id, table_id, profile_id, header,
                         target, "enum", ops, origin, status, evidence, affected_rows,
                         review_prompt, review_options or [])


def boolean_map_plan(job_id, table_id, profile_id, header, target, *, value_map, status, evidence,
                     affected_rows=0, origin="deterministic") -> TransformPlan:
    ops = [{"op": "trim"}, {"op": "boolean_map", "value_map": dict(value_map)}]
    return TransformPlan(plan_id(job_id, profile_id, target), job_id, table_id, profile_id, header,
                         target, "boolean", ops, origin, status, evidence, affected_rows)


def redundant_plan(job_id, table_id, profile_id, header, *, redundant_with, evidence,
                   affected_rows=0) -> TransformPlan:
    ops = [{"op": "redundant_representation", "redundant_with": redundant_with}]
    return TransformPlan(plan_id(job_id, profile_id, "__redundant__"), job_id, table_id, profile_id,
                         header, None, "redundant", ops, "deterministic", AUTO_ACCEPTED, evidence,
                         affected_rows)


def derive_reference_plan(job_id, table_id, profile_id, header, target, *, value_map, basis,
                          source_header, status, evidence, affected_rows=0,
                          review_prompt="", review_options=None) -> TransformPlan:
    ops = [{"op": "derive_exact_reference", "value_map": dict(value_map), "basis": basis,
            "source_header": source_header}]
    return TransformPlan(plan_id(job_id, profile_id, target), job_id, table_id, profile_id, header,
                         target, "reference", ops, "deterministic", status, evidence, affected_rows,
                         review_prompt, review_options or [])


# --- validation of MODEL-proposed operations (defense: never trust model output) --------------
def validate_model_operations(ops: list, tf: TargetField | None) -> tuple[list[dict], str | None]:
    """Validate a model-proposed operation list against the whitelist and the target schema.

    Returns (cleaned_ops, error). Any unknown op, arbitrary target value, or malformed param is a
    hard reject — the model can never introduce executable content or an out-of-schema value.
    """
    if not isinstance(ops, list):
        return [], "operations must be a list"
    allowed_enum = set(tf.enum_values or ()) if (tf and tf.value_type in ("enum", "multiselect")) else None
    cleaned: list[dict] = []
    for op in ops:
        if not isinstance(op, dict) or "op" not in op:
            return [], "each operation must be an object with an 'op'"
        name = op["op"]
        if name not in ALLOWED_OPS:
            return [], f"operation '{name}' is not in the allowed whitelist {sorted(ALLOWED_OPS)}"
        if name in ("trim", "preserve_string", "number_parse"):
            cleaned.append({"op": name})
        elif name == "enum_map":
            vm = op.get("value_map")
            if not isinstance(vm, dict):
                return [], "enum_map requires a value_map object"
            out_vm = {}
            for k, v in vm.items():
                if allowed_enum is not None and v not in allowed_enum:
                    return [], f"enum_map target '{v}' is not in the target enum {sorted(allowed_enum)}"
                out_vm[str(k)] = v
            cleaned.append({"op": "enum_map", "value_map": out_vm})
        elif name == "boolean_map":
            vm = op.get("value_map")
            if not isinstance(vm, dict) or any(v not in ("true", "false") for v in vm.values()):
                return [], "boolean_map values must be 'true'/'false'"
            cleaned.append({"op": "boolean_map", "value_map": {str(k): v for k, v in vm.items()}})
        elif name == "date_parse":
            order = op.get("order")
            if order not in ("MDY", "DMY", "YMD", None):
                return [], "date_parse order must be MDY/DMY/YMD/null"
            cleaned.append({"op": "date_parse", "order": order, "century": {"method": "constraint", "pivot": None}})
        else:
            # split_declared_multi_value / derive_exact_reference / redundant_representation are
            # never accepted directly from the model; they are built from deterministic evidence.
            return [], f"operation '{name}' may not be proposed by the model"
    return cleaned, None
