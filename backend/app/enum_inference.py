"""Column-level enum / boolean value-map inference and validation (M3C).

Enum normalization is a COLUMN-LEVEL transformation, not one review per employee. Given a target
enum field and the complete low-cardinality source domain, this builds a reusable value map:

    Tier 1 (deterministic): exact / case / space / hyphen / underscore canonical equivalence, plus
      the schema's explicitly-declared LEXICAL value aliases (e.g. gender {F: female, M: male}).
    Tier 2 (model, validated here): a model may PROPOSE a value map for the remaining low-cardinality
      values; :func:`validate_model_value_map` proves it deterministically against the target enum
      (every target value exists; every observed source value accounted for; nothing unobserved;
      ambiguous proposals escalate) before anything is applied.

A business-taxonomy translation (e.g. "Research & Development" -> "Engineering") is NOT lexical and
is never auto-mapped by Tier 1; it stays for a scoped human decision unless the tenant schema
explicitly declares it. Unknown / unseen future values are never forced through an existing map.

Pure and deterministic. No model call happens here — the model's output is only VALIDATED here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .schema_loader import TargetField
from .validators import canonicalize_enum, normalize_boolean

ENUM_TRANSFORM_VERSION = "enum_map.v1"


def _fold(s: str) -> str:
    return re.sub(r"[\s_\-]+", " ", (s or "").strip().casefold())


@dataclass
class EnumMapInference:
    target: str
    status: str                          # complete | partial | not_applicable
    value_map: dict[str, str] = field(default_factory=dict)      # source_value -> canonical target label
    origin_by_value: dict[str, str] = field(default_factory=dict)  # source_value -> canonical | alias
    unmapped: list[str] = field(default_factory=list)
    reason: str = ""

    def to_evidence(self) -> dict:
        return {"version": ENUM_TRANSFORM_VERSION, "target": self.target, "status": self.status,
                "value_map": self.value_map, "origin_by_value": self.origin_by_value,
                "unmapped": self.unmapped, "reason": self.reason}


def build_enum_map(tf: TargetField, distinct_values: list[str]) -> EnumMapInference:
    """Deterministic Tier-1 map from a target enum/boolean field + observed distinct source values."""
    if tf is None or tf.value_type not in ("enum", "multiselect", "boolean"):
        return EnumMapInference(target=(tf.path if tf else ""), status="not_applicable",
                                reason="target is not an enum/boolean field")

    value_map: dict[str, str] = {}
    origin: dict[str, str] = {}
    unmapped: list[str] = []

    if tf.value_type == "boolean":
        for v in distinct_values:
            canon, st = normalize_boolean(v)
            if st == "valid":
                value_map[v] = canon
                origin[v] = "canonical"
            elif str(v).strip():
                unmapped.append(v)
    else:
        enum_values = list(tf.enum_values or ())
        # Normalized declared-alias lookup (fold both sides so casing/spacing is forgiving).
        alias_fold = {_fold(a): canon for a, canon in tf.value_alias_map().items()}
        for v in distinct_values:
            if not str(v).strip():
                continue
            canon, st = canonicalize_enum(v, enum_values)
            if st == "canonical":
                value_map[v] = canon
                origin[v] = "canonical"
                continue
            hit = alias_fold.get(_fold(v))
            if hit is not None and hit in enum_values:
                value_map[v] = hit
                origin[v] = "alias"
                continue
            unmapped.append(v)

    status = "complete" if not unmapped else ("partial" if value_map else "partial")
    reason = ("every observed value maps to the target enum via canonical/declared-alias equivalence"
              if not unmapped else
              f"{len(unmapped)} value(s) have no deterministic mapping and need semantic interpretation")
    return EnumMapInference(target=tf.path, status=status, value_map=value_map,
                            origin_by_value=origin, unmapped=unmapped, reason=reason)


@dataclass
class ValidatedValueMap:
    ok: bool
    target: str
    accepted: dict[str, str] = field(default_factory=dict)     # source_value -> target label
    unresolved: list[str] = field(default_factory=list)        # source values left for review
    rejected: list[dict] = field(default_factory=list)         # [{source, target, reason}]
    reason: str = ""

    def to_evidence(self) -> dict:
        return {"ok": self.ok, "target": self.target, "accepted": self.accepted,
                "unresolved": self.unresolved, "rejected": self.rejected, "reason": self.reason}


def validate_model_value_map(tf: TargetField, proposals: list[dict], observed_values: list[str],
                             *, already: dict[str, str] | None = None) -> ValidatedValueMap:
    """Deterministically validate a model-proposed value map against the target enum.

    ``proposals`` = [{"source_value", "target_value" (or None), "ambiguous" (bool), ...}].
    A proposal is ACCEPTED only when: the target value exists in the target enum, the source value
    was actually observed, and the model did not flag it ambiguous. Everything else is escalated;
    nothing unobserved is ever executed. The model can never emit an arbitrary target value.
    """
    already = already or {}
    if tf is None or tf.value_type not in ("enum", "multiselect", "boolean"):
        return ValidatedValueMap(ok=False, target=(tf.path if tf else ""),
                                 reason="target is not an enum/boolean field")
    allowed = set(tf.enum_values or ()) if tf.value_type != "boolean" else {"true", "false"}
    observed = {str(v).strip() for v in observed_values if str(v).strip()}
    accepted: dict[str, str] = dict(already)
    rejected: list[dict] = []
    seen_sources: set[str] = set()

    for p in proposals:
        src = str(p.get("source_value", "")).strip()
        tgt = p.get("target_value")
        if not src:
            continue
        seen_sources.add(src)
        if src in already:
            continue                      # deterministic Tier-1 mapping wins; never overridden by the model
        if src not in observed:
            rejected.append({"source": src, "target": tgt, "reason": "source value was not observed in the column"})
            continue
        if tgt is None or p.get("ambiguous"):
            continue                      # model declined / flagged ambiguous -> stays unresolved
        if tgt not in allowed:
            rejected.append({"source": src, "target": tgt,
                             "reason": f"target value '{tgt}' is not in the target enum {sorted(allowed)}"})
            continue
        accepted[src] = tgt

    unresolved = sorted(v for v in observed if v not in accepted)
    ok = not unresolved and not rejected
    reason = ("all observed values validated against the target enum" if ok else
              f"{len(unresolved)} unresolved, {len(rejected)} rejected proposal(s) — escalating")
    return ValidatedValueMap(ok=ok, target=tf.path, accepted=accepted, unresolved=unresolved,
                             rejected=rejected, reason=reason)
