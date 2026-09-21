"""M3A.2 (order G3.3-G3.7): structured one-to-many collections at the ``prepare()`` unit level.

No model, no HTTP, no fixtures from disk: every test builds a tiny multi-table job (an employee
table + one or more relational child tables / legacy flattened columns) directly in a temp
``Database`` and calls :func:`app.prepare.prepare`. Human resolutions are exercised through the
same persisted record-issue path the graph uses (``upsert_record_issue`` +
``resolve_record_issue_if_current`` -> overlays on the next recompute).

Covered:
  G3.3  one employee receives several child items (relational child sheet)
  G3.4  exact duplicate child rows collapse into ONE item, provenance of BOTH rows kept
  G3.5  same identity, different value -> ``collection_conflict`` with variant options; the
        candidate is blocked; a human ``select`` makes it eligible with the chosen variant
  G3.6  a second child sheet (education) attaches rows to the right employee only
  G3.7  every item field carries full provenance (file, sheet, row, header, raw, col_index)
  plus  orphan child rows (never silently dropped), indexed columns, multi-value columns,
        drop_item / correct overlays, required item field missing, item enum normalization.
"""
from __future__ import annotations

from pathlib import Path

from app.db import Database
from app.prepare import normalize_field, prepare
from app.profiling import ColumnProfile
from app.schema_loader import load_schema, resolve_schema_path
from app.source_records import RawCell, RawType, SourceRecord, SourceRef, SourceTable

REPO = Path(__file__).resolve().parent.parent.parent
SCHEMA = load_schema(resolve_schema_path(REPO / "schemas", "employee.v2"))

EMP_HEADERS = ["employee_id", "full_name", "work_email", "hire_date"]
EMP_PATHS = ["employee_id", "full_name", "work_email", "hire_date"]
EMP_ROWS = [
    ["E100", "Priya Sharma", "priya@example.com", "2020-01-15"],
    ["E101", "Arjun Mehta", "arjun@example.com", "2019-06-01"],
    ["E102", "Meera Nair", "meera@example.com", "2021-03-10"],
]
VEH_HEADERS = ["employee_id", "type", "registration_number"]
VEH_PATHS = ["employee_id", "vehicles[].type", "vehicles[].registration_number"]
EDU_HEADERS = ["employee_id", "level", "institution", "score", "year"]
EDU_PATHS = ["employee_id", "education_history[].level", "education_history[].institution",
             "education_history[].score", "education_history[].year"]

PROV_KEYS = {"original_filename", "sheet_name", "row_number", "header", "raw", "col_index", "table_id"}


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------
def _new_job(tmp_path):
    db = Database(tmp_path / "app.db")
    job = db.create_job(schema_version=SCHEMA.version, provider="fake", model_id="fake", adapter_kind="fake")
    return db, job


def _add_table(db, job, *, table_id, file_id, filename, sheet, headers, rows, mappings):
    """Add one source table with its rows, column profiles and ACCEPTED mapping decisions.

    ``mappings`` is aligned with ``headers``: a path string, a ``(path, path_meta)`` tuple, or
    ``None`` for a column that stays unmapped.
    """
    table = SourceTable(table_id=table_id, file_id=file_id, original_filename=filename, sheet_name=sheet,
                        headers=headers, n_rows=len(rows))
    db.add_source_table(job, table)
    profs = []
    for ci, h in enumerate(headers):
        p = ColumnProfile(profile_id=f"{table_id}_col_{ci}", table_id=table_id, col_index=ci, header=h,
                          non_empty_count=len(rows), missing_count=0, distinct_count=len(rows),
                          observed_types={"text": len(rows)}, format_indicators={}, samples=[])
        profs.append(p)
        m = mappings[ci]
        if m is None:
            continue
        path, meta = m if isinstance(m, tuple) else (m, None)
        db.upsert_decision(job, profile_id=p.profile_id, table_id=table_id, source_header=h,
                           target_field=path, status="auto_accepted", actor="system", method="rule",
                           reason="test", destination_kind=SCHEMA.destination_kind(path), path_meta=meta)
    db.add_profiles(job, profs)
    recs = []
    for ri, row in enumerate(rows, start=1):
        ref = SourceRef(file_id=file_id, original_filename=filename, table_id=table_id, sheet_name=sheet,
                        row_number=ri)
        cells = [RawCell(col_index=ci, header=headers[ci],
                         value=(row[ci] if row[ci] != "" else None), raw_type=RawType.TEXT,
                         ref=ref.model_copy(update={"col_index": ci, "header": headers[ci]}))
                 for ci in range(len(headers))]
        recs.append(SourceRecord(ref=ref, cells=cells))
    db.add_source_rows(job, recs)
    return table_id


def _add_employees(db, job, rows=EMP_ROWS, headers=EMP_HEADERS, mappings=EMP_PATHS):
    return _add_table(db, job, table_id="tbl_emp", file_id="f_emp", filename="employees.csv", sheet=None,
                      headers=headers, rows=rows, mappings=mappings)


def _add_vehicles(db, job, rows):
    return _add_table(db, job, table_id="tbl_veh", file_id="f_hr", filename="hr_details.xlsx", sheet="Vehicles",
                      headers=VEH_HEADERS, rows=rows, mappings=VEH_PATHS)


def _add_education(db, job, rows):
    return _add_table(db, job, table_id="tbl_edu", file_id="f_hr", filename="hr_details.xlsx", sheet="Education",
                      headers=EDU_HEADERS, rows=rows, mappings=EDU_PATHS)


def _by_key(result):
    return {c["business_key"]: c for c in result["candidates"]}


def _items(cand, coll):
    return cand["collections"].get(coll, [])


def _item_by_reg(cand, reg):
    return next(it for it in _items(cand, "vehicles") if it["identity_key"] == reg.lower())


def _persist_issues(db, job, result):
    """Mirror graph.prepare_records_node: persist the recomputed issues so a human can resolve them."""
    keep = set()
    for iss in result["issues"]:
        keep.add(iss["id"])
        db.upsert_record_issue(job, issue_id=iss["id"], candidate_key=iss.get("candidate_key"),
                               field=iss.get("field"), issue_type=iss["issue_type"], reason=iss["reason"],
                               options=iss.get("options", []), affected=iss.get("affected", {}),
                               scope=iss.get("scope"))
    db.supersede_open_issues_not_in(job, keep)


def _only_issue(result, issue_type):
    found = [i for i in result["issues"] if i["issue_type"] == issue_type]
    assert len(found) == 1, f"expected exactly one {issue_type}, got {[i['issue_type'] for i in result['issues']]}"
    return found[0]


def _resolve(db, issue, resolution):
    status = db.resolve_record_issue_if_current(issue["id"], expected_version=1, resolution=resolution)
    assert status == "resolved", status


# --------------------------------------------------------------------------------------------
# G3.3 — one employee receives several child items
# --------------------------------------------------------------------------------------------
def test_child_sheet_attaches_two_vehicle_items_to_one_employee(tmp_path):
    db, job = _new_job(tmp_path)
    try:
        _add_employees(db, job)
        _add_vehicles(db, job, [
            ["E100", "Car", "TN70XY9876"],
            ["E100", "Motorcycle", "TN70AB1234"],
        ])
        result = prepare(db, job, SCHEMA)
        by_key = _by_key(result)
        # child rows never become employee candidates
        assert set(by_key) == {"E100", "E101", "E102"}
        vehicles = _items(by_key["E100"], "vehicles")
        assert len(vehicles) == 2
        assert {it["identity_key"] for it in vehicles} == {"tn70xy9876", "tn70ab1234"}
        assert {it["status"] for it in vehicles} == {"resolved"}
        car = _item_by_reg(by_key["E100"], "TN70XY9876")
        bike = _item_by_reg(by_key["E100"], "TN70AB1234")
        assert car["fields"]["type"]["value"] == "car"                 # enum canonicalized
        assert car["fields"]["registration_number"]["value"] == "TN70XY9876"
        assert bike["fields"]["type"]["value"] == "motorcycle"
        # other employees received nothing
        assert _items(by_key["E101"], "vehicles") == []
        assert _items(by_key["E102"], "vehicles") == []
        assert result["issues"] == []
        assert by_key["E100"]["eligibility"] == "eligible"
        assert result["metrics"]["child_rows"] == 2
        assert result["metrics"]["employee_rows"] == 3
        assert result["metrics"]["collection_items"] == 2
    finally:
        db.close()


# --------------------------------------------------------------------------------------------
# G3.4 — exact duplicates collapse into one item; both source rows kept
# --------------------------------------------------------------------------------------------
def test_exact_duplicate_child_row_collapses_without_losing_provenance(tmp_path):
    db, job = _new_job(tmp_path)
    try:
        _add_employees(db, job)
        _add_vehicles(db, job, [
            ["E101", "Car", "KA01CD5678"],
            ["E101", "Car", "KA01CD5678"],     # exact duplicate
        ])
        result = prepare(db, job, SCHEMA)
        e101 = _by_key(result)["E101"]
        vehicles = _items(e101, "vehicles")
        assert len(vehicles) == 1
        item = vehicles[0]
        assert item["status"] == "resolved"
        assert item["duplicates_collapsed"] == 1
        # BOTH row refs are kept in sources (no data loss)
        assert sorted(s["row_number"] for s in item["sources"]) == [1, 2]
        assert all(s["table_id"] == "tbl_veh" and s["sheet_name"] == "Vehicles" for s in item["sources"])
        # ...and per-field provenance carries both rows too
        reg_prov = item["fields"]["registration_number"]["provenance"]
        assert sorted(p["row_number"] for p in reg_prov) == [1, 2]
        assert {p["raw"] for p in reg_prov} == {"KA01CD5678"}
        assert item["variants"] == []                                   # no conflict recorded
        assert result["issues"] == []
        assert e101["eligibility"] == "eligible"
        assert result["metrics"]["child_rows"] == 2                     # both rows were processed
    finally:
        db.close()


# --------------------------------------------------------------------------------------------
# G3.5 — same identity, different value -> conflict -> human select -> eligible
# --------------------------------------------------------------------------------------------
def test_conflicting_item_variant_blocks_then_human_select_resolves(tmp_path):
    db, job = _new_job(tmp_path)
    try:
        _add_employees(db, job)
        _add_vehicles(db, job, [
            ["E100", "Car", "TN70XY9876"],
            ["E100", "Motorcycle", "TN70AB1234"],
            ["E100", "Scooter", "TN70AB1234"],   # same registration, different type
        ])
        result = prepare(db, job, SCHEMA)
        e100 = _by_key(result)["E100"]
        assert e100["eligibility"] == "blocked"

        issue = _only_issue(result, "collection_conflict")
        assert issue["candidate_key"] == "E100"
        assert issue["field"] == "vehicles[]"
        assert issue["affected"]["collection"] == "vehicles"
        assert issue["affected"]["identity_key"] == "tn70ab1234"
        assert issue["affected"]["conflict_fields"] == ["type"]
        assert issue["scope"] == {"collection": "vehicles", "identity_key": "tn70ab1234"}
        assert issue["id"] in e100["issue_ids"]

        options = issue["options"]
        assert len(options) == 2
        for opt in options:
            assert opt["variant_key"] and isinstance(opt["variant_key"], str)
            assert opt["values"]["registration_number"] == "TN70AB1234"
            assert len(opt["sources"]) == 1                             # each variant knows its row
            assert set(opt["sources"][0]) >= {"table_id", "row_number", "original_filename", "sheet_name"}
        assert {o["values"]["type"] for o in options} == {"motorcycle", "scooter"}
        assert {o["sources"][0]["row_number"] for o in options} == {2, 3}
        assert len(issue["affected"]["options_detail"]) == 2
        assert len(issue["affected"]["provenance"]) >= 4                 # 2 rows x 2 mapped item fields

        # The conflicting item is exposed as a conflict (not silently picked); the other is fine.
        bad = _item_by_reg(e100, "TN70AB1234")
        assert bad["status"] == "conflict"
        assert bad["fields"]["type"]["status"] == "conflict"
        assert bad["fields"]["type"]["value"] is None
        assert len(bad["variants"]) == 2
        assert sorted(s["row_number"] for s in bad["sources"]) == [2, 3]
        assert _item_by_reg(e100, "TN70XY9876")["status"] == "resolved"

        # Human chooses the motorcycle variant.
        motorcycle = next(o for o in options if o["values"]["type"] == "motorcycle")
        _persist_issues(db, job, result)
        _resolve(db, issue, {"action": "select", "value": motorcycle["variant_key"], "actor": "human"})

        result2 = prepare(db, job, SCHEMA)
        e100 = _by_key(result2)["E100"]
        assert e100["eligibility"] == "eligible"
        assert [i for i in result2["issues"] if i["issue_type"] == "collection_conflict"] == []
        chosen = _item_by_reg(e100, "TN70AB1234")
        assert chosen["status"] == "resolved"
        assert chosen["fields"]["type"]["value"] == "motorcycle"
        assert chosen["fields"]["type"]["rule"] == "human.select"
        assert chosen["fields"]["registration_number"]["value"] == "TN70AB1234"
        # both contributing rows remain traceable after the selection
        assert sorted(s["row_number"] for s in chosen["sources"]) == [2, 3]
        assert len(_items(e100, "vehicles")) == 2
        # the resolved issue stays resolved (idempotent recompute)
        assert db.get_record_issue(issue["id"])["status"] == "resolved"
    finally:
        db.close()


def test_drop_item_resolution_removes_the_conflicting_item(tmp_path):
    db, job = _new_job(tmp_path)
    try:
        _add_employees(db, job)
        _add_vehicles(db, job, [
            ["E100", "Car", "TN70XY9876"],
            ["E100", "Motorcycle", "TN70AB1234"],
            ["E100", "Scooter", "TN70AB1234"],
        ])
        result = prepare(db, job, SCHEMA)
        issue = _only_issue(result, "collection_conflict")
        _persist_issues(db, job, result)
        _resolve(db, issue, {"action": "drop_item", "actor": "human", "note": "not a real vehicle"})

        result2 = prepare(db, job, SCHEMA)
        e100 = _by_key(result2)["E100"]
        assert e100["eligibility"] == "eligible"
        assert result2["issues"] == []
        vehicles = _items(e100, "vehicles")
        assert [it["identity_key"] for it in vehicles] == ["tn70xy9876"]
        assert result2["metrics"]["collection_items"] == 1
    finally:
        db.close()


# --------------------------------------------------------------------------------------------
# G3.6 — a second child sheet attaches rows to the correct employee only
# --------------------------------------------------------------------------------------------
def test_education_child_sheet_attaches_rows_to_the_right_employee(tmp_path):
    db, job = _new_job(tmp_path)
    try:
        _add_employees(db, job)
        _add_vehicles(db, job, [["E100", "Car", "TN70XY9876"]])
        _add_education(db, job, [
            ["E100", "10th", "ABC School", "92.4", "2014"],
            ["E100", "B.E.", "XYZ College", "8.1", "2020"],
            ["E101", "12th", "PQR School", "88", "2016"],
        ])
        result = prepare(db, job, SCHEMA)
        by_key = _by_key(result)
        assert result["issues"] == []
        assert all(c["eligibility"] == "eligible" for c in by_key.values())

        e100_edu = _items(by_key["E100"], "education_history")
        e101_edu = _items(by_key["E101"], "education_history")
        assert len(e100_edu) == 2
        assert len(e101_edu) == 1
        assert _items(by_key["E102"], "education_history") == []

        assert {it["fields"]["institution"]["value"] for it in e100_edu} == {"ABC School", "XYZ College"}
        assert e101_edu[0]["fields"]["institution"]["value"] == "PQR School"
        assert e101_edu[0]["fields"]["level"]["value"] == "12th"
        assert e101_edu[0]["fields"]["score"]["value"] == "88"
        assert e101_edu[0]["fields"]["year"]["value"] == "2016"          # number kept as canonical digits
        # identity = (level, institution), lower-cased, joined with '|'
        assert {it["identity_key"] for it in e100_edu} == {"10th|abc school", "b.e.|xyz college"}
        assert e101_edu[0]["identity_key"] == "12th|pqr school"
        # collections stay separate: vehicles untouched by the education sheet
        assert len(_items(by_key["E100"], "vehicles")) == 1
        assert "education_history" not in by_key["E102"]["collections"] or \
            by_key["E102"]["collections"]["education_history"] == []
        assert result["metrics"]["child_rows"] == 4
        assert result["metrics"]["collection_items"] == 4
    finally:
        db.close()


# --------------------------------------------------------------------------------------------
# G3.7 — every item field carries full provenance
# --------------------------------------------------------------------------------------------
def test_every_item_field_carries_full_provenance(tmp_path):
    db, job = _new_job(tmp_path)
    try:
        _add_employees(db, job)
        _add_vehicles(db, job, [
            ["E100", "Car", "TN70XY9876"],
            ["E101", "Motorcycle", "KA01CD5678"],
        ])
        _add_education(db, job, [["E102", "MBA", "LMN Institute", "3.7", "2021"]])
        result = prepare(db, job, SCHEMA)
        by_key = _by_key(result)

        checked = 0
        for cand in by_key.values():
            for coll, items in cand["collections"].items():
                for it in items:
                    for key, fv in it["fields"].items():
                        assert fv["provenance"], f"{coll}.{key} has no provenance"
                        for p in fv["provenance"]:
                            assert PROV_KEYS <= set(p), f"missing keys in {p}"
                            assert p["original_filename"] == "hr_details.xlsx"
                            assert p["row_number"] >= 1
                            assert isinstance(p["col_index"], int)
                            assert p["header"] == key                    # headers == item keys here
                            checked += 1
        assert checked == 2 * 2 + 4                                       # 2 vehicles x 2 fields + 1 edu x 4

        car = _item_by_reg(by_key["E100"], "TN70XY9876")
        p = car["fields"]["registration_number"]["provenance"][0]
        assert p == {"table_id": "tbl_veh", "row_number": 1, "original_filename": "hr_details.xlsx",
                     "sheet_name": "Vehicles", "header": "registration_number", "raw": "TN70XY9876",
                     "col_index": 2}
        p_type = car["fields"]["type"]["provenance"][0]
        assert p_type["raw"] == "Car" and p_type["col_index"] == 1 and p_type["header"] == "type"

        edu = _items(by_key["E102"], "education_history")[0]
        p_year = edu["fields"]["year"]["provenance"][0]
        assert p_year["sheet_name"] == "Education" and p_year["row_number"] == 1
        assert p_year["raw"] == "2021" and p_year["col_index"] == 4 and p_year["table_id"] == "tbl_edu"
        # item-level sources point at the exact staged row for the deep link
        assert edu["sources"] == [{"table_id": "tbl_edu", "row_number": 1,
                                   "original_filename": "hr_details.xlsx", "sheet_name": "Education"}]
    finally:
        db.close()


# --------------------------------------------------------------------------------------------
# Orphan child rows are never silently dropped
# --------------------------------------------------------------------------------------------
def test_orphan_child_row_becomes_one_reviewable_issue(tmp_path):
    db, job = _new_job(tmp_path)
    try:
        _add_employees(db, job)
        _add_vehicles(db, job, [
            ["E100", "Car", "TN70XY9876"],
            ["E999", "Car", "MH01ZZ0000"],       # employee key not in the employee table
            ["E998", "Scooter", "MH02ZZ0001"],   # another orphan in the same sheet
        ])
        result = prepare(db, job, SCHEMA)
        by_key = _by_key(result)
        # the orphan key never becomes an employee candidate
        assert set(by_key) == {"E100", "E101", "E102"}
        # the valid row still attached
        assert [it["identity_key"] for it in _items(by_key["E100"], "vehicles")] == ["tn70xy9876"]

        issue = _only_issue(result, "orphan_child_row")               # ONE issue per child table
        assert issue["candidate_key"] is None
        assert issue["options"] == ["exclude"]
        assert issue["scope"] == {"table_id": "tbl_veh", "rows": [2, 3]}
        aff = issue["affected"]
        assert aff["table_id"] == "tbl_veh"
        assert aff["original_filename"] == "hr_details.xlsx" and aff["sheet_name"] == "Vehicles"
        assert aff["row_count"] == 2
        assert [r["employee_id_raw"] for r in aff["rows"]] == ["E999", "E998"]
        assert aff["rows"][0]["collection"] == "vehicles"
        assert aff["rows"][0]["values"] == {"registration_number": "MH01ZZ0000", "type": "car"}
        assert [p["row_number"] for p in aff["provenance"]] == [2, 3]
        assert "silently dropped" in issue["reason"]
        # every source row was processed (nothing vanished)
        assert result["metrics"]["source_rows_processed"] == 6
        assert result["metrics"]["child_rows"] == 3
        # an orphan row blocks the stage (open blocking issue) even though no candidate owns it
        assert result["metrics"]["open_issue_estimate"] == 1
        assert all(c["eligibility"] == "eligible" for c in by_key.values())

        # explicit human exclusion clears it on the next recompute
        _persist_issues(db, job, result)
        _resolve(db, issue, {"action": "exclude", "actor": "human", "note": "terminated staff"})
        result2 = prepare(db, job, SCHEMA)
        assert result2["issues"] == []
        assert [it["identity_key"] for it in _items(_by_key(result2)["E100"], "vehicles")] == ["tn70xy9876"]
    finally:
        db.close()


# --------------------------------------------------------------------------------------------
# Legacy flattened shapes declared by the schema: indexed columns and multi-value columns
# --------------------------------------------------------------------------------------------
def test_indexed_columns_in_employee_table_build_separate_items_and_skip_blank_slots(tmp_path):
    db, job = _new_job(tmp_path)
    try:
        headers = EMP_HEADERS + ["Vehicle 1", "Vehicle 2"]
        mappings = EMP_PATHS + [("vehicles[].registration_number", {"index": 1}),
                                ("vehicles[].registration_number", {"index": 2})]
        rows = [
            ["E100", "Priya Sharma", "priya@example.com", "2020-01-15", "TN01AA1111", "TN02BB2222"],
            ["E101", "Arjun Mehta", "arjun@example.com", "2019-06-01", "KA01CD5678", ""],   # blank slot
            ["E102", "Meera Nair", "meera@example.com", "2021-03-10", "", ""],
        ]
        _add_employees(db, job, rows=rows, headers=headers, mappings=mappings)
        result = prepare(db, job, SCHEMA)
        by_key = _by_key(result)
        assert result["issues"] == []
        assert all(c["eligibility"] == "eligible" for c in by_key.values())

        e100 = _items(by_key["E100"], "vehicles")
        assert [it["identity_key"] for it in e100] == ["tn01aa1111", "tn02bb2222"]
        assert {it["fields"]["registration_number"]["value"] for it in e100} == {"TN01AA1111", "TN02BB2222"}
        # each item points at the exact column it came from
        provs = {it["identity_key"]: it["fields"]["registration_number"]["provenance"][0] for it in e100}
        assert provs["tn01aa1111"]["header"] == "Vehicle 1" and provs["tn01aa1111"]["col_index"] == 4
        assert provs["tn02bb2222"]["header"] == "Vehicle 2" and provs["tn02bb2222"]["col_index"] == 5
        assert provs["tn01aa1111"]["original_filename"] == "employees.csv"
        assert provs["tn01aa1111"]["row_number"] == 1

        e101 = _items(by_key["E101"], "vehicles")
        assert len(e101) == 1                                           # blank "Vehicle 2" is not an item
        assert e101[0]["identity_key"] == "ka01cd5678"
        assert _items(by_key["E102"], "vehicles") == []
        # the employee table is NOT treated as a child table: scalars still land on the record
        assert by_key["E100"]["record"]["full_name"]["value"] == "Priya Sharma"
        assert result["metrics"]["employee_rows"] == 3 and result["metrics"]["child_rows"] == 0
        assert result["metrics"]["collection_items"] == 3
    finally:
        db.close()


def test_multi_value_column_splits_only_on_declared_delimiters(tmp_path):
    db, job = _new_job(tmp_path)
    try:
        headers = EMP_HEADERS + ["Vehicle Numbers"]
        meta = {"multi_value": True, "delimiters": [";", "|"]}
        mappings = EMP_PATHS + [("vehicles[].registration_number", meta)]
        rows = [
            ["E100", "Priya Sharma", "priya@example.com", "2020-01-15", "TN01AA1111; TN02BB2222"],
            ["E101", "Arjun Mehta", "arjun@example.com", "2019-06-01", "KA01CD5678|KA02EF9999"],
            ["E102", "Meera Nair", "meera@example.com", "2021-03-10", "MH01AA1111, MH02BB2222"],  # comma not declared
        ]
        _add_employees(db, job, rows=rows, headers=headers, mappings=mappings)
        result = prepare(db, job, SCHEMA)
        by_key = _by_key(result)
        assert result["issues"] == []

        e100 = _items(by_key["E100"], "vehicles")
        assert [it["fields"]["registration_number"]["value"] for it in e100] == ["TN01AA1111", "TN02BB2222"]
        assert [it["identity_key"] for it in e100] == ["tn01aa1111", "tn02bb2222"]
        # provenance keeps the whole raw cell and the part position
        p1 = e100[0]["fields"]["registration_number"]["provenance"][0]
        assert p1["header"] == "Vehicle Numbers" and p1["col_index"] == 4 and p1["row_number"] == 1
        assert p1["raw"] == "TN01AA1111" and p1["raw_cell"] == "TN01AA1111; TN02BB2222" and p1["part"] == 1
        p2 = e100[1]["fields"]["registration_number"]["provenance"][0]
        assert p2["raw"] == "TN02BB2222" and p2["part"] == 2

        e101 = _items(by_key["E101"], "vehicles")
        assert sorted(it["identity_key"] for it in e101) == ["ka01cd5678", "ka02ef9999"]   # '|' declared

        e102 = _items(by_key["E102"], "vehicles")
        assert len(e102) == 1                                           # ',' is NOT a declared delimiter
        assert e102[0]["fields"]["registration_number"]["value"] == "MH01AA1111, MH02BB2222"
        assert result["metrics"]["collection_items"] == 5
    finally:
        db.close()


# --------------------------------------------------------------------------------------------
# Item validation: required item field missing / unknown item enum, and the 'correct' overlay
# --------------------------------------------------------------------------------------------
def test_required_item_field_missing_is_collection_item_invalid_and_correctable(tmp_path):
    db, job = _new_job(tmp_path)
    try:
        _add_employees(db, job)
        _add_vehicles(db, job, [
            ["E100", "Car", ""],                 # registration_number (required identity) blank
            ["E101", "Car", "KA01CD5678"],
        ])
        result = prepare(db, job, SCHEMA)
        by_key = _by_key(result)
        assert by_key["E100"]["eligibility"] == "blocked"
        assert by_key["E101"]["eligibility"] == "eligible"

        issue = _only_issue(result, "collection_item_invalid")
        assert issue["candidate_key"] == "E100"
        assert issue["field"] == "vehicles[]"
        assert issue["affected"]["collection"] == "vehicles"
        assert issue["affected"]["item"] == {"registration_number": None, "type": "car"}
        assert [o["field"] for o in issue["options"]] == ["registration_number"]
        assert issue["affected"]["problems"][0]["field"] == "registration_number"
        assert "required" in issue["affected"]["problems"][0]["reason"]
        assert issue["affected"]["provenance"]                          # the blank cell is still traceable
        assert issue["id"] in by_key["E100"]["issue_ids"]
        item = _items(by_key["E100"], "vehicles")[0]
        assert item["status"] == "invalid"
        assert item["fields"]["registration_number"]["value"] is None
        assert item["fields"]["type"]["value"] == "car"                 # nothing else was lost
        ident = issue["affected"]["identity_key"]
        assert ident == item["identity_key"]

        # a human supplies the missing registration via a scoped item-field correction
        _persist_issues(db, job, result)
        _resolve(db, issue, {"action": "correct", "value": " TN09ZZ0009 ", "actor": "human",
                             "scope": {"item_field": "registration_number"}})
        result2 = prepare(db, job, SCHEMA)
        e100 = _by_key(result2)["E100"]
        assert e100["eligibility"] == "eligible"
        assert result2["issues"] == []
        fixed = _items(e100, "vehicles")[0]
        assert fixed["status"] == "resolved"
        assert fixed["fields"]["registration_number"]["value"] == "TN09ZZ0009"   # trimmed, verbatim
        assert fixed["fields"]["registration_number"]["rule"] == "human.correction"
        assert fixed["fields"]["type"]["value"] == "car"
    finally:
        db.close()


def test_unknown_item_enum_value_is_collection_item_invalid_with_allowed_values(tmp_path):
    db, job = _new_job(tmp_path)
    try:
        _add_employees(db, job)
        _add_vehicles(db, job, [["E102", "Hovercraft", "GJ01HH0001"]])
        result = prepare(db, job, SCHEMA)
        e102 = _by_key(result)["E102"]
        assert e102["eligibility"] == "blocked"
        issue = _only_issue(result, "collection_item_invalid")
        assert issue["candidate_key"] == "E102"
        assert issue["affected"]["identity_key"] == "gj01hh0001"
        opt = issue["options"][0]
        assert opt["field"] == "type"
        assert opt["allowed"] == ["car", "motorcycle", "scooter", "bicycle", "other"]
        item = _items(e102, "vehicles")[0]
        assert item["status"] == "invalid"
        assert item["fields"]["type"]["status"] == "unresolved"
        assert item["fields"]["type"]["value"] == "Hovercraft"          # raw preserved, never guessed
    finally:
        db.close()


# --------------------------------------------------------------------------------------------
# Normalization by collection item path
# --------------------------------------------------------------------------------------------
def test_normalize_field_by_collection_item_path():
    assert normalize_field("vehicles[].type", "Car", SCHEMA).value == "car"
    assert normalize_field("vehicles[].type", " MOTORCYCLE ", SCHEMA).value == "motorcycle"
    fv = normalize_field("vehicles[].type", "Hovercraft", SCHEMA)
    assert fv.status == "unresolved" and fv.value == "Hovercraft"
    assert normalize_field("vehicles[].registration_number", " TN70XY9876 ", SCHEMA).value == "TN70XY9876"
    assert normalize_field("vehicles[].registration_number", "", SCHEMA).status == "missing"
    assert normalize_field("education_history[].year", "2014", SCHEMA).value == "2014"
    assert normalize_field("education_history[].year", "twenty", SCHEMA).status == "invalid"
    assert normalize_field("dependents[].date_of_birth", "2018-05-10", SCHEMA).value == "2018-05-10"
    assert normalize_field("addresses[].type", "HOME", SCHEMA).value == "home"
    # schema awareness: item paths resolve through the path-aware API
    assert SCHEMA.destination_kind("vehicles[].type") == "COLLECTION_FIELD"
    assert SCHEMA.get_collection("vehicles").item_identity == ("registration_number",)
    assert SCHEMA.get_collection("education_history").item_identity == ("level", "institution")
