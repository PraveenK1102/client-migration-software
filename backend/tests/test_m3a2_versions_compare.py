"""M3A.2 (order G3.8 / G3.9): version + audit compatibility at UNIT level.

Covers ``app.versions.compare_snapshots`` (scalar / collection-item / custom-attribute diff kinds),
``app.prepare.effective_snapshot_from_candidate`` (deterministic item ordering, exclusion of
unresolved items and empty custom values), ``Database.snapshot_hash`` / ``add_employee_version``
(dedup + immutability) and ``app.versions.build_and_store_versions`` driven by hand-built
reconciliation results (existing-target baseline + migration update, NO_CHANGE, REVIEW_REQUIRED,
READY_CREATE, human-decided update) including the ``employee_version`` audit linkage.

Everything here is offline, synthetic and deterministic: no HTTP, no worker, no model.
"""
from __future__ import annotations

import json

import pytest

from app.db import Database
from app.prepare import effective_snapshot_from_candidate, item_identity_key
from app.schema_loader import get_target_schema
from app.versions import build_and_store_versions, compare_snapshots

TENANT = "beta"
TSHIRT_DEF = {"id": "cfd_test_tshirt", "tenant_id": TENANT, "key": "tshirt_size", "label": "T-Shirt Size",
              "type": "enum", "options": ["S", "M", "L"], "required": False, "multi_value": False,
              "description": None, "aliases": ["tee size"]}
BADGE_DEF = {"id": "cfd_test_badge", "tenant_id": TENANT, "key": "badge_colour", "label": "Badge Colour",
             "type": "enum", "options": ["Red", "Blue", "Green"], "required": False, "multi_value": False}
SCHEMA = get_target_schema().with_custom_definitions([TSHIRT_DEF, BADGE_DEF], TENANT)
VEHICLES = SCHEMA.get_collection("vehicles")


# ------------------------------------------------------------------------------------------
# synthetic builders
# ------------------------------------------------------------------------------------------
def _snapshot(scalars: dict | None = None, collections: dict | None = None, custom: list | None = None) -> dict:
    """A complete snapshot shaped like effective_snapshot_from_candidate / reconcile output."""
    out: dict = {f: None for f in SCHEMA.field_names}
    out.update(scalars or {})
    out["collections"] = {c.key: [] for c in SCHEMA.collections}
    for k, v in (collections or {}).items():
        out["collections"][k] = v
    out["custom_attributes"] = list(custom or [])
    return out


def _veh(reg: str, vtype: str | None = "car") -> dict:
    return {"type": vtype, "registration_number": reg}


def _ca(key: str, value, definition_id: str | None = None) -> dict:
    return {"definition_id": definition_id or f"cfd_test_{key}", "key": key, "value": value}


def _prov(header: str, raw, *, row_number: int = 2, table_id: str = "tbl_veh", col_index: int = 1,
          sheet: str | None = "Vehicles", filename: str = "02_hr_details.xlsx") -> dict:
    return {"table_id": table_id, "row_number": row_number, "original_filename": filename,
            "sheet_name": sheet, "header": header, "raw": raw, "col_index": col_index}


def _fv(value, *, status: str = "resolved", rule: str | None = "trim", reason: str | None = None,
        prov: list | None = None) -> dict:
    return {"value": value, "status": status, "rule": rule, "reason": reason, "provenance": prov or []}


def _item(reg: str, vtype: str = "car", *, status: str = "resolved", row_number: int = 2,
          duplicates_collapsed: int = 0, reason: str | None = None) -> dict:
    """A persisted collection item as _cand_to_dict writes it."""
    fields = {
        "registration_number": _fv(reg, rule="trim",
                                   prov=[_prov("Vehicle Number", reg, row_number=row_number, col_index=2)]),
        "type": _fv(vtype, rule="enum", prov=[_prov("Vehicle Type", vtype.title(), row_number=row_number,
                                                     col_index=1)]),
    }
    return {"identity_key": item_identity_key(VEHICLES, {"registration_number": reg}), "status": status,
            "reason": reason, "duplicates_collapsed": duplicates_collapsed, "fields": fields,
            "sources": [{"table_id": "tbl_veh", "row_number": row_number,
                         "original_filename": "02_hr_details.xlsx", "sheet_name": "Vehicles"}],
            "variants": []}


def _cand_row(cid: str, bk: str, *, record: dict | None = None, vehicles: list | None = None,
              custom: list | None = None, eligibility: str = "eligible") -> dict:
    rec = {
        "employee_id": _fv(bk, rule="identity", prov=[_prov("Employee ID", bk, table_id="tbl_emp", col_index=0,
                                                              sheet=None, filename="01_employees.csv")]),
        "full_name": _fv(f"Person {bk}", prov=[_prov("Full Name", f"Person {bk}", table_id="tbl_emp",
                                                       col_index=1, sheet=None, filename="01_employees.csv")]),
        "work_email": _fv(f"{bk.lower()}@example.test", rule="email",
                          prov=[_prov("Work Email", f"{bk}@Example.test", table_id="tbl_emp", col_index=2,
                                      sheet=None, filename="01_employees.csv")]),
        "hire_date": _fv("2021-03-01", rule="date_iso", prov=[_prov("Hire Date", "2021-03-01",
                                                                    table_id="tbl_emp", col_index=3,
                                                                    sheet=None, filename="01_employees.csv")]),
    }
    rec.update(record or {})
    return {"id": cid, "business_key": bk, "eligibility": eligibility, "exclude_reason": None,
            "issue_ids": [], "source_refs": [{"table_id": "tbl_emp", "row_number": 1}], "record": rec,
            "collections": {"vehicles": list(vehicles or [])}, "custom_attributes": list(custom or [])}


def _custom_attr(key: str, value, *, status: str = "resolved", definition_id: str | None = None) -> dict:
    return {"definition_id": definition_id or f"cfd_test_{key}", "key": key, "label": key, "type": "enum",
            **_fv(value, status=status, rule="enum",
                  prov=[_prov(key, value, table_id="tbl_emp", row_number=1, col_index=8, sheet=None,
                              filename="01_employees.csv")])}


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "unit.db")
    try:
        yield d
    finally:
        d.close()


def _job(db: Database) -> str:
    return db.create_job(schema_version=SCHEMA.version, provider="fake", model_id="fake", adapter_kind="fake",
                         tenant_id=TENANT)


def _version_events(db: Database, job: str) -> list[dict]:
    out = []
    for a in db.get_audit(job):
        if a["event_type"] != "employee_version":
            continue
        a = dict(a)
        a["after"] = json.loads(a["after"]) if a["after"] else None
        a["before"] = json.loads(a["before"]) if a["before"] else None
        a["source_ref"] = json.loads(a["source_ref"]) if a["source_ref"] else None
        out.append(a)
    return out


def _by_ident(cmp: dict, coll: str) -> dict[str, dict]:
    return {r["identity_key"]: r for r in cmp["collections"] if r["collection"] == coll}


# ------------------------------------------------------------------------------------------
# compare_snapshots
# ------------------------------------------------------------------------------------------
def test_compare_snapshots_collection_item_added_removed_changed_unchanged():
    a = _snapshot({"employee_id": "E100"}, {"vehicles": [
        _veh("TN70XY9876", "car"),          # unchanged
        _veh("KA01CD5678", "car"),          # changed -> motorcycle
        _veh("OLD0001", "scooter"),         # removed
    ]})
    b = _snapshot({"employee_id": "E100"}, {"vehicles": [
        _veh("KA01CD5678", "motorcycle"),
        _veh("NEW0002", "car"),             # added
        _veh("TN70XY9876", "car"),
    ]})
    cmp = compare_snapshots(a, b, SCHEMA)

    rows = _by_ident(cmp, "vehicles")
    # identity keys are the lowercased registration numbers, one row per identity
    assert set(rows) == {"tn70xy9876", "ka01cd5678", "old0001", "new0002"}

    unchanged = rows["tn70xy9876"]
    assert unchanged["kind"] == "unchanged" and unchanged["changed_fields"] == []
    assert unchanged["a"] == _veh("TN70XY9876", "car") and unchanged["b"] == _veh("TN70XY9876", "car")

    changed = rows["ka01cd5678"]
    assert changed["kind"] == "changed" and changed["changed_fields"] == ["type"]
    assert changed["a"]["type"] == "car" and changed["b"]["type"] == "motorcycle"

    removed = rows["old0001"]
    assert removed["kind"] == "removed" and removed["b"] is None
    assert removed["a"] == _veh("OLD0001", "scooter")
    assert set(removed["changed_fields"]) == {"type", "registration_number"}   # every populated item field

    added = rows["new0002"]
    assert added["kind"] == "added" and added["a"] is None
    assert added["b"] == _veh("NEW0002", "car")
    assert set(added["changed_fields"]) == {"type", "registration_number"}

    assert cmp["changed_collections"] == ["vehicles"]
    # other (empty) collections produce no item rows and are not reported as changed
    assert {r["collection"] for r in cmp["collections"]} == {"vehicles"}
    # scalars are identical -> no scalar / custom changes leak into the collection diff
    assert cmp["changed_fields"] == [] and cmp["changed_custom_attributes"] == []


def test_compare_snapshots_collection_rows_are_sorted_by_identity_and_item_order_is_irrelevant():
    items = [_veh("B2", "car"), _veh("A1", "car"), _veh("C3", "car")]
    a = _snapshot(collections={"vehicles": items})
    b = _snapshot(collections={"vehicles": list(reversed(items))})
    cmp = compare_snapshots(a, b, SCHEMA)
    assert [r["identity_key"] for r in cmp["collections"]] == ["a1", "b2", "c3"]   # deterministic order
    assert all(r["kind"] == "unchanged" for r in cmp["collections"])
    assert cmp["changed_collections"] == []


def test_compare_snapshots_scalar_kinds_changed_added_cleared_unchanged():
    a = _snapshot({"employee_id": "E103", "designation": "Analyst", "grade": "G5", "department": None,
                   "work_email": "e103@example.test"})
    b = _snapshot({"employee_id": "E103", "designation": "Senior Analyst", "grade": "", "department": "Finance",
                   "work_email": "e103@example.test"})
    cmp = compare_snapshots(a, b, SCHEMA)

    assert len(cmp["fields"]) == len(SCHEMA.field_names)           # one row per core scalar, always
    by = {r["field"]: r for r in cmp["fields"]}
    assert by["designation"]["kind"] == "changed" and by["designation"]["changed"] is True
    assert by["designation"]["a"] == "Analyst" and by["designation"]["b"] == "Senior Analyst"
    assert by["department"]["kind"] == "added" and by["department"]["a"] is None and by["department"]["b"] == "Finance"
    assert by["grade"]["kind"] == "cleared" and by["grade"]["a"] == "G5" and by["grade"]["b"] is None  # "" -> None
    assert by["employee_id"]["kind"] == "unchanged" and by["employee_id"]["changed"] is False
    assert by["work_email"]["changed"] is False
    # changed_fields follows the schema's field order
    assert cmp["changed_fields"] == ["designation", "grade", "department"]
    assert cmp["changed_collections"] == [] and cmp["changed_custom_attributes"] == []


def test_compare_snapshots_custom_attributes_added_changed_cleared_unchanged():
    a = _snapshot({"employee_id": "E100"}, custom=[
        _ca("badge_colour", "Blue"), _ca("legacy_code", "LP-1"), _ca("locker", "12"),
    ])
    b = _snapshot({"employee_id": "E100"}, custom=[
        _ca("badge_colour", "Red"),           # changed
        _ca("tshirt_size", "M"),              # added
        _ca("legacy_code", ""),               # cleared ("" normalises to None)
        _ca("locker", "12"),                  # unchanged
    ])
    cmp = compare_snapshots(a, b, SCHEMA)
    by = {r["field"]: r for r in cmp["custom_attributes"]}
    assert set(by) == {"badge_colour", "tshirt_size", "legacy_code", "locker"}
    assert by["badge_colour"]["kind"] == "changed" and by["badge_colour"]["a"] == "Blue" and by["badge_colour"]["b"] == "Red"
    assert by["tshirt_size"]["kind"] == "added" and by["tshirt_size"]["a"] is None and by["tshirt_size"]["b"] == "M"
    assert by["legacy_code"]["kind"] == "cleared" and by["legacy_code"]["b"] is None
    assert by["locker"]["kind"] == "unchanged" and by["locker"]["changed"] is False
    assert cmp["changed_custom_attributes"] == ["badge_colour", "legacy_code", "tshirt_size"]   # sorted keys
    assert cmp["changed_fields"] == [] and cmp["changed_collections"] == []


def test_compare_snapshots_identical_snapshots_report_no_changes():
    s = _snapshot({"employee_id": "E100", "full_name": "Priya"}, {"vehicles": [_veh("TN70AB1234", "motorcycle")]},
                  [_ca("badge_colour", "Blue")])
    cmp = compare_snapshots(s, json.loads(json.dumps(s)), SCHEMA)
    assert cmp["changed_fields"] == [] and cmp["changed_collections"] == [] and cmp["changed_custom_attributes"] == []
    assert all(not r["changed"] for r in cmp["fields"])
    assert all(r["kind"] == "unchanged" for r in cmp["collections"])
    assert all(not r["changed"] for r in cmp["custom_attributes"])


# ------------------------------------------------------------------------------------------
# effective_snapshot_from_candidate + snapshot_hash
# ------------------------------------------------------------------------------------------
def test_effective_snapshot_sorts_items_deterministically_so_hashes_match(db):
    veh_a, veh_b = _item("TN70XY9876", "car", row_number=2), _item("TN70AB1234", "motorcycle", row_number=3)
    ca_x, ca_y = _custom_attr("tshirt_size", "M"), _custom_attr("badge_colour", "Blue")
    r1 = _cand_row("cand_1", "E100", vehicles=[veh_a, veh_b], custom=[ca_x, ca_y])
    r2 = _cand_row("cand_1", "E100", vehicles=[veh_b, veh_a], custom=[ca_y, ca_x])

    s1, s2 = effective_snapshot_from_candidate(r1, SCHEMA), effective_snapshot_from_candidate(r2, SCHEMA)
    assert s1 == s2
    assert Database.snapshot_hash(s1) == Database.snapshot_hash(s2)
    # the effective snapshot orders items deterministically (by canonical JSON) and custom attrs by key
    assert [i["registration_number"] for i in s1["collections"]["vehicles"]] == ["TN70AB1234", "TN70XY9876"]
    assert [c["key"] for c in s1["custom_attributes"]] == ["badge_colour", "tshirt_size"]
    assert s1["custom_attributes"][0] == {"definition_id": "cfd_test_badge_colour", "key": "badge_colour",
                                          "value": "Blue"}

    # Control: the hash itself is order-sensitive -> equality above comes from the snapshot's sorting.
    raw_ab = _snapshot(collections={"vehicles": [_veh("TN70XY9876"), _veh("TN70AB1234", "motorcycle")]})
    raw_ba = _snapshot(collections={"vehicles": [_veh("TN70AB1234", "motorcycle"), _veh("TN70XY9876")]})
    assert Database.snapshot_hash(raw_ab) != Database.snapshot_hash(raw_ba)
    # ...but key order inside dicts does not matter (canonical sort_keys)
    assert Database.snapshot_hash({"b": 1, "a": [1, 2]}) == Database.snapshot_hash({"a": [1, 2], "b": 1})

    # The persisted form (JSON text columns, as get_candidates returns them) yields the same snapshot.
    job = _job(db)
    db.replace_candidates(job, [r1])
    stored = db.get_candidates(job)[0]
    assert isinstance(stored["record"], str) and isinstance(stored["collections"], str)
    assert effective_snapshot_from_candidate(stored, SCHEMA) == s1


def test_effective_snapshot_excludes_unresolved_items_and_empty_custom_values():
    kept = _item("KA05ZZ0002", "car", row_number=2)
    conflict = _item("TN70AB1234", "motorcycle", row_number=3, status="conflict", reason="type differs")
    invalid = _item("BAD-1", "car", row_number=4, status="invalid")
    row = _cand_row("cand_2", "E103",
                    record={"department": _fv("Engineering"),
                            "designation": _fv(None, status="missing", rule=None),
                            "grade": _fv("G7", status="conflict", reason="two values")},
                    vehicles=[kept, conflict, invalid],
                    custom=[_custom_attr("tshirt_size", "M"),
                            _custom_attr("badge_colour", ""),                      # empty value -> dropped
                            _custom_attr("locker", None),                          # None -> dropped
                            _custom_attr("legacy_code", "LP-9", status="invalid")])  # unresolved -> dropped
    snap = effective_snapshot_from_candidate(row, SCHEMA)

    # core scalars: every schema field present; only resolved values carry through
    assert set(snap) == set(SCHEMA.field_names) | {"collections", "custom_attributes"}
    assert snap["employee_id"] == "E103" and snap["department"] == "Engineering"
    assert snap["designation"] is None and snap["grade"] is None          # missing / conflict -> None
    # collections: all declared collections are present, only resolved items survive, plain values only
    assert set(snap["collections"]) == {c.key for c in SCHEMA.collections}
    assert snap["collections"]["vehicles"] == [{"registration_number": "KA05ZZ0002", "type": "car"}]
    assert snap["collections"]["addresses"] == []
    # custom attributes: only resolved + non-empty, reduced to {definition_id, key, value}
    assert snap["custom_attributes"] == [{"definition_id": "cfd_test_tshirt_size", "key": "tshirt_size",
                                          "value": "M"}]


def test_add_employee_version_dedup_returns_none_and_previous_rows_are_immutable(db):
    job, cid = _job(db), "cand_v"
    s1 = _snapshot({"employee_id": "E103", "designation": "Analyst"}, {"vehicles": [_veh("KA05ZZ0001")]})
    s2 = _snapshot({"employee_id": "E103", "designation": "Analyst", "department": "Engineering"},
                   {"vehicles": [_veh("KA05ZZ0001"), _veh("KA05ZZ0002")]}, [_ca("tshirt_size", "M")])

    v1 = db.add_employee_version(job, cid, snapshot=s1, origin="existing_target", created_by="system",
                                 business_key="E103", change_reason="Existing target baseline",
                                 target_revision=3, dedup=True)
    assert v1 is not None and v1["version_no"] == 1 and v1["parent_version_id"] is None
    assert v1["record_hash"] == Database.snapshot_hash(s1) and json.loads(v1["snapshot"]) == s1
    assert v1["target_revision"] == 3 and v1["origin"] == "existing_target"
    v1_row_before = db.get_employee_version(job, cid, 1)
    assert v1_row_before == v1

    # identical snapshot (even via a JSON round-trip) -> dedup -> None, nothing appended
    assert db.add_employee_version(job, cid, snapshot=json.loads(json.dumps(s1)), origin="migration",
                                   created_by="system", dedup=True) is None
    assert [v["version_no"] for v in db.get_employee_versions(job, cid)] == [1]

    # a changed snapshot -> v2 chained to v1
    v2 = db.add_employee_version(job, cid, snapshot=s2, origin="migration", created_by="system",
                                 business_key="E103", change_reason="Migration update",
                                 field_changes=[{"field": "department", "from": None, "to": "Engineering"}],
                                 dedup=True)
    assert v2["version_no"] == 2 and v2["parent_version_id"] == v1["id"]
    assert json.loads(v2["snapshot"]) == s2 and json.loads(v2["field_changes"])[0]["field"] == "department"
    assert [v["version_no"] for v in db.get_employee_versions(job, cid)] == [1, 2]

    # immutability: v1 re-read after v2 was appended is byte-for-byte the same row
    assert db.get_employee_version(job, cid, 1) == v1_row_before
    assert json.loads(db.get_employee_version(job, cid, 1)["snapshot"]) == s1

    # dedup compares against the LATEST version only: re-appending s1 after s2 is a real change (v3)
    v3 = db.add_employee_version(job, cid, snapshot=s1, origin="migration", created_by="system", dedup=True)
    assert v3 is not None and v3["version_no"] == 3 and v3["parent_version_id"] == v2["id"]
    # dedup=False forces an append even for an identical snapshot
    v4 = db.add_employee_version(job, cid, snapshot=s1, origin="migration", created_by="system", dedup=False)
    assert v4["version_no"] == 4 and v4["record_hash"] == v3["record_hash"]
    # versions are scoped per (job, candidate)
    assert db.get_employee_versions(job, "someone_else") == []
    assert db.get_employee_version(job, cid, 99) is None


# ------------------------------------------------------------------------------------------
# build_and_store_versions (hand-built reconciliation results)
# ------------------------------------------------------------------------------------------
def _e103_fixture(db):
    """Prepared candidate E103 (2 vehicles + a custom attr) vs. a target baseline with 1 vehicle."""
    job, cid = _job(db), "cand_e103"
    dept_prov = [_prov("Department", "Engineering ", table_id="tbl_emp", row_number=4, col_index=3, sheet=None,
                       filename="01_employees.csv")]
    cand = _cand_row(cid, "E103",
                     record={"department": _fv("Engineering", rule="trim", prov=dept_prov),
                             "designation": _fv("Analyst")},
                     vehicles=[_item("KA05ZZ0001", "car", row_number=6), _item("KA05ZZ0002", "car", row_number=7)],
                     custom=[_custom_attr("tshirt_size", "M")])
    db.replace_candidates(job, [cand])

    baseline = _snapshot({"employee_id": "E103", "full_name": "Person E103", "work_email": "e103@example.test",
                          "hire_date": "2021-03-01", "designation": "Analyst", "department": None},
                         {"vehicles": [_veh("KA05ZZ0001", "car")]})
    desired = _snapshot({"employee_id": "E103", "full_name": "Person E103", "work_email": "e103@example.test",
                         "hire_date": "2021-03-01", "designation": "Analyst", "department": "Engineering"},
                        {"vehicles": [_veh("KA05ZZ0001", "car"), _veh("KA05ZZ0002", "car")]},
                        [_ca("tshirt_size", "M")])
    diff = {
        "department": {"incoming": "Engineering", "target": None, "status": "update"},
        "designation": {"incoming": "Analyst", "target": "Analyst", "status": "no_change"},
        "custom_attributes.tshirt_size": {"incoming": "M", "target": None, "status": "update"},
        "vehicles[]": {"status": "update", "items": [
            {"identity_key": "ka05zz0001", "status": "no_change", "incoming": _veh("KA05ZZ0001"),
             "target": _veh("KA05ZZ0001")},
            {"identity_key": "ka05zz0002", "status": "update", "incoming": _veh("KA05ZZ0002"), "target": None},
        ]},
    }
    result = {"candidate_id": cid, "business_key": "E103", "outcome": "READY_UPDATE",
              "target_record_id": "tgt_e103", "target_revision": 3, "match_basis": "employee_id",
              "diff": diff, "baseline": baseline, "desired": desired}
    return job, cid, baseline, desired, result, dept_prov


def test_build_and_store_versions_existing_target_baseline_then_migration_update(db):
    job, cid, baseline, desired, result, dept_prov = _e103_fixture(db)

    created = build_and_store_versions(db, job, SCHEMA, [result])
    assert [(v["version_no"], v["origin"]) for v in created] == [(1, "existing_target"), (2, "migration")]

    versions = db.get_employee_versions(job, cid)
    assert [v["version_no"] for v in versions] == [1, 2]
    v1, v2 = versions
    assert json.loads(v1["snapshot"]) == baseline and v1["target_revision"] == 3
    assert v1["created_by"] == "system" and v1["parent_version_id"] is None and v1["business_key"] == "E103"
    assert json.loads(v2["snapshot"]) == desired and v2["parent_version_id"] == v1["id"]
    assert v2["created_by"] == "system" and v2["decision_note"] is None and v2["decision_id"] is None
    assert v2["change_reason"] == "Migration update (safe fill of blank target data)"

    changes = json.loads(v2["field_changes"])
    by_field = {c["field"]: c for c in changes}
    assert set(by_field) == {"department", "custom_attributes.tshirt_size", "vehicles[ka05zz0002]"}

    dept = by_field["department"]
    assert dept["from"] is None and dept["to"] == "Engineering"
    assert dept["provenance"] == dept_prov                       # real provenance from the candidate record
    assert dept["method"] == "normalized"                        # rule-derived, no human decision

    veh = by_field["vehicles[ka05zz0002]"]
    assert veh["field"].startswith("vehicles[") and veh["kind"] == "added"
    assert veh["from"] is None and veh["to"] == _veh("KA05ZZ0002", "car")
    assert set(veh["changed_fields"]) == {"type", "registration_number"}
    assert veh["method"] == "merged" and len(veh["provenance"]) == 2   # one per item field
    assert {p["header"] for p in veh["provenance"]} == {"Vehicle Number", "Vehicle Type"}
    assert all(p["row_number"] == 7 and p["table_id"] == "tbl_veh" for p in veh["provenance"])

    ca = by_field["custom_attributes.tshirt_size"]
    assert ca["from"] is None and ca["to"] == "M" and ca["provenance"] and ca["provenance"][0]["header"] == "tshirt_size"

    # the unchanged vehicle and unchanged scalars are NOT listed as changes
    assert "vehicles[ka05zz0001]" not in by_field and "designation" not in by_field

    # the compare endpoint's underlying function agrees with the stored versions
    cmp = compare_snapshots(json.loads(v1["snapshot"]), json.loads(v2["snapshot"]), SCHEMA)
    assert cmp["changed_fields"] == ["department"] and cmp["changed_collections"] == ["vehicles"]
    assert cmp["changed_custom_attributes"] == ["tshirt_size"]
    assert _by_ident(cmp, "vehicles")["ka05zz0002"]["kind"] == "added"
    assert _by_ident(cmp, "vehicles")["ka05zz0001"]["kind"] == "unchanged"


def test_build_and_store_versions_audit_links_v1_to_v2_and_is_idempotent(db):
    job, cid, _baseline, _desired, result, _ = _e103_fixture(db)
    created = build_and_store_versions(db, job, SCHEMA, [result])
    v1, v2 = created

    events = _version_events(db, job)
    assert len(events) == 2
    # select by version linkage, not by insertion order (two writes may share a timestamp)
    e1 = next(e for e in events if e["after"].get("to_version") == 1)
    e2 = next(e for e in events if e["after"].get("to_version") == 2)
    assert e1["actor"] == "system" and e1["before"] is None
    assert e1["after"] == {"to_version": 1, "origin": "existing_target"}
    assert e1["source_ref"]["candidate_id"] == cid and e1["source_ref"]["version_id"] == v1["id"]
    assert e1["reason"] == "Existing target baseline"

    assert e2["before"] == {"version_no": 1}
    assert e2["after"]["from_version"] == 1 and e2["after"]["to_version"] == 2
    assert e2["after"]["origin"] == "migration" and e2["after"]["decision_note"] is None
    assert set(e2["after"]["changed_fields"]) == {"department", "custom_attributes.tshirt_size",
                                                  "vehicles[ka05zz0002]"}
    assert e2["source_ref"] == {"candidate_id": cid, "business_key": "E103", "field": None,
                                "version_id": v2["id"]}
    assert e2["reason"] == v2["change_reason"]

    # idempotent: same results again -> nothing created, no new versions, no new audit events
    assert build_and_store_versions(db, job, SCHEMA, [result]) == []
    assert [v["version_no"] for v in db.get_employee_versions(job, cid)] == [1, 2]
    assert len(_version_events(db, job)) == 2
    # and the stored rows are untouched by the re-run
    assert db.get_employee_version(job, cid, 1) == v1 and db.get_employee_version(job, cid, 2) == v2


def test_build_and_store_versions_no_change_creates_only_the_baseline(db):
    job, cid = _job(db), "cand_e100"
    db.replace_candidates(job, [_cand_row(cid, "E100", vehicles=[_item("TN70XY9876")])])
    snap = _snapshot({"employee_id": "E100", "full_name": "Person E100", "work_email": "e100@example.test",
                      "hire_date": "2021-03-01"}, {"vehicles": [_veh("TN70XY9876")]})
    result = {"candidate_id": cid, "business_key": "E100", "outcome": "NO_CHANGE", "target_record_id": "tgt_e100",
              "target_revision": 1, "match_basis": "employee_id",
              "diff": {"employee_id": {"incoming": "E100", "target": "E100", "status": "no_change"}},
              "baseline": snap, "desired": json.loads(json.dumps(snap))}

    created = build_and_store_versions(db, job, SCHEMA, [result])
    assert [(v["version_no"], v["origin"]) for v in created] == [(1, "existing_target")]
    assert created[0]["target_revision"] == 1 and created[0]["field_changes"] is None
    events = _version_events(db, job)
    assert len(events) == 1 and events[0]["after"] == {"to_version": 1, "origin": "existing_target"}

    # running again creates nothing new
    assert build_and_store_versions(db, job, SCHEMA, [result]) == []
    assert [v["version_no"] for v in db.get_employee_versions(job, cid)] == [1]
    assert len(_version_events(db, job)) == 1


def test_build_and_store_versions_skips_review_required_and_excluded(db):
    job = _job(db)
    db.replace_candidates(job, [_cand_row("cand_r", "E200"), _cand_row("cand_x", "E400", eligibility="excluded")])
    baseline = _snapshot({"employee_id": "E200", "department": "Finance"})
    results = [
        {"candidate_id": "cand_r", "business_key": "E200", "outcome": "REVIEW_REQUIRED", "target_record_id": "t",
         "target_revision": 1, "match_basis": "employee_id",
         "diff": {"department": {"incoming": "Sales", "target": "Finance", "status": "conflict"}},
         "baseline": baseline, "desired": None},
        {"candidate_id": "cand_x", "business_key": "E400", "outcome": "EXCLUDED", "target_record_id": None,
         "target_revision": None, "match_basis": "none", "diff": {}, "baseline": None, "desired": None},
    ]
    assert build_and_store_versions(db, job, SCHEMA, results) == []
    assert db.get_employee_versions(job, "cand_r") == [] and db.get_employee_versions(job, "cand_x") == []
    assert _version_events(db, job) == []


def test_build_and_store_versions_new_employee_starts_at_v1_then_updates(db):
    job, cid = _job(db), "cand_new"
    db.replace_candidates(job, [_cand_row(cid, "E900", record={"department": _fv("Sales")},
                                          vehicles=[_item("MH12AB0001", "car")])])
    desired = _snapshot({"employee_id": "E900", "full_name": "Person E900", "work_email": "e900@example.test",
                         "hire_date": "2021-03-01"}, {"vehicles": [_veh("MH12AB0001")]})
    result = {"candidate_id": cid, "business_key": "E900", "outcome": "READY_CREATE", "target_record_id": None,
              "target_revision": None, "match_basis": "none", "diff": {}, "baseline": None, "desired": desired}

    created = build_and_store_versions(db, job, SCHEMA, [result])
    assert [(v["version_no"], v["origin"]) for v in created] == [(1, "migration")]       # no fake v0
    v1 = created[0]
    assert v1["parent_version_id"] is None and v1["field_changes"] is None and v1["target_revision"] is None
    assert v1["change_reason"] == "Initial canonical migration state" and json.loads(v1["snapshot"]) == desired
    ev = _version_events(db, job)
    assert len(ev) == 1 and ev[0]["after"] == {"to_version": 1, "origin": "migration"} and ev[0]["before"] is None

    # unchanged desired state -> nothing new
    assert build_and_store_versions(db, job, SCHEMA, [result]) == []
    assert len(db.get_employee_versions(job, cid)) == 1

    # a later recompute with a changed canonical state -> v2 chained to v1 with real field changes
    desired2 = json.loads(json.dumps(desired))
    desired2["department"] = "Sales"
    created2 = build_and_store_versions(db, job, SCHEMA, [dict(result, desired=desired2)])
    assert [(v["version_no"], v["origin"]) for v in created2] == [(2, "migration")]
    v2 = created2[0]
    assert v2["parent_version_id"] == v1["id"] and v2["change_reason"] == "Canonical migration state updated"
    changes = json.loads(v2["field_changes"])
    assert [c["field"] for c in changes] == ["department"]
    assert changes[0]["from"] is None and changes[0]["to"] == "Sales" and changes[0]["method"] == "normalized"
    ev2 = [e for e in _version_events(db, job) if e["after"].get("from_version")]
    assert len(ev2) == 1 and ev2[0]["after"]["from_version"] == 1 and ev2[0]["after"]["to_version"] == 2
    assert ev2[0]["after"]["changed_fields"] == ["department"]
    assert ev2[0]["source_ref"]["field"] == "department"      # single change -> field is named on the event
    # v1 remains immutable
    assert db.get_employee_version(job, cid, 1) == v1


def test_build_and_store_versions_human_decision_yields_human_origin_with_note(db):
    job, cid = _job(db), "cand_h"
    dept_prov = [_prov("Dept", "Finance", table_id="tbl_emp", row_number=2, col_index=3, sheet=None,
                       filename="01_employees.csv")]
    db.replace_candidates(job, [_cand_row(cid, "E100", record={"department": _fv("Finance", prov=dept_prov)})])
    # a resolved target-review decision (as the API would store it) carries the note + decision id
    issue_id = "tri_test_dept"
    db.upsert_target_review_issue(job, issue_id=issue_id, candidate_id=cid, business_key="E100", field="department",
                                  issue_type="value_conflict", reason="differs", incoming_value="Finance",
                                  target_value="Engineering", match_basis="employee_id",
                                  options=["keep_existing", "use_incoming", "exclude"],
                                  affected={"candidate_id": cid, "field": "department"})
    outcome, _enq = db.resolve_target_issue_and_enqueue(job, issue_id, expected_version=1,
                                                        resolution={"action": "use_incoming",
                                                                    "note": "client confirmed dept"})
    assert outcome == "resolved"

    base = {"employee_id": "E100", "full_name": "Person E100", "work_email": "e100@example.test",
            "hire_date": "2021-03-01"}
    baseline = _snapshot({**base, "department": "Engineering"})
    desired = _snapshot({**base, "department": "Finance"})
    result = {"candidate_id": cid, "business_key": "E100", "outcome": "READY_UPDATE", "target_record_id": "tgt_e100",
              "target_revision": 2, "match_basis": "employee_id",
              "diff": {"department": {"incoming": "Finance", "target": "Engineering", "status": "update",
                                      "decision": "use_incoming"}},
              "baseline": baseline, "desired": desired}
    created = build_and_store_versions(db, job, SCHEMA, [result])
    assert [(v["version_no"], v["origin"]) for v in created] == [(1, "existing_target"), (2, "human")]
    v2 = created[1]
    assert v2["created_by"] == "human" and v2["decision_note"] == "client confirmed dept"
    assert v2["decision_id"] == issue_id and v2["change_reason"] == "Human-approved migration change"
    ch = json.loads(v2["field_changes"])
    assert len(ch) == 1 and ch[0]["field"] == "department" and ch[0]["from"] == "Engineering" and ch[0]["to"] == "Finance"
    assert ch[0]["method"] == "human (use_incoming)" and ch[0]["provenance"] == dept_prov
    ev = [e for e in _version_events(db, job) if e["after"].get("from_version") == 1]
    assert len(ev) == 1 and ev[0]["actor"] == "human" and ev[0]["after"]["decision_note"] == "client confirmed dept"
    assert ev[0]["after"]["origin"] == "human" and ev[0]["after"]["changed_fields"] == ["department"]
    # the human target decision itself is a separate audit event (audit system preserved)
    assert any(a["event_type"] == "target_decision" and a["issue_id"] == issue_id for a in db.get_audit(job))
