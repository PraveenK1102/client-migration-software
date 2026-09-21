"""Unit-level normalization + reconciliation invariants (no model, no HTTP)."""
from __future__ import annotations

from app.db import Database
from app.prepare import normalize_field, prepare
from app.profiling import ColumnProfile
from app.schema_loader import get_target_schema
from app.source_records import RawCell, RawType, SourceRecord, SourceRef, SourceTable

SCHEMA = get_target_schema()


def test_normalize_preserves_identity_and_names():
    assert normalize_field("employee_id", "0017", SCHEMA).value == "0017"       # leading zeros kept
    assert normalize_field("employee_id", 17, SCHEMA).value == "17"             # never int-cast semantics
    nm = normalize_field("full_name", "  José  O'Brien ", SCHEMA)
    assert nm.value == "José  O'Brien" and nm.status == "resolved"              # internal content preserved
    assert normalize_field("employee_id", "0", SCHEMA).value == "0"            # "0" is not blank
    assert normalize_field("full_name", "   ", SCHEMA).status == "missing"


def test_normalize_email_domain_casing():
    fv = normalize_field("work_email", " User.Name@EXAMPLE.COM ", SCHEMA)
    assert fv.value == "User.Name@example.com" and fv.status == "resolved"


def _setup(tmp_path, headers, rows):
    db = Database(tmp_path / "app.db")
    job = db.create_job(schema_version="employee.v1", provider="fake", model_id="fake", adapter_kind="fake")
    table = SourceTable(table_id="tbl_x", file_id="f1", original_filename="t.csv", sheet_name=None,
                        headers=headers, n_rows=len(rows))
    db.add_source_table(job, table)
    profs = []
    for ci, h in enumerate(headers):
        p = ColumnProfile(profile_id=f"col_{ci}", table_id="tbl_x", col_index=ci, header=h,
                          non_empty_count=len(rows), missing_count=0, distinct_count=len(rows),
                          observed_types={"text": len(rows)}, format_indicators={}, samples=[])
        profs.append(p)
        db.upsert_decision(job, profile_id=p.profile_id, table_id="tbl_x", source_header=h,
                           target_field=h, status="auto_accepted", actor="system", method="rule",
                           reason="test")
    db.add_profiles(job, profs)
    recs = []
    for ri, row in enumerate(rows, start=1):
        ref = SourceRef(file_id="f1", original_filename="t.csv", table_id="tbl_x", row_number=ri)
        cells = [RawCell(col_index=ci, header=headers[ci],
                         value=(row[ci] if row[ci] != "" else None), raw_type=RawType.TEXT,
                         ref=ref.model_copy(update={"col_index": ci, "header": headers[ci]}))
                 for ci in range(len(headers))]
        recs.append(SourceRecord(ref=ref, cells=cells))
    db.add_source_rows(job, recs)
    return db, job


def test_no_hire_date_fallback_from_contract(tmp_path):
    headers = ["employee_id", "full_name", "work_email", "hire_date", "contract_start_date"]
    rows = [["001", "A B", "a@x.com", "", "2021-01-01"]]   # hire missing, contract present
    db, job = _setup(tmp_path, headers, rows)
    try:
        result = prepare(db, job, SCHEMA)
        cand = result["candidates"][0]
        assert cand["record"]["hire_date"]["value"] is None        # NOT filled from contract
        assert cand["record"]["contract_start_date"]["value"] == "2021-01-01"
        assert cand["eligibility"] == "blocked"                     # missing required hire_date
        assert any(i["issue_type"] == "missing_required" and i["field"] == "hire_date"
                   for i in result["issues"])
    finally:
        db.close()


def test_duplicate_collapse_and_complementary_merge(tmp_path):
    headers = ["employee_id", "full_name", "work_email", "hire_date", "contract_start_date"]
    rows = [
        ["001", "Alice", "alice@x.com", "2021-01-01", ""],
        ["001", "Alice", "alice@x.com", "2021-01-01", ""],   # exact duplicate -> collapse
        ["002", "Bob", "", "2020-02-02", ""],
        ["002", "", "bob@x.com", "", ""],                    # complementary -> merge
    ]
    db, job = _setup(tmp_path, headers, rows)
    try:
        result = prepare(db, job, SCHEMA)
        by_key = {c["business_key"]: c for c in result["candidates"]}
        assert set(by_key) == {"001", "002"}                 # 4 rows -> 2 candidates
        assert by_key["002"]["record"]["full_name"]["value"] == "Bob"       # from row 3
        assert by_key["002"]["record"]["work_email"]["value"] == "bob@x.com"  # from row 4
        assert by_key["001"]["eligibility"] == "eligible"
    finally:
        db.close()


def test_unknown_optional_value_blocks_not_silently_dropped(tmp_path):
    headers = ["employee_id", "full_name", "work_email", "department", "hire_date"]
    rows = [
        ["100", "Ada L", "ada@x.com", "Engineering", "2020-01-01"],   # clean -> eligible
        ["101", "Bo K", "bo@x.com", "Marketing", "2020-02-02"],       # unknown optional dept -> blocked
    ]
    db, job = _setup(tmp_path, headers, rows)
    try:
        result = prepare(db, job, SCHEMA)
        by_key = {c["business_key"]: c for c in result["candidates"]}
        assert by_key["100"]["eligibility"] == "eligible"
        assert by_key["101"]["eligibility"] == "blocked"   # unknown enum can't silently pass
        # M3C: unknown enum values collapse to ONE column-scoped review (not one per row), but the
        # value still BLOCKS and the affected candidate is surfaced — never silently dropped.
        ue = [i for i in result["issues"] if i["issue_type"] == "unknown_enum"]
        assert len(ue) == 1 and ue[0]["candidate_key"] is None
        assert by_key["101"]["id"] in ue[0]["affected"]["candidate_ids"]
    finally:
        db.close()


def test_same_id_conflict_is_flagged_not_guessed(tmp_path):
    headers = ["employee_id", "full_name", "work_email", "hire_date", "contract_start_date"]
    rows = [
        ["003", "Carol", "carol@x.com", "2019-03-03", ""],
        ["003", "Caroline", "carol@x.com", "2019-03-03", ""],   # conflicting full_name
    ]
    db, job = _setup(tmp_path, headers, rows)
    try:
        result = prepare(db, job, SCHEMA)
        cand = result["candidates"][0]
        assert cand["record"]["full_name"]["status"] == "conflict"
        assert any(i["issue_type"] == "value_conflict" and i["field"] == "full_name"
                   for i in result["issues"])
    finally:
        db.close()
