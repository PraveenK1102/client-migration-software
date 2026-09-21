"""Model-safe profile projection (M3D P0/P1).

The external model (Groq) — and any external tracer (LangSmith) — must NEVER receive raw,
high-cardinality employee PII: names, emails, phone numbers, employee IDs, addresses, free text.
Mapping a column to a target field is driven by the HEADER and by structural EVIDENCE (types,
ratios, cardinality, format shape), not by the literal personal values, so redacting the values is
essentially lossless for mapping quality while removing the leak.

Policy (precedence order), decided per column from its profile — never from a row:
  email / phone / url  -> a single class placeholder (`<EMAIL>` / `<PHONE>` / `<URL>`)
  date-like            -> masked FORMAT pattern(s) only (e.g. ``####-##-##``) — the deterministic
                          date engine inspects the real values locally; the model never needs them
  identifier / free text (high-cardinality) -> masked SHAPE pattern(s): every letter -> ``A``/``a``,
                          every digit -> ``#`` (no original character survives), so structure is
                          conveyed but no name/id/value content is
  low-cardinality categorical/enum (values genuinely repeat) -> the bounded DISTINCT domain of real
                          labels, because a business taxonomy ("Active"/"Terminated",
                          "Engineering"/"Sales") is what semantic mapping legitimately needs and is
                          detached from any employee identity. Individual values that themselves look
                          like PII are still masked, and the domain is bounded.

The same projection is used for (a) the mapping proposal request, (b) low-cardinality enum-transform
values, and (c) redacted evidence persisted in audit/metrics — so no code path re-introduces raw PII.
"""
from __future__ import annotations

from .llm.base import ProposalColumn
from .profiling import ColumnProfile

# A value at/above this ratio of matches is treated as that PII class for the whole column.
_PII_RATIO = 0.30
# Free text longer than this is never sent even masked beyond a bounded prefix.
_MASK_MAX_CHARS = 40
# Headers that identify or finance an individual: never send their raw values, even low-cardinality
# ones. Gender/marital/nationality are intentionally NOT here — those are business enums the mapper
# legitimately needs and are detached from identity; identifying/financial fields are not.
_SENSITIVE_HEADER = (
    "ssn", "social security", "aadhaar", "aadhar", "passport", "national id", "nationalid",
    "tax id", "taxid", "pan number", "pan_no", "bank", "account number", "account_no", "iban",
    "routing", "salary", "compensation", "ctc", "payslip", "credit card", "card number",
)


def _sensitive_header(header: str) -> bool:
    h = (header or "").lower()
    return any(tok in h for tok in _SENSITIVE_HEADER)


def _mask(value: str) -> str:
    """Replace every letter with A/a and every digit with #, keeping separators/spaces.

    No original alphanumeric character survives, so a masked token reveals character-class STRUCTURE
    (and thus date/id/text shape) while carrying none of the value's content — a masked name, email,
    id, address or injection string is inert.
    """
    out: list[str] = []
    for ch in value[:_MASK_MAX_CHARS]:
        if ch.isdigit():
            out.append("#")
        elif ch.isalpha():
            out.append("A" if ch.isupper() else "a")
        else:
            out.append(ch)
    masked = "".join(out)
    return masked + ("…" if len(value) > _MASK_MAX_CHARS else "")


def _looks_email(v: str) -> bool:
    return "@" in v and "." in v.split("@")[-1]


def _looks_phone(v: str) -> bool:
    digits = sum(c.isdigit() for c in v)
    return digits >= 6 and all(c.isdigit() or c in "+()-. " for c in v)


def redaction_class(profile: ColumnProfile) -> str:
    """The column's model-safe class: email | phone | url | date | identifier | enum | text."""
    ind = profile.format_indicators or {}

    def r(key: str) -> float:
        try:
            return float(ind.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    if r("email_ratio") >= _PII_RATIO:
        return "email"
    if r("url_like_ratio") >= _PII_RATIO:
        return "url"
    # Date BEFORE phone: an ISO/slash date (e.g. 1990-05-14) also matches a lenient phone shape, and
    # a date column is never a real phone column (a phone has no yyyy-mm-dd/slash-date structure).
    if ind.get("looks_date_like") or r("iso_date_ratio") >= _PII_RATIO or r("slash_date_ratio") >= _PII_RATIO:
        return "date"
    if r("phone_like_ratio") >= _PII_RATIO:
        return "phone"
    # Sensitive-header policy: an identifying/financial column's values are masked even when
    # low-cardinality (a "salary band" is still salary), so only the header + shape reach the model.
    if _sensitive_header(profile.header):
        return "text"
    # A genuine low-cardinality categorical: a real enum has a CLOSED, repeating vocabulary
    # (duplicate_ratio > 0), whereas an all-distinct textual column (names, ids) never repeats —
    # this discriminator is dataset-size independent, unlike distinct_count/ratio alone.
    if ind.get("boolean_domain"):
        return "enum"
    if ind.get("likely_low_cardinality_category") and r("duplicate_ratio") > 0.0:
        return "enum"
    if ind.get("likely_identifier"):
        # Distinguish a code-like identifier from a multi-word free-text column (names, addresses):
        # both are masked so this only sharpens the SAFE hint the model receives. Reading raw samples
        # here is local (redaction decision); nothing raw is emitted.
        sample = [s for s in (profile.samples or []) if s]
        multiword = sum(1 for s in sample if " " in s.strip())
        if sample and multiword / len(sample) > 0.5:
            return "text"
        return "identifier"
    return "text"


def _scalar(value: str) -> str:
    """Redact ONE value defensively even inside an otherwise-safe enum domain."""
    v = (value or "").strip()
    if _looks_email(v):
        return "<EMAIL>"
    if _looks_phone(v):
        return "<PHONE>"
    if len(v) > _MASK_MAX_CHARS:
        return _mask(v)
    return v


def model_safe_samples(profile: ColumnProfile, *, max_samples: int = 5) -> list[str]:
    """Redacted/abstracted representative values safe to hand to an external model.

    NEVER returns raw high-cardinality PII. For enums, returns the bounded distinct domain of real
    labels (each individually defended). For everything else, returns class placeholders or masked
    shape patterns.
    """
    cls = redaction_class(profile)
    if cls == "email":
        return ["<EMAIL>"]
    if cls == "phone":
        return ["<PHONE>"]
    if cls == "url":
        return ["<URL>"]
    if cls == "enum":
        # Real labels — bounded and individually defended (a stray email/phone is still masked).
        domain = [d.get("value", "") for d in (profile.value_domain or [])] or list(profile.samples or [])
        out: list[str] = []
        for v in domain:
            s = _scalar(v)
            if s not in out:
                out.append(s)
            if len(out) >= max_samples:
                break
        return out
    # date / identifier / text -> masked shape patterns (distinct patterns, bounded).
    patterns: list[str] = []
    for v in (profile.samples or []):
        p = _mask((v or "").strip())
        if p and p not in patterns:
            patterns.append(p)
        if len(patterns) >= max_samples:
            break
    return patterns


# The structural signals worth sending to the model for mapping. Zero/false values carry no signal
# and only cost tokens, so they are dropped (M3E: keep the request within the free-tier TPM budget).
_MODEL_INDICATOR_KEYS = (
    "email_ratio", "iso_date_ratio", "slash_date_ratio", "looks_date_like", "numeric_ratio",
    "integer_ratio", "phone_like_ratio", "url_like_ratio", "boolean_domain", "distinct_ratio",
    "duplicate_ratio", "constant", "likely_identifier", "likely_low_cardinality_category",
    "has_leading_zero_values",
)


def safe_indicators(profile: ColumnProfile) -> dict:
    """The INFORMATIVE structural signals the model may see, plus the redaction class and compact
    shape/cardinality summaries — nothing derived from a raw personal value. Only non-zero / true
    indicators are included; the many always-zero ratios are dropped so the request stays TPM-safe."""
    ind = profile.format_indicators or {}
    stats = profile.stats or {}
    out: dict = {"redaction_class": redaction_class(profile),
                 "distinct_count": profile.distinct_count,
                 "non_empty_count": profile.non_empty_count}
    if "min_len" in stats:
        out["min_len"] = stats["min_len"]
    if "max_len" in stats:
        out["max_len"] = stats["max_len"]
    for k in _MODEL_INDICATOR_KEYS:
        v = ind.get(k)
        if isinstance(v, bool):
            if v:
                out[k] = True
        elif isinstance(v, (int, float)) and v:
            out[k] = v
    return out


def to_model_safe_column(profile: ColumnProfile, *, max_samples: int = 5) -> ProposalColumn:
    """Build a :class:`ProposalColumn` carrying ONLY model-safe content: the header (a field name,
    not a person's data), observed types, safe indicators/shape, and redacted/abstracted samples."""
    return ProposalColumn(
        source_column_id=profile.profile_id,
        header=profile.header,
        observed_types=profile.observed_types,
        format_indicators=safe_indicators(profile),
        samples=model_safe_samples(profile, max_samples=max_samples),
    )


def redact_values(profile: ColumnProfile, values: list[str], *, max_values: int = 10) -> list[str]:
    """Redact a bounded list of already-distinct observed values for PII-safe audit/metric evidence.

    Enum domains stay human-readable (labels a reviewer needs); high-cardinality PII becomes class
    placeholders / masked shapes. Used so raw sample PII is not persisted merely for convenience.
    """
    cls = redaction_class(profile)
    if cls in ("email", "phone", "url"):
        token = {"email": "<EMAIL>", "phone": "<PHONE>", "url": "<URL>"}[cls]
        return [token]
    if cls == "enum":
        out: list[str] = []
        for v in values[:max_values]:
            s = _scalar(v)
            if s not in out:
                out.append(s)
        return out
    seen: list[str] = []
    for v in values[:max_values]:
        p = _mask((v or "").strip())
        if p and p not in seen:
            seen.append(p)
    return seen
