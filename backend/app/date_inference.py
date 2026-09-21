"""Full-column date-format inference + two-digit-year century resolution (M3C).

A careful human does not decide a numeric date's convention from a single ambiguous cell — they
read the WHOLE column. ``11/30/2025`` proves MDY (month 30 is impossible under DMY); ``30/11/2025``
proves DMY. This module reasons the same way across the complete column:

    for a numeric A/B/Y shape, count DECISIVE evidence (one reading is calendar-impossible),
    AMBIGUOUS values (both readings valid and different), and INVALID values, then:
      - >=1 decisive MDY and 0 decisive DMY  -> infer MDY for the column
      - >=1 decisive DMY and 0 decisive MDY  -> infer DMY for the column
      - decisive evidence for BOTH            -> mixed_format (never choose one globally)
      - no decisive evidence but ambiguity    -> one scoped review establishes the convention

DATE ORDER and YEAR CENTURY are treated as SEPARATE inference problems. ``11/30/25`` proves MDY but
says nothing about whether ``25`` is 1925 or 2025. A two-digit year is resolved ONLY by
schema-driven temporal constraints (see ``schemas/employee.v2.yaml`` business_rules) when exactly
one century candidate survives — never by a hidden library pivot. If more than one survives, ONE
column-level review establishes a pivot.

Pure and deterministic. No model calls. The full column is used for statistics only; nothing here
is ever sent to a model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

DATE_INFERENCE_VERSION = "date_infer.v1"

# A/B/C numeric date with uniform single separator (/ . -). Parts keep their literal digit length.
_NUMERIC_DATE = re.compile(r"^(\d{1,4})([/.\-])(\d{1,2})\2(\d{1,4})$")
# ISO / month-name shapes we treat as order-independent (delegated to validators.interpret_date).
_ISO_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_MONTH_NAME_LIKE = re.compile(r"[A-Za-z]{3,}")


@dataclass
class DateFormatInference:
    status: str                 # inferred | mixed_format | ambiguous_needs_review | single_format | not_date
    order: str | None           # MDY | DMY | YMD | None
    candidates_considered: list[str]
    decisive_mdy: int = 0
    decisive_dmy: int = 0
    ambiguous: int = 0
    invalid: int = 0
    ymd_count: int = 0
    order_invariant: int = 0    # a==b values (identical under MDY and DMY)
    two_digit_year: bool = False
    parseable: int = 0
    total_non_empty: int = 0
    decisive_samples: list[str] = field(default_factory=list)
    reason: str = ""

    def to_evidence(self) -> dict:
        return {
            "version": DATE_INFERENCE_VERSION, "status": self.status, "inferred_order": self.order,
            "candidates_considered": self.candidates_considered,
            "decisive_mdy": self.decisive_mdy, "decisive_dmy": self.decisive_dmy,
            "ambiguous": self.ambiguous, "invalid": self.invalid, "ymd_count": self.ymd_count,
            "order_invariant": self.order_invariant, "two_digit_year": self.two_digit_year,
            "parseable": self.parseable, "total_non_empty": self.total_non_empty,
            "decisive_examples": self.decisive_samples, "reason": self.reason,
        }


def _valid_ymd(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def _split(value: str) -> tuple[int, int, int, int, bool] | None:
    """Return (a, b, y, year_digits, year_first) for a numeric date, else None.

    year_first is True for a 4-digit-leading YMD shape. Otherwise the year is the last part and
    a/b are the first two components (their day/month roles are what inference decides)."""
    m = _NUMERIC_DATE.match(value.strip())
    if not m:
        return None
    p0, _, p1, p2 = m.group(1), m.group(2), m.group(3), m.group(4)
    if len(p0) == 4 and len(p2) <= 2:           # YYYY/M/D -> year first (YMD)
        return (int(p1), int(p2), int(p0), 4, True)
    if len(p2) in (2, 4):                        # A/B/YY or A/B/YYYY -> year last
        return (int(p0), int(p1), int(p2), len(p2), False)
    return None


def _month_day_possible(month: int, day: int) -> bool:
    """Order-decisiveness check independent of century: month 1..12 and day 1..31."""
    return 1 <= month <= 12 and 1 <= day <= 31


def infer_date_format(values: list[str], *, max_samples: int = 8) -> DateFormatInference:
    """Infer the numeric date ORDER for a whole column from decisive full-column evidence."""
    non_empty = [str(v).strip() for v in values if v is not None and str(v).strip() != ""]
    inf = DateFormatInference(status="not_date", order=None,
                              candidates_considered=["MDY", "DMY", "YMD"],
                              total_non_empty=len(non_empty))
    if not non_empty:
        inf.reason = "no non-empty values"
        return inf

    numeric_seen = 0
    for v in non_empty:
        parts = _split(v)
        if parts is None:
            continue
        a, b, y, ydigits, year_first = parts
        numeric_seen += 1
        if year_first:
            inf.ymd_count += 1
            if _valid_ymd(y, a, b) is None:
                inf.invalid += 1
            continue
        if ydigits == 2:
            inf.two_digit_year = True
            can_mdy = _month_day_possible(a, b)     # month=a, day=b
            can_dmy = _month_day_possible(b, a)     # month=b, day=a
        else:                                        # 4-digit year -> exact calendar validity
            can_mdy = _valid_ymd(y, a, b) is not None
            can_dmy = _valid_ymd(y, b, a) is not None
        if can_mdy and not can_dmy:
            inf.decisive_mdy += 1
            if len(inf.decisive_samples) < max_samples:
                inf.decisive_samples.append(v)
        elif can_dmy and not can_mdy:
            inf.decisive_dmy += 1
            if len(inf.decisive_samples) < max_samples:
                inf.decisive_samples.append(v)
        elif can_mdy and can_dmy:
            if a == b:
                inf.order_invariant += 1
            else:
                inf.ambiguous += 1
        else:
            inf.invalid += 1

    if numeric_seen == 0:
        inf.reason = "no numeric slash/dot/dash dates in column"
        return inf

    # Pure YMD column (no A/B-year-last values).
    ab_seen = inf.decisive_mdy + inf.decisive_dmy + inf.ambiguous + inf.order_invariant + \
        (inf.invalid if not inf.ymd_count else max(0, inf.invalid))
    ab_signal = inf.decisive_mdy + inf.decisive_dmy + inf.ambiguous + inf.order_invariant
    if ab_signal == 0 and inf.ymd_count > 0:
        inf.order = "YMD"
        inf.status = "single_format"
        inf.parseable = inf.ymd_count - inf.invalid
        inf.reason = "all values are ISO-style year-first (YMD)"
        return inf

    if inf.decisive_mdy > 0 and inf.decisive_dmy > 0:
        inf.status = "mixed_format"
        inf.order = None
        inf.reason = (f"column has decisive evidence for BOTH MDY ({inf.decisive_mdy}) and DMY "
                      f"({inf.decisive_dmy}); no single convention is safe")
        return inf
    if inf.decisive_mdy > 0:
        inf.order = "MDY"
        inf.status = "inferred"
        inf.parseable = inf.decisive_mdy + inf.ambiguous + inf.order_invariant + inf.ymd_count
        inf.reason = (f"{inf.decisive_mdy} value(s) make DMY impossible, {inf.ambiguous} individually "
                      f"ambiguous, 0 contradict MDY")
        return inf
    if inf.decisive_dmy > 0:
        inf.order = "DMY"
        inf.status = "inferred"
        inf.parseable = inf.decisive_dmy + inf.ambiguous + inf.order_invariant + inf.ymd_count
        inf.reason = (f"{inf.decisive_dmy} value(s) make MDY impossible, {inf.ambiguous} individually "
                      f"ambiguous, 0 contradict DMY")
        return inf
    # No decisive evidence.
    if inf.ambiguous > 0:
        inf.status = "ambiguous_needs_review"
        inf.order = None
        inf.reason = (f"{inf.ambiguous} value(s) are individually ambiguous and no value is decisive; "
                      f"one scoped review must confirm MDY or DMY")
        return inf
    # Only order-invariant (a==b) and/or YMD and/or invalid values: order does not matter.
    inf.order = "MDY"
    inf.status = "single_format"
    inf.parseable = inf.order_invariant + inf.ymd_count
    inf.reason = "all parseable values are identical under MDY and DMY (order-invariant)"
    return inf


# --------------------------------------------------------------------------------------------
# Two-digit-year century resolution (schema-driven; separate from order inference).
# --------------------------------------------------------------------------------------------
@dataclass
class DateApplied:
    status: str                 # valid | ambiguous_century | invalid | missing
    iso: str | None = None
    reason: str | None = None
    century_candidates: list[str] = field(default_factory=list)


def _satisfies(iso: str, constraints: dict, reference_date: date) -> bool:
    d = date.fromisoformat(iso)
    ref_year = reference_date.year
    if constraints.get("not_future") or constraints.get("allow_future") is False:
        if d > reference_date:
            return False
    min_year = constraints.get("min_year")
    if isinstance(min_year, int) and d.year < min_year:
        return False
    max_age = constraints.get("max_age_years")
    if isinstance(max_age, int) and d.year < ref_year - max_age:
        return False
    min_age = constraints.get("min_age_years")
    if isinstance(min_age, int) and d.year > ref_year - min_age:
        return False
    return True


def apply_date_value(raw, *, order: str | None, constraints: dict | None = None,
                     reference_date: date | None = None, pivot: int | None = None) -> DateApplied:
    """Deterministically parse ONE raw value under an inferred column ``order``.

    Handles 4-digit and 2-digit years. For a 2-digit year, plausible centuries are constrained by
    the field's temporal constraints; resolves only when exactly one candidate survives (or a
    human-confirmed ``pivot`` decides it). Never uses a hidden default two-digit pivot.
    """
    constraints = constraints or {}
    reference_date = reference_date or date.today()
    if raw is None or str(raw).strip() == "":
        return DateApplied("missing")
    s = str(raw).strip()

    # Order-independent shapes: reuse the strict validators (ISO, ISO datetime, month names).
    if _ISO_LIKE.match(s) or _MONTH_NAME_LIKE.search(s):
        from .validators import interpret_date
        r = interpret_date(s)
        if r.status == "valid":
            return DateApplied("valid", iso=r.iso)
        if r.status == "invalid":
            return DateApplied("invalid", reason=r.reason)
        # ambiguous ISO-like should not happen; fall through

    parts = _split(s)
    if parts is None:
        return DateApplied("invalid", reason="unsupported date format")
    a, b, y, ydigits, year_first = parts

    if year_first:
        iso = _valid_ymd(y, a, b)
        return DateApplied("valid", iso=iso) if iso else DateApplied("invalid", reason="impossible calendar date")

    if order == "MDY":
        month, day = a, b
    elif order == "DMY":
        day, month = a, b
    else:
        return DateApplied("invalid", reason="no column date order to apply")

    if ydigits == 4:
        iso = _valid_ymd(y, month, day)
        return DateApplied("valid", iso=iso) if iso else DateApplied("invalid", reason="impossible calendar date")

    # Two-digit year: resolve century.
    yy = y
    if pivot is not None:
        century_year = 2000 + yy if yy <= pivot else 1900 + yy
        iso = _valid_ymd(century_year, month, day)
        return DateApplied("valid", iso=iso) if iso else DateApplied("invalid", reason="impossible under pivot")
    survivors = []
    for base in (2000, 1900):
        iso = _valid_ymd(base + yy, month, day)
        if iso and _satisfies(iso, constraints, reference_date):
            survivors.append(iso)
    if len(survivors) == 1:
        return DateApplied("valid", iso=survivors[0])
    if len(survivors) >= 2:
        return DateApplied("ambiguous_century", century_candidates=sorted(survivors),
                           reason="two-digit year has more than one plausible century under the schema constraints")
    return DateApplied("invalid", reason="two-digit year satisfies no plausible century under the schema constraints")


@dataclass
class CenturyResolution:
    status: str                 # not_needed | resolved | needs_review
    two_digit_values: int = 0
    resolved_values: int = 0
    ambiguous_values: int = 0
    invalid_values: int = 0
    ambiguous_samples: list[str] = field(default_factory=list)
    reason: str = ""

    def to_evidence(self) -> dict:
        return {"status": self.status, "two_digit_values": self.two_digit_values,
                "resolved_values": self.resolved_values, "ambiguous_values": self.ambiguous_values,
                "invalid_values": self.invalid_values, "ambiguous_examples": self.ambiguous_samples,
                "reason": self.reason}


def resolve_column_century(values: list[str], *, order: str | None, constraints: dict | None = None,
                           reference_date: date | None = None, max_samples: int = 8) -> CenturyResolution:
    """Whether a column's two-digit years all resolve to a unique century under the constraints.

    resolved   -> every two-digit-year value has exactly one surviving century (auto-apply).
    needs_review-> at least one value has >1 surviving century (a column pivot must be confirmed).
    """
    res = CenturyResolution(status="not_needed")
    for v in values:
        if v is None or str(v).strip() == "":
            continue
        parts = _split(str(v))
        if parts is None or parts[4] or parts[3] != 2:
            continue
        res.two_digit_values += 1
        applied = apply_date_value(v, order=order, constraints=constraints, reference_date=reference_date)
        if applied.status == "valid":
            res.resolved_values += 1
        elif applied.status == "ambiguous_century":
            res.ambiguous_values += 1
            if len(res.ambiguous_samples) < max_samples:
                res.ambiguous_samples.append(str(v).strip())
        else:
            res.invalid_values += 1
    if res.two_digit_values == 0:
        res.reason = "no two-digit-year values"
        return res
    if res.ambiguous_values > 0:
        res.status = "needs_review"
        res.reason = (f"{res.ambiguous_values} two-digit-year value(s) have more than one plausible "
                      f"century; a column-level pivot must be confirmed")
    else:
        res.status = "resolved"
        res.reason = (f"all {res.two_digit_values} two-digit-year value(s) resolve to a unique century "
                      f"under the schema temporal constraints")
    return res
