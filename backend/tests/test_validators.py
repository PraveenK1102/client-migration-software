"""Validators: email (maintained lib, no DNS), calendar-safe dates, fullmatch shapes,
enum canonicalization. No network."""
from __future__ import annotations

from app.validators import (
    canonicalize_department,
    interpret_date,
    looks_like_email,
    looks_like_iso_date,
    validate_work_email,
)

ENUM = ["Engineering", "Sales", "Finance", "People Operations"]


def test_email_valid_normalizes_domain_preserves_local():
    r = validate_work_email("  Alice.Smith@ACME.com ")
    assert r.status == "valid"
    assert r.normalized == "Alice.Smith@acme.com"          # domain lowercased, local preserved
    assert r.comparison_key == "alice.smith@acme.com"      # case-insensitive whole address


def test_email_invalid_rejected_no_dns():
    assert validate_work_email("not-an-email").status == "invalid"
    assert validate_work_email("a@b").status == "invalid"
    assert validate_work_email("a b@x.com").status == "invalid"


def test_iso_shape_fullmatch_rejects_extra_content():
    assert looks_like_iso_date("2024-03-15")
    assert not looks_like_iso_date("2024-03-15 and more")
    assert not looks_like_iso_date("x2024-03-15")


def test_email_shape_is_profile_only_and_fullmatch():
    assert looks_like_email("a@b.com")
    assert not looks_like_email("a@b.com trailing")


def test_dates_iso_and_impossible():
    assert interpret_date("2021-03-15").status == "valid"
    assert interpret_date("2026-02-31").status == "invalid"   # impossible calendar date
    assert interpret_date("2024-13-01").status == "invalid"


def test_dates_numeric_unambiguous_ambiguous_impossible():
    assert interpret_date("13/04/2024").iso == "2024-04-13"   # DMY unambiguous
    amb = interpret_date("03/04/2024")
    assert amb.status == "ambiguous" and len(amb.candidates) == 2
    assert interpret_date("31/02/2024").status == "invalid"
    same = interpret_date("05/05/2024")                        # identical under both readings
    assert same.status == "valid" and same.iso == "2024-05-05"


def test_dates_convention_resolves_ambiguous():
    assert interpret_date("03/04/2024", convention="DMY").iso == "2024-04-03"
    assert interpret_date("03/04/2024", convention="MDY").iso == "2024-03-04"


def test_dates_month_name():
    assert interpret_date("15 Mar 2024").iso == "2024-03-15"
    assert interpret_date("March 15, 2024").iso == "2024-03-15"


def test_department_canonicalization():
    assert canonicalize_department("ENGINEERING", ENUM) == ("Engineering", "canonical")
    assert canonicalize_department("  people operations ", ENUM) == ("People Operations", "canonical")
    assert canonicalize_department("Marketing", ENUM) == (None, "unknown")
    assert canonicalize_department("eng", ENUM) == (None, "unknown")   # no abbreviation guessing
    assert canonicalize_department("", ENUM) == (None, "empty")
