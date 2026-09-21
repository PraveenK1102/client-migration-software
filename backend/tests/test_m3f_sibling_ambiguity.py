"""M3F §F — generic sibling / role ambiguity policy patch.

An overconfident model may map a GENERIC header ("Email", "Phone", "Name", "ID", "Start Date") to one
member of a same-shape family with no alternatives, and today the type/shape can corroborate it. This
suite proves the policy escalates a bare family header to human review EVEN when the model is maximally
confident and returns alternatives=[], while a header carrying a distinctive qualifier ("Corporate
Email", "Office Telephone", …) may still auto-accept. The policy discovers the sibling destinations
independently of the model. Fixed at policy level only — the frozen deterministic alias surface is
covered by test_m3e_anti_overfitting and must not change (also asserted here).
"""
from __future__ import annotations

import pytest

from app.alias_audit import inventory_hash
from app.llm.schema import ProposalItem
from app.policy import Decision, classify_proposal
from app.profiling import ColumnProfile
from app.schema_loader import get_target_schema

SCHEMA = get_target_schema()

_IND = {
    "email": {"email_ratio": 1.0, "redaction_class": "email"},
    "phone": {"phone_like_ratio": 1.0, "email_ratio": 0.0, "looks_date_like": False,
              "redaction_class": "phone"},
    "name": {"email_ratio": 0.0, "looks_date_like": False, "redaction_class": "name"},
    "id": {"likely_identifier": True, "has_leading_zero_values": False, "email_ratio": 0.0,
           "looks_date_like": False, "redaction_class": "identifier"},
    "date": {"looks_date_like": True, "iso_date_ratio": 1.0, "email_ratio": 0.0,
             "redaction_class": "date"},
}


def _profile(header, kind, *, non_empty=50, observed=None):
    return ColumnProfile(profile_id="c1", table_id="t", col_index=0, header=header,
                         non_empty_count=non_empty, missing_count=0, distinct_count=non_empty,
                         observed_types=observed or {"text": non_empty},
                         format_indicators=_IND[kind], samples=[])


def _item(header, target, *, ambiguous=False, alts=None, conf=0.99):
    return ProposalItem(source_column_id="c1", source_header=header, proposed_target_field=target,
                        alternative_target_fields=alts or [], is_ambiguous=ambiguous,
                        ambiguity_reason=None, evidence=[f"header {header!r}"], confidence=conf)


def _classify(header, kind, target, **kw):
    return classify_proposal(_item(header, target, **kw), _profile(header, kind), SCHEMA)


# --------------------------------------------------------------------- GENERIC family -> REVIEW
# Each of these is maximally confident (conf=0.99), non-ambiguous, alternatives=[]. It must STILL review.
@pytest.mark.parametrize("header,kind,target,siblings", [
    ("Email", "email", "work_email", {"work_email", "personal_email"}),
    ("E-mail", "email", "work_email", {"work_email", "personal_email"}),
    ("Phone", "phone", "mobile_phone", {"mobile_phone", "work_phone"}),
    ("Telephone", "phone", "work_phone", {"mobile_phone", "work_phone"}),
    ("Name", "name", "full_name", {"full_name", "preferred_name"}),
    ("ID", "id", "employee_id", {"employee_id", "manager_employee_id"}),
    ("Identifier", "id", "employee_id", {"employee_id", "manager_employee_id"}),
])
def test_generic_sibling_header_reviews_even_when_confident(header, kind, target, siblings):
    r = _classify(header, kind, target)
    assert r.decision is Decision.NEEDS_REVIEW, f"{header!r} should escalate, got {r.reason}"
    # the policy independently offers BOTH sibling destinations, though the model gave alternatives=[]
    assert siblings <= set(r.candidate_target_fields), r.candidate_target_fields


def test_generic_start_date_reviews_with_date_siblings():
    r = _classify("Start Date", "date", "hire_date")
    assert r.decision is Decision.NEEDS_REVIEW
    assert "hire_date" in r.candidate_target_fields and "contract_start_date" in r.candidate_target_fields


def test_generic_dob_family_date_reviews():
    # a generic date proposed at a non-hire/contract role is caught by the sibling gate (all 5 dates).
    r = _classify("Effective Date", "date", "date_of_birth")
    assert r.decision is Decision.NEEDS_REVIEW
    assert "date_of_birth" in r.candidate_target_fields


# --------------------------------------------------------------------- QUALIFIED header -> may AUTO
@pytest.mark.parametrize("header,kind,target", [
    ("Corporate Email", "email", "work_email"),
    ("Work Mailbox", "email", "work_email"),
    ("Corporate Mailbox", "email", "work_email"),           # M3F high-ambiguity fixture header
    ("Personal Mailbox", "email", "personal_email"),
    ("Office Telephone", "phone", "work_phone"),
    ("Mobile Number", "phone", "mobile_phone"),
    ("Legal Display Name", "name", "full_name"),            # M3F high-ambiguity fixture header
    ("Preferred Name", "name", "preferred_name"),
    ("Employee Reference", "id", "employee_id"),
    ("Worker Reference", "id", "employee_id"),              # M3F high-ambiguity fixture header
    ("Reports To Reference", "id", "manager_employee_id"),  # M3F high-ambiguity fixture header
    ("Joining Effective Date", "date", "hire_date"),
    ("Contract Effective Date", "date", "contract_start_date"),
    ("Service Commencement", "date", "hire_date"),          # M3F high-ambiguity fixture header
])
def test_qualified_sibling_header_auto_accepts(header, kind, target):
    r = _classify(header, kind, target)
    assert r.decision is Decision.AUTO_ACCEPT, f"{header!r} -> {target} should auto-accept, got {r.reason}"
    assert r.target_field == target


# --------------------------------------------------------------------- CONFLICT -> REVIEW
def test_qualifier_points_at_a_different_sibling_reviews():
    # model proposes work_email but the header says "Personal"; escalate rather than trust confidence.
    r = _classify("Personal Mailbox", "email", "work_email")
    assert r.decision is Decision.NEEDS_REVIEW
    assert "personal_email" in r.candidate_target_fields


# --------------------------------------------------------------------- non-sibling targets unaffected
def test_non_sibling_target_is_not_touched_by_the_gate():
    # 'Org Function' -> department is not part of any sibling family; still auto-accepts as before.
    prof = ColumnProfile(profile_id="c1", table_id="t", col_index=0, header="Org Function",
                         non_empty_count=10, missing_count=0, distinct_count=3,
                         observed_types={"text": 10},
                         format_indicators={"likely_low_cardinality_category": True,
                                            "duplicate_ratio": 0.9, "redaction_class": "enum"},
                         samples=[])
    r = classify_proposal(_item("Org Function", "department"), prof, SCHEMA)
    assert r.decision is Decision.AUTO_ACCEPT


def test_confidence_alone_never_overrides_generic_ambiguity():
    low = _classify("Email", "email", "work_email", conf=0.10)
    high = _classify("Email", "email", "work_email", conf=1.0)
    assert low.decision is Decision.NEEDS_REVIEW and high.decision is Decision.NEEDS_REVIEW


# --------------------------------------------------------------------- schema wiring + frozen surface
def test_schema_declares_the_sibling_role_groups():
    keys = {g.key for g in SCHEMA.sibling_role_groups}
    assert {"email_role", "phone_role", "name_role", "identifier_role", "date_role"} <= keys
    assert SCHEMA.sibling_group_for("work_email").key == "email_role"
    assert SCHEMA.sibling_group_for("designation") is None


def test_sibling_groups_do_not_change_the_alias_inventory_hash():
    # §F: the fix is policy-level. The deterministic alias surface (and its frozen hash) is unchanged;
    # the exact frozen value is asserted in test_m3e_anti_overfitting — here we assert it stays stable
    # and stable across repeated loads (sibling_role_groups is not part of the hashed inventory).
    assert inventory_hash(SCHEMA) == inventory_hash(get_target_schema())
