"""Deterministic mapping rules: canonical/alias resolve; generic tokens, semantic-word
shifts, bare 'Start', and header collisions do NOT resolve."""
from __future__ import annotations

from app.mapping_rules import map_table_deterministic
from app.profiling import ColumnProfile
from app.schema_loader import get_target_schema

SCHEMA = get_target_schema()


def _p(header, col=0, indicators=None):
    return ColumnProfile(profile_id=f"c{col}", table_id="t", col_index=col, header=header,
                         non_empty_count=3, missing_count=0, distinct_count=3,
                         observed_types={"text": 3}, format_indicators=indicators or {}, samples=["x"])


def _by_header(results):
    return {r.header: r for r in results}


def test_canonical_headers_resolve():
    profs = [_p("employee_id", 0), _p("full_name", 1), _p("work_email", 2),
             _p("department", 3), _p("hire_date", 4), _p("contract_start_date", 5)]
    res = _by_header(map_table_deterministic(profs, SCHEMA))
    assert res["employee_id"].target == "employee_id" and res["employee_id"].resolved
    assert res["hire_date"].target == "hire_date" and res["hire_date"].method == "rule"
    assert res["contract_start_date"].target == "contract_start_date"


def test_case_space_underscore_normalization():
    res = _by_header(map_table_deterministic([_p("Employee ID"), _p("Work Email", 1)], SCHEMA))
    assert res["Employee ID"].target == "employee_id"
    assert res["Work Email"].target == "work_email"


def test_declared_alias_resolves():
    res = _by_header(map_table_deterministic([_p("Employee Number"), _p("Contract Start", 1)], SCHEMA))
    assert res["Employee Number"].target == "employee_id"
    assert res["Contract Start"].target == "contract_start_date"


def test_universal_employee_id_abbreviations_resolve():
    # Common tenant-agnostic abbreviations for an employee identifier (compact-matched) must resolve
    # deterministically, the same as "Employee Number" — validated on real external HR exports where
    # the id column is spelled EmpID / Emp_ID rather than the canonical form.
    for header in ["EmpID", "Emp_ID", "Emp ID", "Emp No", "Emp Code", "Staff Number"]:
        res = _by_header(map_table_deterministic([_p(header)], SCHEMA))
        assert res[header].target == "employee_id" and res[header].resolved, header


def test_generic_tokens_do_not_resolve():
    for header in ("id", "Name", "date", "Start", "contact", "code"):
        r = map_table_deterministic([_p(header)], SCHEMA)[0]
        assert not r.resolved, f"{header} should be unresolved"


def test_semantic_word_shift_not_resolved():
    # personal_email must NOT become work_email; birth/contract date not hire_date. Under the v2
    # contract these headers resolve to their OWN fields (personal_email / date_of_birth) — never
    # to a different concept; a header with no destination ("company") stays unresolved.
    for header in ("personal_email", "Personal Email", "birth_date", "date_of_birth"):
        r = map_table_deterministic([_p(header)], SCHEMA)[0]
        assert r.target not in ("work_email", "hire_date", "contract_start_date"), f"{header} shifted meaning"
    assert map_table_deterministic([_p("personal_email")], SCHEMA)[0].target == "personal_email"
    assert map_table_deterministic([_p("date_of_birth")], SCHEMA)[0].target == "date_of_birth"
    r = map_table_deterministic([_p("company")], SCHEMA)[0]
    assert not r.resolved, "company has no destination and must not resolve"


def test_child_table_context_resolves_collection_item_fields():
    # A sheet named like a declared collection resolves its columns against that collection only.
    res = _by_header(map_table_deterministic(
        [_p("Employee ID", 0), _p("Vehicle Type", 1), _p("Registration Number", 2), _p("Colour", 3)],
        SCHEMA, table_name="Vehicles"))
    assert res["Employee ID"].target == "employee_id" and res["Employee ID"].destination_kind == "CORE_FIELD"
    assert res["Vehicle Type"].target == "vehicles[].type"
    assert res["Registration Number"].target == "vehicles[].registration_number"
    assert res["Registration Number"].destination_kind == "COLLECTION_FIELD"
    assert not res["Colour"].resolved
    # The same bare item header in an EMPLOYEE table is NOT grouped heuristically.
    assert not map_table_deterministic([_p("Registration Number")], SCHEMA, table_name="employees")[0].resolved


def test_legacy_indexed_and_delimited_vehicle_columns_are_schema_configured():
    res = _by_header(map_table_deterministic(
        [_p("Vehicle 1", 0), _p("Vehicle 2", 1), _p("Vehicle Numbers", 2), _p("Vehicle Number", 3)],
        SCHEMA, table_name="employees"))
    assert res["Vehicle 1"].path_meta == {"index": 1} and res["Vehicle 2"].path_meta == {"index": 2}
    assert res["Vehicle 1"].target == "vehicles[].registration_number"
    assert res["Vehicle Numbers"].path_meta["multi_value"] is True
    assert res["Vehicle Number"].path_meta == {"single_item": True}
    assert all(r.resolved for r in res.values())     # indexed columns are distinct items, not a collision


def test_within_table_collision_unresolves_both():
    # Two columns normalize to the same target -> both unresolved (no silent overwrite).
    res = map_table_deterministic([_p("hire_date", 0), _p("Hire Date", 1)], SCHEMA)
    assert all(not r.resolved for r in res)
    assert all("collision" in r.reason.lower() for r in res)


def test_email_shape_mismatch_flags_data_quality_but_keeps_mapping():
    # Canonical header 'work_email' with non-email values -> mapping kept, DQ flagged.
    prof = _p("work_email", indicators={"email_ratio": 0.0})
    r = map_table_deterministic([prof], SCHEMA)[0]
    assert r.resolved and r.target == "work_email"
    assert r.data_quality_flags
