"""Column profiling (profile.v2, M3C).

Turns the raw source records for one table into a rich per-column profile computed from the FULL
column: identity, counts/ratios, observed types, structural + semantic indicators, the COMPLETE
distinct value domain with frequencies when cardinality is bounded, and a small set of
INFORMATION-RICH representative samples.

The full column is used for statistics only, and ``samples``/``value_domain`` here are RAW — they
stay in-process for the deterministic engines (date inference, custom-field suggestion) and for the
consultant UI (which also has a source deep-link). What the external MODEL may see is NOT these raw
fields: it is the redacted/abstracted projection built in :mod:`app.model_projection`
(``to_model_safe_column``), where high-cardinality PII (names, emails, phones, ids, free text) is
replaced by class placeholders or masked shape patterns and only genuinely low-cardinality business
domains are sent as real labels. Never hand ``ColumnProfile.samples`` straight to a model/tracer.

profile.v2 adds statistics/flags/value_domain on top of the M1 profile; the M1 fields
(``format_indicators`` keys, ``samples``, counts) are preserved so existing callers keep working.
Profiles carry a stable, DETERMINISTIC ``profile_id`` per (table, column position) so re-running the
mapping stage re-uses the same rows/decisions instead of duplicating them.
"""
from __future__ import annotations

import hashlib
import json
import re

from pydantic import BaseModel, Field

from .source_records import SourceRecord, SourceTable

PROFILE_VERSION = "profile.v2"

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DATE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}")
_DATE_SLASH = re.compile(r"^\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}$")
_LEADING_ZERO = re.compile(r"^0\d+$")
_INT = re.compile(r"^-?\d+$")
_NUM = re.compile(r"^-?\d+(\.\d+)?$")
_PHONE = re.compile(r"^\+?[0-9][0-9 ()\-.]{5,24}[0-9]$")
_URL = re.compile(r"^(https?://|www\.)", re.IGNORECASE)
_BOOL = {"true", "false", "yes", "no", "y", "n", "1", "0", "t", "f"}


class ColumnProfile(BaseModel):
    profile_id: str
    table_id: str
    col_index: int
    header: str
    non_empty_count: int
    missing_count: int
    distinct_count: int
    observed_types: dict[str, int] = Field(default_factory=dict)
    format_indicators: dict[str, object] = Field(default_factory=dict)
    samples: list[str] = Field(default_factory=list)
    # profile.v2 additions (defaults keep M1/M2 constructors valid).
    profile_version: str = PROFILE_VERSION
    stats: dict[str, object] = Field(default_factory=dict)
    value_domain: list[dict] = Field(default_factory=list)   # [{"value","count"}] when low-cardinality


def profile_from_row(row: dict) -> ColumnProfile:
    """Reconstruct a :class:`ColumnProfile` from a persisted ``column_profiles`` DB row, including
    the profile.v2 ``stats`` / ``value_domain`` (from ``profile_ext``; absent on pre-M3C rows)."""
    ext = {}
    raw_ext = row.get("profile_ext")
    if raw_ext:
        try:
            ext = json.loads(raw_ext)
        except (ValueError, TypeError):
            ext = {}
    return ColumnProfile(
        profile_id=row["id"], table_id=row["table_id"], col_index=row["col_index"], header=row["header"],
        non_empty_count=row["non_empty_count"], missing_count=row["missing_count"],
        distinct_count=row["distinct_count"], observed_types=json.loads(row["observed_types"]),
        format_indicators=json.loads(row["format_indicators"]), samples=json.loads(row["samples"]),
        profile_version=ext.get("profile_version", "profile.v1"),
        stats=ext.get("stats", {}) or {}, value_domain=ext.get("value_domain", []) or [])


def _trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…"


def _looks_date_component_disambiguating(v: str) -> bool:
    """A numeric slash/dot/dash date with a component > 12 (disambiguates MDY vs DMY)."""
    m = re.match(r"^(\d{1,4})[/.\-](\d{1,2})[/.\-](\d{1,4})$", v)
    if not m:
        return False
    a, b = int(m.group(1)), int(m.group(2))
    return a > 12 or b > 12


def _representative_samples(counts: dict[str, int], *, max_samples: int, sample_max_chars: int) -> list[str]:
    """Discriminative representative values (not just the first five sorted distinct).

    Prefers: most common, rarest, shortest, longest, a date-disambiguating value, and a value per
    observed shape — then fills with sorted distinct. Bounded to ``max_samples`` and truncated.
    """
    if not counts:
        return []
    picks: list[str] = []

    def add(v: str | None) -> None:
        if v is not None and v not in picks:
            picks.append(v)

    by_freq_desc = sorted(counts, key=lambda k: (-counts[k], k))
    by_freq_asc = sorted(counts, key=lambda k: (counts[k], k))
    by_len = sorted(counts, key=lambda k: (len(k), k))
    add(by_freq_desc[0])                       # most common
    if len(by_freq_desc) > 1:
        add(by_freq_asc[0])                    # rarest
    add(by_len[0])                             # shortest
    add(by_len[-1])                            # longest
    for v in counts:                           # a date-disambiguating example, if any
        if _looks_date_component_disambiguating(v):
            add(v)
            break
    for v in sorted(counts):                   # fill deterministically
        if len(picks) >= max_samples:
            break
        add(v)
    return [_trunc(s, sample_max_chars) for s in picks[:max_samples]]


def profile_table(
    table: SourceTable,
    records: list[SourceRecord],
    *,
    max_samples: int = 5,
    sample_max_chars: int = 120,
    low_cardinality_max: int = 50,
) -> list[ColumnProfile]:
    profiles: list[ColumnProfile] = []
    total = len(records)
    for col_index, header in enumerate(table.headers):
        values: list[str] = []
        types: dict[str, int] = {}
        counts: dict[str, int] = {}
        for rec in records:
            if col_index >= len(rec.cells):
                continue
            cell = rec.cells[col_index]
            types[cell.raw_type.value] = types.get(cell.raw_type.value, 0) + 1
            if cell.value is not None and cell.value.strip() != "":
                v = cell.value
                values.append(v)
                counts[v] = counts.get(v, 0) + 1

        non_empty = len(values)
        distinct = sorted(counts)
        distinct_n = len(distinct)

        stripped = [v.strip() for v in values]
        email_hits = sum(1 for v in stripped if _EMAIL.match(v))
        iso_date_hits = sum(1 for v in stripped if _DATE_ISO.match(v))
        slash_date_hits = sum(1 for v in stripped if _DATE_SLASH.match(v))
        leading_zero_hits = sum(1 for v in stripped if _LEADING_ZERO.match(v))
        whitespace_hits = sum(1 for v in values if v != v.strip())
        int_hits = sum(1 for v in stripped if _INT.match(v))
        num_hits = sum(1 for v in stripped if _NUM.match(v))
        phone_hits = sum(1 for v in stripped if _PHONE.match(v))
        url_hits = sum(1 for v in stripped if _URL.match(v))
        numeric_vals = [float(v) for v in stripped if _NUM.match(v)]

        def ratio(x: int) -> float:
            return round(x / non_empty, 3) if non_empty else 0.0

        distinct_ratio = round(distinct_n / non_empty, 3) if non_empty else 0.0
        max_freq = max(counts.values()) if counts else 0
        duplicate_ratio = round((non_empty - distinct_n) / non_empty, 3) if non_empty else 0.0
        is_low_card = 0 < distinct_n <= low_cardinality_max and (distinct_n <= 12 or distinct_ratio <= 0.5)
        boolean_domain = 0 < distinct_n <= 3 and all(v.strip().lower() in _BOOL for v in stripped)

        indicators: dict[str, object] = {
            # --- M1 keys (preserved) ---
            "email_ratio": ratio(email_hits),
            "iso_date_ratio": ratio(iso_date_hits),
            "slash_date_ratio": ratio(slash_date_hits),
            "has_leading_zero_values": leading_zero_hits > 0,
            "has_untrimmed_whitespace": whitespace_hits > 0,
            "looks_date_like": (iso_date_hits + slash_date_hits) / non_empty > 0.5 if non_empty else False,
            "ambiguous_slash_dates": slash_date_hits > 0,
            # --- profile.v2 additions ---
            "numeric_ratio": ratio(num_hits),
            "integer_ratio": ratio(int_hits),
            "phone_like_ratio": ratio(phone_hits),
            "url_like_ratio": ratio(url_hits),
            "boolean_domain": boolean_domain,
            "distinct_ratio": distinct_ratio,
            "null_ratio": round((total - non_empty) / total, 3) if total else 0.0,
            "duplicate_ratio": duplicate_ratio,
            "constant": distinct_n <= 1 and non_empty > 0,
            "likely_identifier": distinct_n == non_empty and non_empty >= 2 and duplicate_ratio == 0.0,
            "likely_low_cardinality_category": is_low_card,
        }

        stats: dict[str, object] = {
            "profile_version": PROFILE_VERSION,
            "row_count": total,
            "non_empty_count": non_empty,
            "null_count": total - non_empty,
            "distinct_count": distinct_n,
            "min_len": min((len(v) for v in stripped), default=0),
            "max_len": max((len(v) for v in stripped), default=0),
            "numeric_count": num_hits,
            "integer_count": int_hits,
            "numeric_min": min(numeric_vals) if numeric_vals else None,
            "numeric_max": max(numeric_vals) if numeric_vals else None,
            "has_negative": any(v < 0 for v in numeric_vals),
            "max_value_frequency": max_freq,
            "all_lower": all(v == v.lower() for v in stripped) if stripped else False,
            "all_upper": all(v == v.upper() for v in stripped) if stripped else False,
        }

        # Complete distinct domain with frequencies, ONLY when the column is low-cardinality.
        value_domain: list[dict] = []
        if is_low_card:
            value_domain = [{"value": _trunc(v, sample_max_chars), "count": counts[v]}
                            for v in sorted(counts, key=lambda k: (-counts[k], k))]

        samples = _representative_samples(counts, max_samples=max_samples, sample_max_chars=sample_max_chars)

        profiles.append(
            ColumnProfile(
                profile_id=f"col_{hashlib.sha1(f'{table.table_id}|{col_index}'.encode()).hexdigest()[:12]}",
                table_id=table.table_id,
                col_index=col_index,
                header=header,
                non_empty_count=non_empty,
                missing_count=total - non_empty,
                distinct_count=distinct_n,
                observed_types=types,
                format_indicators=indicators,
                samples=samples,
                stats=stats,
                value_domain=value_domain,
            )
        )
    return profiles
