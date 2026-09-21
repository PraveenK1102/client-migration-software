"""M3E policy behavioural tests (order §7): allow real generalization without blind trust.

For an UNSEEN header (not a deterministic alias) the deterministic policy must:
  * AUTO-ACCEPT when the header is semantically clear, has compatible structural evidence, and has no
    competing same-type role;
  * REVIEW when the evidence cannot disambiguate an important alternative (generic date role, no
    concept support, model-flagged ambiguity), regardless of the model's self-reported confidence.
And it must RECORD the target-specific structural evidence it accepted WITH (never confidence alone).
"""
from __future__ import annotations

from app.llm.schema import ProposalItem
from app.policy import Decision, classify_proposal
from app.profiling import ColumnProfile
from app.schema_loader import get_target_schema

SCHEMA = get_target_schema()


def _profile(header, indicators, *, non_empty=50, distinct=50, observed=None):
    return ColumnProfile(profile_id="c1", table_id="t", col_index=0, header=header,
                         non_empty_count=non_empty, missing_count=0, distinct_count=distinct,
                         observed_types=observed or {"text": non_empty}, format_indicators=indicators,
                         samples=[])


def _item(header, target, *, ambiguous=False, alts=None, conf=0.9):
    return ProposalItem(source_column_id="c1", source_header=header, proposed_target_field=target,
                        alternative_target_fields=alts or [], is_ambiguous=ambiguous,
                        ambiguity_reason="two plausible targets" if ambiguous else None,
                        evidence=[f"header {header!r}"], confidence=conf)


# --------------------------------------------------------------- unseen but defensible -> AUTO
def test_unseen_corporate_email_auto_accepts_with_email_evidence():
    prof = _profile("Corporate Email", {"email_ratio": 1.0, "redaction_class": "email"})
    r = classify_proposal(_item("Corporate Email", "work_email"), prof, SCHEMA)
    assert r.decision is Decision.AUTO_ACCEPT, r.reason
    assert r.evidence_summary["structural_evidence"]["email_ratio"] == 1.0


def test_unseen_employee_reference_auto_accepts_with_identifier_evidence():
    prof = _profile("Employee Reference",
                    {"likely_identifier": True, "has_leading_zero_values": False, "redaction_class": "identifier"},
                    distinct=50)
    r = classify_proposal(_item("Employee Reference", "employee_id"), prof, SCHEMA)
    assert r.decision is Decision.AUTO_ACCEPT, r.reason
    assert r.evidence_summary["structural_evidence"]["likely_identifier"] is True


def test_unseen_joining_effective_date_auto_accepts_distinguished_from_contract():
    prof = _profile("Joining Effective Date",
                    {"looks_date_like": True, "iso_date_ratio": 1.0, "redaction_class": "date"})
    r = classify_proposal(_item("Joining Effective Date", "hire_date"), prof, SCHEMA)
    assert r.decision is Decision.AUTO_ACCEPT, r.reason      # 'joining' keyword distinguishes hire_date
    assert "structural_evidence" in r.evidence_summary


def test_unseen_org_function_auto_accepts_to_department():
    prof = _profile("Org Function",
                    {"likely_low_cardinality_category": True, "duplicate_ratio": 0.9, "redaction_class": "enum"},
                    distinct=3)
    r = classify_proposal(_item("Org Function", "department"), prof, SCHEMA)
    assert r.decision is Decision.AUTO_ACCEPT, r.reason      # column maps; VALUE taxonomy reviewed later


# --------------------------------------------------------------- unseen but ambiguous -> REVIEW
def test_generic_effective_date_reviews_on_date_role_ambiguity():
    prof = _profile("Effective Date", {"looks_date_like": True, "iso_date_ratio": 1.0})
    r = classify_proposal(_item("Effective Date", "hire_date", conf=0.99), prof, SCHEMA)
    assert r.decision is Decision.NEEDS_REVIEW               # no hire/contract keyword -> can't decide role
    assert set(r.candidate_target_fields) == set(SCHEMA.date_role_group)


def test_no_concept_support_reviews_even_when_confident():
    # 'Sparkle Index' shares no concept token with grade; high confidence must NOT auto-accept.
    prof = _profile("Sparkle Index", {"numeric_ratio": 1.0}, observed={"number": 50})
    r = classify_proposal(_item("Sparkle Index", "grade", conf=0.99), prof, SCHEMA)
    assert r.decision is Decision.NEEDS_REVIEW
    assert "structural_evidence" in r.evidence_summary


def test_model_flagged_ambiguity_reviews():
    prof = _profile("Contact", {"email_ratio": 1.0})
    r = classify_proposal(_item("Contact", "work_email", ambiguous=True,
                                alts=["personal_email"]), prof, SCHEMA)
    assert r.decision is Decision.NEEDS_REVIEW


def test_competing_email_alternative_reviews():
    # 'Corporate Email' -> work_email but the model also lists personal_email -> escalate, don't guess.
    prof = _profile("Corporate Email", {"email_ratio": 1.0})
    r = classify_proposal(_item("Corporate Email", "work_email", alts=["personal_email"]), prof, SCHEMA)
    assert r.decision is Decision.NEEDS_REVIEW
    assert "personal_email" in r.candidate_target_fields


def test_confidence_alone_never_gates_autoaccept():
    """A perfectly-confident proposal with NO structural/semantic support is still reviewed."""
    prof = _profile("Xyzzy", {"redaction_class": "text"})
    low = classify_proposal(_item("Xyzzy", "grade", conf=1.0), prof, SCHEMA)
    assert low.decision is Decision.NEEDS_REVIEW
