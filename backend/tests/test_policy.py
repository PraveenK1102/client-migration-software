"""Deterministic acceptance/escalation policy tests (no model involved)."""
from __future__ import annotations

from app.llm.schema import ProposalItem
from app.policy import Decision, classify_proposal, resolve_table_conflicts
from app.profiling import ColumnProfile
from app.schema_loader import get_target_schema

SCHEMA = get_target_schema()


def make_profile(header, *, samples=None, indicators=None, observed_types=None, non_empty=5, col=0):
    return ColumnProfile(
        profile_id=f"col_{header}", table_id="tbl_1", col_index=col, header=header,
        non_empty_count=non_empty, missing_count=0, distinct_count=non_empty,
        observed_types=observed_types or {"text": non_empty},
        format_indicators=indicators or {"email_ratio": 0.0, "iso_date_ratio": 0.0,
                                         "slash_date_ratio": 0.0, "looks_date_like": False,
                                         "has_leading_zero_values": False},
        samples=samples or ["x", "y"],
    )


def make_item(header, target, *, alts=None, ambiguous=False, conf=0.9, evidence=None):
    return ProposalItem(
        source_column_id=f"col_{header}", source_header=header, proposed_target_field=target,
        alternative_target_fields=alts or [], is_ambiguous=ambiguous, ambiguity_reason=None,
        evidence=evidence or [f"header {header}"], confidence=conf,
    )


def test_obvious_case_auto_accepts():
    prof = make_profile("EmployeeNumber", samples=["001", "002"], observed_types={"number": 5})
    res = classify_proposal(make_item("EmployeeNumber", "employee_id"), prof, SCHEMA)
    assert res.decision is Decision.AUTO_ACCEPT
    assert res.target_field == "employee_id"


def test_email_case_auto_accepts():
    # M3F §F: a *qualified* email header (distinctive "work" qualifier) still auto-accepts.
    # A bare "Mail"/"Email" is generic and now escalates (see test_m3f_sibling_ambiguity).
    prof = make_profile("Work Email", samples=["a@x.com"], indicators={"email_ratio": 1.0,
                        "looks_date_like": False})
    res = classify_proposal(make_item("Work Email", "work_email"), prof, SCHEMA)
    assert res.decision is Decision.AUTO_ACCEPT


def test_disambiguated_dates_auto_accept():
    joined = make_profile("Joined", indicators={"looks_date_like": True, "email_ratio": 0})
    r1 = classify_proposal(make_item("Joined", "hire_date"), joined, SCHEMA)
    assert r1.decision is Decision.AUTO_ACCEPT

    contract = make_profile("Contract Start", indicators={"looks_date_like": True, "email_ratio": 0})
    r2 = classify_proposal(make_item("Contract Start", "contract_start_date"), contract, SCHEMA)
    assert r2.decision is Decision.AUTO_ACCEPT


def test_ambiguous_date_role_escalates_even_with_high_confidence():
    """The KEY escalation: a bare 'Start' with an OVERCONFIDENT, non-ambiguous proposal
    must still route to review. Model confidence is not a gate."""
    prof = make_profile("Start", indicators={"looks_date_like": True, "email_ratio": 0})
    item = make_item("Start", "hire_date", ambiguous=False, conf=0.99)
    res = classify_proposal(item, prof, SCHEMA)
    assert res.decision is Decision.NEEDS_REVIEW
    assert set(res.candidate_target_fields) == {"hire_date", "contract_start_date"}


def test_nonexistent_target_not_accepted():
    prof = make_profile("Bonus")
    res = classify_proposal(make_item("Bonus", "salary"), prof, SCHEMA)  # salary not in schema
    assert res.decision is Decision.NEEDS_REVIEW
    assert res.target_field is None


def test_type_incompatible_escalates():
    # values look like dates but proposed to work_email
    prof = make_profile("Weird", indicators={"email_ratio": 0.0, "looks_date_like": True})
    res = classify_proposal(make_item("Weird mail", "work_email"), prof, SCHEMA)
    assert res.decision is Decision.NEEDS_REVIEW


def test_competing_alternative_escalates():
    prof = make_profile("Contact")
    item = make_item("Contact", "work_email", alts=["full_name"])
    res = classify_proposal(item, prof, SCHEMA)
    assert res.decision is Decision.NEEDS_REVIEW


def test_unresolved_null_paths():
    prof = make_profile("Mystery")
    # null + not ambiguous + no alts -> unmapped (recorded, not an escalation)
    r1 = classify_proposal(make_item("Mystery", None, conf=0.1), prof, SCHEMA)
    assert r1.decision is Decision.UNMAPPED
    # null + ambiguous -> review
    r2 = classify_proposal(make_item("Mystery", None, ambiguous=True), prof, SCHEMA)
    assert r2.decision is Decision.NEEDS_REVIEW


def test_no_semantic_support_escalates_despite_confidence():
    # header shares no concept token with target; high confidence must not auto-accept
    prof = make_profile("XYZ123", samples=["Alice", "Bob"])
    res = classify_proposal(make_item("XYZ123", "full_name", conf=0.99), prof, SCHEMA)
    assert res.decision is Decision.NEEDS_REVIEW


def test_within_table_conflict_downgrades_both():
    p1 = make_profile("Emp ID", col=0, samples=["001"])
    p2 = make_profile("Staff No", col=1, samples=["A1"])
    r1 = classify_proposal(make_item("Emp ID", "employee_id"), p1, SCHEMA)
    r2 = classify_proposal(make_item("Staff No", "employee_id"), p2, SCHEMA)
    assert r1.decision is Decision.AUTO_ACCEPT and r2.decision is Decision.AUTO_ACCEPT
    resolved = resolve_table_conflicts([r1, r2])
    assert all(r.decision is Decision.NEEDS_REVIEW for r in resolved)
    assert all("conflict" in r.reason.lower() for r in resolved)


def test_cross_file_same_target_is_allowed():
    # Same target from two DIFFERENT tables must NOT be treated as a conflict.
    p1 = make_profile("EmployeeNumber", samples=["001"])
    p2 = make_profile("Emp ID", samples=["001"])
    r1 = classify_proposal(make_item("EmployeeNumber", "employee_id"), p1, SCHEMA)
    r2 = classify_proposal(make_item("Emp ID", "employee_id"), p2, SCHEMA)
    r2.evidence_summary["table_id"] = "tbl_2"
    # resolve_table_conflicts is only ever called per-table, so cross-table pairs
    # are never passed together; each stays auto-accepted.
    assert resolve_table_conflicts([r1])[0].decision is Decision.AUTO_ACCEPT
    assert resolve_table_conflicts([r2])[0].decision is Decision.AUTO_ACCEPT
