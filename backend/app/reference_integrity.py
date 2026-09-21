"""Referential-integrity proof for ID / foreign-key-like fields (M3C).

A header alias alone (``ManagerID`` -> ``manager_employee_id``) is NOT proof that a source value is
the target employee business key. Before treating an ID column as a reference, this module checks
the ACTUAL value domains: it compares non-null reference values against the source employee_id
domain (and, when available, existing target employee ids). Strong overlap accepts the reference;
zero/poor overlap DOWNGRADES it to review instead of silently treating arbitrary ids as employees.

It also supports deriving a manager employee id from an exact, UNIQUE name match between a
``ManagerName`` column and the employee-name column — never a fuzzy match. A duplicate or missing
name escalates instead of guessing.

Pure and deterministic. No model calls.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

REFERENCE_VERSION = "reference.v1"


def normalize_name(raw) -> str:
    """Exact-match normalization for people names: casefold + collapse internal whitespace.

    This is for EXACT equality only — it deliberately does NOT do any fuzzy/phonetic matching.
    """
    return re.sub(r"\s+", " ", str(raw or "").strip()).casefold()


@dataclass
class ReferenceCoverage:
    verdict: str                 # valid | poor | empty
    coverage: float
    matched: int
    unmatched: int
    non_null: int
    unmatched_samples: list[str] = field(default_factory=list)
    reason: str = ""

    def to_evidence(self) -> dict:
        return {"version": REFERENCE_VERSION, "verdict": self.verdict, "coverage": self.coverage,
                "matched": self.matched, "unmatched": self.unmatched, "non_null": self.non_null,
                "unmatched_examples": self.unmatched_samples, "reason": self.reason}


def validate_reference_domain(values: list, employee_id_domain: set[str], *,
                              target_id_domain: set[str] | None = None,
                              threshold: float = 0.9, max_samples: int = 8) -> ReferenceCoverage:
    """Validate reference values against the employee-key domain(s).

    ``employee_id_domain`` = the set of source employee_id business keys across the job.
    ``target_id_domain`` = existing target employee ids (optional). A value counts as matched when it
    is present in either domain (comparison is exact after trimming; leading zeros preserved).
    """
    domain = set(employee_id_domain) | set(target_id_domain or set())
    non_null = [str(v).strip() for v in values if v is not None and str(v).strip() != ""]
    if not non_null:
        return ReferenceCoverage("empty", 0.0, 0, 0, 0, reason="no non-null reference values")
    matched = [v for v in non_null if v in domain]
    unmatched = [v for v in non_null if v not in domain]
    coverage = round(len(matched) / len(non_null), 4)
    if coverage >= threshold:
        verdict = "valid"
        reason = f"{len(matched)}/{len(non_null)} reference values match the employee-key domain"
    else:
        verdict = "poor"
        reason = (f"only {len(matched)}/{len(non_null)} reference values match the employee-key "
                  f"domain; not treating this column as an employee reference without review")
    seen: list[str] = []
    for v in unmatched:
        if v not in seen:
            seen.append(v)
        if len(seen) >= max_samples:
            break
    return ReferenceCoverage(verdict, coverage, len(matched), len(unmatched), len(non_null),
                             unmatched_samples=seen, reason=reason)


@dataclass
class NameDerivation:
    status: str                  # derivable | not_derivable
    resolved: dict[str, str] = field(default_factory=dict)     # raw manager name -> employee_id
    ambiguous: list[str] = field(default_factory=list)         # names matching >1 employee
    unmatched: list[str] = field(default_factory=list)         # names matching 0 employees
    derived_count: int = 0
    reason: str = ""

    def to_evidence(self) -> dict:
        return {"version": REFERENCE_VERSION, "status": self.status,
                "resolved_examples": dict(list(self.resolved.items())[:20]),
                "ambiguous_examples": self.ambiguous[:20], "unmatched_examples": self.unmatched[:20],
                "derived_count": self.derived_count,
                "resolvable": len(self.resolved), "ambiguous_count": len(self.ambiguous),
                "unmatched_count": len(self.unmatched), "reason": self.reason}


def derive_reference_by_name(manager_names: list, name_to_ids: dict[str, set[str]],
                             *, max_samples: int = 20) -> NameDerivation:
    """Resolve manager names to employee ids by EXACT unique name match.

    ``name_to_ids`` maps a normalized employee name -> the set of employee_ids carrying that name.
    A manager name is derived ONLY when it resolves to exactly one employee id. Duplicate names
    (one name -> several ids) and unmatched names are escalated, never fuzzy-guessed.
    """
    resolved: dict[str, str] = {}
    ambiguous: list[str] = []
    unmatched: list[str] = []
    distinct_raw: dict[str, str] = {}
    for raw in manager_names:
        if raw is None or str(raw).strip() == "":
            continue
        norm = normalize_name(raw)
        distinct_raw.setdefault(norm, str(raw).strip())
    for norm, raw in distinct_raw.items():
        ids = name_to_ids.get(norm) or set()
        if len(ids) == 1:
            resolved[raw] = next(iter(ids))
        elif len(ids) > 1:
            if len(ambiguous) < max_samples:
                ambiguous.append(raw)
        else:
            if len(unmatched) < max_samples:
                unmatched.append(raw)
    status = "derivable" if resolved and not ambiguous and not unmatched else (
        "derivable" if resolved else "not_derivable")
    reason = (f"{len(resolved)} manager name(s) resolve to exactly one employee id"
              if resolved else "no manager name resolves to a unique employee id")
    if ambiguous or unmatched:
        reason += f"; {len(ambiguous)} ambiguous and {len(unmatched)} unmatched name(s) escalated"
    return NameDerivation(status=status, resolved=resolved, ambiguous=ambiguous, unmatched=unmatched,
                          derived_count=len(resolved), reason=reason)
