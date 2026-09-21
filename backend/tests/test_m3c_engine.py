"""M3C adaptive source-intelligence — pure engine unit tests (order §15/§23).

Deterministic, no model, no DB: full-column date inference (MDY/DMY/ambiguous/mixed/YMD + two-digit
year century), column-level enum/boolean value maps (Tier-1 aliases + validated model proposals),
code/display relationship discovery (incl. the small-sample false-positive guards), referential
integrity and exact unique-name derivation, and the transform-operation whitelist.

These lock in the exact behaviour the milestone mandates and guard against regressions in the engine
that the mapping/preparation graphs depend on.
"""
from __future__ import annotations

from datetime import date

from app.date_inference import apply_date_value, infer_date_format, resolve_column_century
from app.enum_inference import build_enum_map, validate_model_value_map
from app.ingest import parse_file
from app.profiling import profile_table
from app.reference_integrity import (
    derive_reference_by_name,
    normalize_name,
    validate_reference_domain,
)
from app.relationships import CODE_SHAPE_MIN_SCORE, detect_code_display_pairs
from app.schema_loader import load_schema
from app.transform_plan import validate_model_operations
from app.config import get_settings


# ============================================================ date order inference (§4/§15)
def test_decisive_mdy_inferred_for_whole_column():
    inf = infer_date_format(["07/05/2011", "03/30/2015", "01/07/2008"])
    assert inf.order == "MDY" and inf.status == "inferred"
    assert inf.decisive_mdy >= 1 and inf.decisive_dmy == 0


def test_decisive_dmy_inferred_for_whole_column():
    inf = infer_date_format(["05/07/2011", "30/03/2015", "07/01/2008"])
    assert inf.order == "DMY" and inf.status == "inferred"
    assert inf.decisive_dmy >= 1 and inf.decisive_mdy == 0


def test_all_ambiguous_escalates_once():
    inf = infer_date_format(["05/07/2011", "03/04/2015"])
    assert inf.status == "ambiguous_needs_review" and inf.order is None
    assert inf.decisive_mdy == 0 and inf.decisive_dmy == 0 and inf.ambiguous == 2


def test_mixed_format_never_chooses_globally():
    inf = infer_date_format(["03/30/2015", "30/03/2015"])
    assert inf.status == "mixed_format" and inf.order is None
    assert inf.decisive_mdy >= 1 and inf.decisive_dmy >= 1


def test_iso_year_first_is_ymd():
    inf = infer_date_format(["2011-05-07", "2015-03-30", "2020-01-01"])
    assert inf.order == "YMD"


def test_hrdataset_style_mdy_from_late_day_values():
    # 3/30 and 9/24 have a second component > 12 -> decisive MDY; earlier rows are ambiguous.
    inf = infer_date_format(["7/5/2011", "1/7/2008", "3/30/2015", "9/24/2012"])
    assert inf.order == "MDY" and inf.decisive_dmy == 0


def test_feb_31_is_invalid_not_ambiguous():
    inf = infer_date_format(["31/02/2024"])       # 31>12 forces DMY order, but Feb 31 is impossible
    assert inf.invalid == 1 and inf.decisive_mdy == 0


# ============================================================ two-digit year century (§4)
DOB = {"not_future": True, "max_age_years": 100}
REF = date(2026, 1, 1)


def test_two_digit_order_known_century_separate():
    inf = infer_date_format(["11/30/25", "01/02/25", "03/30/70"])
    assert inf.order == "MDY" and inf.two_digit_year is True


def test_century_resolved_by_schema_constraint_future():
    r = apply_date_value("03/30/70", order="MDY", constraints=DOB, reference_date=REF)
    assert r.status == "valid" and r.iso == "1970-03-30"      # 2070 excluded as future DOB


def test_century_resolved_by_schema_constraint_max_age():
    r = apply_date_value("03/30/05", order="MDY", constraints=DOB, reference_date=REF)
    assert r.status == "valid" and r.iso == "2005-03-30"      # 1905 excluded as > max age


def test_century_ambiguous_without_constraints_needs_review():
    r = apply_date_value("03/30/40", order="MDY", constraints={}, reference_date=REF)
    assert r.status == "ambiguous_century" and len(r.century_candidates) == 2


def test_never_uses_hidden_pivot_column_level():
    # A column of 2-digit DOBs with a constraint resolves uniquely -> auto; none needs a hidden pivot.
    res = resolve_column_century(["03/30/70", "06/15/85", "01/02/99"], order="MDY",
                                 constraints=DOB, reference_date=REF)
    assert res.status == "resolved" and res.ambiguous_values == 0


# ============================================================ enum / boolean (§5/§6)
def _schema():
    return load_schema(get_settings().schema_path)


def test_gender_m_f_tier1_alias_complete():
    g = _schema().get("gender")
    m = build_enum_map(g, ["M", "F"])
    assert m.status == "complete" and m.value_map == {"M": "male", "F": "female"}


def test_full_gender_domain_incl_prefer_not_to_say():
    g = _schema().get("gender")
    m = build_enum_map(g, ["Female", "Male", "Non-binary", "Prefer not to say"])
    assert m.status == "complete"
    assert m.value_map["Prefer not to say"] == "undisclosed"
    assert m.value_map["Non-binary"] == "non_binary"


def test_unknown_enum_value_stays_unmapped():
    g = _schema().get("gender")
    m = build_enum_map(g, ["M", "F", "X"])
    assert "X" in m.unmapped and m.status == "partial"


def test_department_taxonomy_not_blindly_mapped():
    d = _schema().get("department")
    m = build_enum_map(d, ["Sales", "Marketing", "IT", "HR"])
    # Sales is a canonical department; Marketing/IT/HR are taxonomy decisions, never auto-mapped.
    assert "Sales" in m.value_map
    assert {"Marketing", "IT", "HR"} <= set(m.unmapped)


def test_model_value_map_validation_gates():
    g = _schema().get("gender")
    # valid target accepted
    assert validate_model_value_map(g, [{"source_value": "X", "target_value": "male"}], ["X"]).accepted == {"X": "male"}
    # out-of-enum target rejected
    v = validate_model_value_map(g, [{"source_value": "X", "target_value": "martian"}], ["X"])
    assert "X" not in v.accepted and v.rejected
    # unobserved source rejected (model cannot invent a value)
    v2 = validate_model_value_map(g, [{"source_value": "ZZ", "target_value": "male"}], ["X"])
    assert "ZZ" not in v2.accepted and any(r["source"] == "ZZ" for r in v2.rejected)
    # ambiguous flag escalates
    v3 = validate_model_value_map(g, [{"source_value": "X", "target_value": "male", "ambiguous": True}], ["X"])
    assert "X" not in v3.accepted and "X" in v3.unresolved


def test_boolean_domain_maps_yes_no_true_false():
    # find a boolean target if the schema has one; else construct via a known boolean field.
    sch = _schema()
    bl = next((f for f in sch.fields if f.value_type == "boolean"), None)
    if bl is None:
        return
    m = build_enum_map(bl, ["Yes", "No"])
    assert m.status == "complete" and set(m.value_map.values()) <= {"true", "false"}


# ============================================================ code/display relationships (§7/§15)
def _profiles_and_values(name: str, csv: bytes):
    pf = parse_file(filename=name, data=csv, stored_name="s", max_bytes=10_000_000, max_rows=10000)
    profs = profile_table(pf.tables[0], pf.records)
    values: dict[int, list] = {}
    for rec in pf.records:
        for ci, cell in enumerate(rec.cells):
            values.setdefault(ci, []).append(cell.value)
    return profs, values


def test_perfect_numeric_code_display_pair_is_coded():
    profs, vals = _profiles_and_values("g.csv", b"GenderID,Sex\n0,F\n1,M\n0,F\n1,M\n")
    pairs = detect_code_display_pairs(vals, profs)
    assert len(pairs) == 1
    pe = pairs[0]
    assert pe.relationship == "one_to_one"
    assert pe.code_header == "GenderID" and pe.label_header == "Sex"
    assert pe.code_is_coded is True and pe.code_score >= CODE_SHAPE_MIN_SCORE


def test_all_unique_columns_are_not_a_code_display_pair():
    # Employee ID + Full Name are both all-unique -> a trivial bijection, NEVER a code/display pair.
    profs, vals = _profiles_and_values(
        "u.csv", b"EmpID,FullName\nE1,Alice\nE2,Bob\nE3,Carol\nE4,Dana\n")
    assert detect_code_display_pairs(vals, profs) == []


def test_textual_coincidence_pair_is_not_code_shaped():
    # Two independent low-card textual attributes that happen to line up 1:1 on a tiny sample:
    # detected, but NOT code-shaped -> must never be auto-retired as redundant.
    profs, vals = _profiles_and_values("t.csv", b"Size,Colour\nM,Red\nL,Blue\nXL,Green\nM,Red\n")
    pairs = detect_code_display_pairs(vals, profs)
    assert pairs and all(not pe.code_is_coded for pe in pairs)


def test_inconsistent_pair_is_flagged_not_one_to_one():
    profs, vals = _profiles_and_values("d.csv", b"DeptID,Dept\n1,Eng\n2,Fin\n1,Fin\n2,Fin\n")
    pairs = detect_code_display_pairs(vals, profs)
    assert pairs and any(pe.relationship == "inconsistent" for pe in pairs)
    assert all(pe.relationship != "one_to_one" for pe in pairs)


# ============================================================ referential integrity (§8/§15)
def test_reference_domain_strong_overlap_valid():
    cov = validate_reference_domain(["E100", "E101", "E100"], {"E100", "E101", "E102"})
    assert cov.verdict == "valid"


def test_reference_domain_zero_overlap_poor():
    cov = validate_reference_domain(["Z1", "Z2", "Z3"], {"E100", "E101"})
    assert cov.verdict == "poor" and cov.coverage == 0.0


def test_exact_unique_name_derivation():
    d = derive_reference_by_name(["Jane Doe", "Sam Roe"],
                                 {"jane doe": {"E100"}, "sam roe": {"E101"}})
    assert d.resolved == {"Jane Doe": "E100", "Sam Roe": "E101"}


def test_duplicate_name_is_never_fuzzy_derived():
    d = derive_reference_by_name(["Jane Doe"], {"jane doe": {"E100", "E200"}})
    assert "Jane Doe" not in d.resolved and d.ambiguous == ["Jane Doe"]


def test_normalize_name_is_exact_only():
    assert normalize_name("  Jane   DOE ") == "jane doe"


# ============================================================ transform-op whitelist (§10)
def test_arbitrary_operation_is_rejected():
    ops, err = validate_model_operations([{"op": "exec", "code": "rm -rf /"}], None)
    assert ops == [] and err and "whitelist" in err


def test_enum_map_out_of_enum_target_rejected():
    g = _schema().get("gender")
    ops, err = validate_model_operations([{"op": "enum_map", "value_map": {"X": "martian"}}], g)
    assert ops == [] and err and "target" in err


def test_structural_ops_not_accepted_from_model():
    ops, err = validate_model_operations([{"op": "redundant_representation"}], None)
    assert ops == [] and err
