"""Deterministic field validators and normalizers (no LLM, no network).

Design rules (Order 02 r2, sections 1.3 & 4):
- Regex is for *lexical shape* only, via compiled ``fullmatch`` with ASCII [0-9]
  classes and bounded input; it never decides a field's business role.
- Email uses the maintained ``email_validator`` (check_deliverability=False), not a
  permissive regex, as the final gate. A shape regex is profile evidence only.
- Dates: strict ISO + a small list of unambiguous explicit formats; impossible
  calendar dates are rejected; an ambiguous numeric date (e.g. 03/04/2024) stays
  UNRESOLVED with both candidates unless a convention is declared/confirmed.
- Identity/name strings are preserved verbatim; nonempty is the only contract.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

# --- compiled lexical shapes (fullmatch, ASCII digits, bounded) -----------
ISO_DATE_SHAPE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
ISO_DATETIME_SHAPE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2})?(\.[0-9]+)?")
NUMERIC_DATE_SHAPE = re.compile(r"[0-9]{1,2}[/.][0-9]{1,2}[/.][0-9]{4}")
EMAIL_SHAPE = re.compile(r"[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}")  # PROFILE evidence only
_MAX = 320  # bound field length before regex evaluation


def _bounded(s: str) -> str:
    return s if len(s) <= _MAX else s[:_MAX]


def looks_like_iso_date(value: str) -> bool:
    v = _bounded(value.strip())
    return bool(ISO_DATE_SHAPE.fullmatch(v) or ISO_DATETIME_SHAPE.fullmatch(v))


def looks_like_email(value: str) -> bool:
    """Cheap shape check for PROFILING only — never the eligibility gate."""
    return bool(EMAIL_SHAPE.fullmatch(_bounded(value.strip())))


# --- Email ---------------------------------------------------------------
@dataclass
class EmailResult:
    status: str            # "valid" | "invalid"
    normalized: str | None # display/comparison form (domain lowercased, local part preserved)
    comparison_key: str | None
    reason: str | None = None


def validate_work_email(raw: str) -> EmailResult:
    from email_validator import EmailNotValidError, validate_email

    trimmed = (raw or "").strip()
    if not trimmed:
        return EmailResult("invalid", None, None, "empty")
    try:
        v = validate_email(_bounded(trimmed), check_deliverability=False)
    except EmailNotValidError as e:
        return EmailResult("invalid", None, None, f"syntax: {e}")
    normalized = v.normalized  # RFC-normalized (domain lowercased, IDNA); local part preserved
    return EmailResult("valid", normalized, normalized.lower(), None)


# --- Department ----------------------------------------------------------
def canonicalize_department(raw: str, enum_values: list[str]) -> tuple[str | None, str]:
    """Match a department to a canonical enum label ignoring outer ws/case.

    Returns (canonical_label|None, status): "canonical" | "unknown" | "empty".
    Never guesses abbreviations (eng/HR); unknown stays unknown for human handling.
    """
    trimmed = (raw or "").strip()
    if not trimmed:
        return None, "empty"
    folded = trimmed.casefold()
    for label in enum_values:
        if label.casefold() == folded:
            return label, "canonical"
    return None, "unknown"


# --- Dates ---------------------------------------------------------------
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}
_MONTHS_FULL = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], start=1)}
_MONTH_NAME = re.compile(r"[0-9]{1,2}[ -]([A-Za-z]{3,9})[ -][0-9]{4}")
_MONTH_NAME_2 = re.compile(r"([A-Za-z]{3,9})[ ]([0-9]{1,2}),?[ ]([0-9]{4})")


@dataclass
class DateResult:
    status: str                  # "valid" | "ambiguous" | "invalid" | "empty"
    iso: str | None = None       # set when status == valid
    candidates: list[dict] = field(default_factory=list)  # [{iso, meaning}] when ambiguous
    reason: str | None = None


def _valid_ymd(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def interpret_date(raw: str, *, convention: str | None = None) -> DateResult:
    """Interpret a raw date value. ``convention`` in {"DMY","MDY"} resolves ambiguity
    for numeric d/m/y forms only (a declared/human-confirmed source convention)."""
    s = (raw or "").strip()
    if not s:
        return DateResult("empty", reason="empty")
    s = _bounded(s)

    # Strict ISO date, or ISO datetime at midnight (Excel date cells arrive as ISO).
    if ISO_DATE_SHAPE.fullmatch(s):
        iso = _valid_ymd(int(s[0:4]), int(s[5:7]), int(s[8:10]))
        return DateResult("valid", iso) if iso else DateResult("invalid", reason="impossible calendar date")
    if ISO_DATETIME_SHAPE.fullmatch(s):
        datepart = s[:10]
        iso = _valid_ymd(int(datepart[0:4]), int(datepart[5:7]), int(datepart[8:10]))
        if not iso:
            return DateResult("invalid", reason="impossible calendar date")
        if s[10:].strip() not in ("", "T00:00:00", " 00:00:00", "T00:00:00.000000"):
            return DateResult("invalid", reason="datetime carries a time-of-day; unsupported without a policy")
        return DateResult("valid", iso)

    # English month-name forms (unambiguous).
    m = _MONTH_NAME.fullmatch(s)
    if m:
        mon = _MONTHS.get(m.group(1).lower()) or _MONTHS_FULL.get(m.group(1).lower())
        nums = re.findall(r"[0-9]+", s)
        if mon and len(nums) == 2:
            iso = _valid_ymd(int(nums[1]), mon, int(nums[0]))
            return DateResult("valid", iso) if iso else DateResult("invalid", reason="impossible calendar date")
    m2 = _MONTH_NAME_2.fullmatch(s)
    if m2:
        mon = _MONTHS.get(m2.group(1).lower()) or _MONTHS_FULL.get(m2.group(1).lower())
        if mon:
            iso = _valid_ymd(int(m2.group(3)), mon, int(m2.group(2)))
            return DateResult("valid", iso) if iso else DateResult("invalid", reason="impossible calendar date")

    # Numeric d/m/y with 4-digit year: decide DMY vs MDY.
    if NUMERIC_DATE_SHAPE.fullmatch(s):
        a, b, y = (int(x) for x in re.split(r"[/.]", s))
        dmy = _valid_ymd(y, b, a)   # a=day, b=month
        mdy = _valid_ymd(y, a, b)   # a=month, b=day
        if convention == "DMY":
            return DateResult("valid", dmy) if dmy else DateResult("invalid", reason="impossible under DMY")
        if convention == "MDY":
            return DateResult("valid", mdy) if mdy else DateResult("invalid", reason="impossible under MDY")
        if dmy and mdy:
            if dmy == mdy:
                return DateResult("valid", dmy)  # identical -> no conflict
            return DateResult("ambiguous", candidates=[
                {"iso": dmy, "meaning": "DMY"}, {"iso": mdy, "meaning": "MDY"}],
                reason="ambiguous numeric date (DMY vs MDY); declare a convention or resolve")
        if dmy:
            return DateResult("valid", dmy)
        if mdy:
            return DateResult("valid", mdy)
        return DateResult("invalid", reason="impossible calendar date")

    return DateResult("invalid", reason="unsupported date format")


# --- Phone (shape only; value preserved) ---------------------------------
PHONE_SHAPE = re.compile(r"\+?[0-9][0-9 ()\-.]{5,24}[0-9]")


@dataclass
class PhoneResult:
    status: str                 # "valid" | "invalid" | "empty"
    normalized: str | None = None
    reason: str | None = None


def validate_phone(raw: str) -> PhoneResult:
    """Lexical-shape check only: optional leading '+', 7–15 digits, common separators. The value
    is preserved verbatim after trimming (no country inference, no reformatting)."""
    s = (raw or "").strip()
    if not s:
        return PhoneResult("empty", reason="empty")
    s = _bounded(s)
    digits = re.sub(r"[^0-9]", "", s)
    if not PHONE_SHAPE.fullmatch(s) or not (7 <= len(digits) <= 15):
        return PhoneResult("invalid", reason="does not look like a phone number (7–15 digits, optional +)")
    return PhoneResult("valid", normalized=s)


# --- Numbers / booleans (custom fields, collection items) -----------------
NUMBER_SHAPE = re.compile(r"-?[0-9]{1,15}(\.[0-9]{1,6})?")
_TRUE = {"true", "yes", "y", "1"}
_FALSE = {"false", "no", "n", "0"}


def is_number_like(value: str) -> bool:
    return bool(NUMBER_SHAPE.fullmatch(_bounded((value or "").strip())))


def is_boolean_like(value: str) -> bool:
    v = (value or "").strip().lower()
    return v in _TRUE or v in _FALSE


def normalize_number(raw: str) -> tuple[str | None, str]:
    """Return (canonical numeric string | None, status). Integers keep their digits ("30"); a
    decimal keeps its literal form. Never coerces identifiers (callers pick the field type)."""
    s = (raw or "").strip()
    if not s:
        return None, "empty"
    s = _bounded(s)
    if not is_number_like(s):
        return None, "invalid"
    if "." in s:
        return s, "valid"
    return str(int(s)), "valid"


def normalize_boolean(raw: str) -> tuple[str | None, str]:
    v = (raw or "").strip().lower()
    if not v:
        return None, "empty"
    if v in _TRUE:
        return "true", "valid"
    if v in _FALSE:
        return "false", "valid"
    return None, "invalid"


def canonicalize_enum(raw: str, enum_values: list[str]) -> tuple[str | None, str]:
    """Generic enum canonicalization (outer whitespace/case-insensitive; also treats '_'/' '/'-'
    as equivalent so 'Full Time' matches 'full_time'). Never guesses abbreviations."""
    trimmed = (raw or "").strip()
    if not trimmed:
        return None, "empty"
    fold = re.sub(r"[\s_\-]+", " ", trimmed.casefold())
    for label in enum_values:
        if re.sub(r"[\s_\-]+", " ", label.casefold()) == fold:
            return label, "canonical"
    return None, "unknown"


def split_multi_value(raw: str, delimiters: tuple[str, ...] = (";", "|", ",")) -> list[str]:
    """Split a delimited multi-value cell on the FIRST delimiter that actually occurs; items trimmed,
    empties dropped, order preserved, exact duplicates removed."""
    s = (raw or "").strip()
    if not s:
        return []
    for d in delimiters:
        if d in s:
            parts = [p.strip() for p in s.split(d)]
            break
    else:
        parts = [s]
    out: list[str] = []
    for p in parts:
        if p and p not in out:
            out.append(p)
    return out


# --- Identity / strings --------------------------------------------------
def is_nonempty_string(value) -> bool:
    return isinstance(value, str) and value.strip() != ""


def trim_preserving(value: str) -> str:
    """Trim outer whitespace only; internal content preserved verbatim."""
    return value.strip() if isinstance(value, str) else value
