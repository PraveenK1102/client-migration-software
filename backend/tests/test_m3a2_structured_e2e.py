"""M3A.2 structured-demo flow over HTTP, NO-PROVIDER configuration (LLM_PROVIDER=groq, no key).

Drives the synthetic tenant-beta fixture pack (sample-data/structured-demo: an employee CSV plus an
xlsx with five child sheets) through the real single-node app:

    upload -> blocked_provider (2 custom-field proposals) -> approve + ignore -> MAP re-run ->
    PREPARE -> awaiting_record_review (one collection_conflict) -> select variant ->
    preparation_complete -> reconcile -> reconciliation_complete -> versions / compare / audit /
    prepared dataset / source deep-link -> idempotent re-reconcile.

Everything is offline: the provider is *unconfigured on purpose* (that is the path under test), the
mock target runs in-process, and every value asserted below is synthetic fixture data. The flow runs
ONCE per module (module-scoped fixture) and each test asserts on the recorded stage snapshots plus
read-only follow-up GETs against the still-open client.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.custom_fields import proposal_id
from app.main import create_app

DEMO = Path(__file__).resolve().parent.parent.parent / "sample-data" / "structured-demo"
CSV, XLSX = "01_employees.csv", "02_hr_details.xlsx"
XLSX_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CANON = "employee_id,full_name,work_email,department,hire_date,contract_start_date\n"

EXPECTED_ROLES = {
    None: "employee",
    "Vehicles": "child:vehicles",
    "Education": "child:education_history",
    "Emergency Contacts": "child:emergency_contacts",
    "Dependents": "child:dependents",
    "Addresses": "child:addresses",
}
PROVENANCE_KEYS = {"original_filename", "sheet_name", "row_number", "header", "raw"}


# --------------------------------------------------------------------------- helpers
def _poll(c, jid, want, tries=600, delay=0.03):
    for _ in range(tries):
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] in want:
            return j
        time.sleep(delay)
    return c.get(f"/api/jobs/{jid}").json()


def _wait_stage_items_settled(c, jid, kind, expected_count, tries=600, delay=0.03):
    """Wait until `expected_count` work items of `kind` exist and none is still active."""
    for _ in range(tries):
        items = [w for w in c.get(f"/api/jobs/{jid}/work-items").json() if w["kind"] == kind]
        active = [w for w in items if w["status"] in ("pending", "processing", "retryable")]
        if len(items) >= expected_count and not active:
            return items
        time.sleep(delay)
    return [w for w in c.get(f"/api/jobs/{jid}/work-items").json() if w["kind"] == kind]


def _versions(c, jid, cid):
    return c.get(f"/api/jobs/{jid}/candidates/{cid}/versions").json()


def _by_key(cands):
    return {x["business_key"]: x for x in cands}


def _vehicle_items(cand):
    return {it["fields"]["registration_number"]["value"]: it
            for it in cand["collections"].get("vehicles", [])}


def _run_flow(c) -> SimpleNamespace:
    f = SimpleNamespace()
    f.tenants = c.get("/api/tenants").json()
    f.beta_fields_before = c.get("/api/tenants/beta/custom-fields").json()

    files = [("files", (CSV, (DEMO / CSV).read_bytes(), "text/csv")),
             ("files", (XLSX, (DEMO / XLSX).read_bytes(), XLSX_CT))]
    r = c.post("/api/jobs", files=files, data={"tenant_id": "beta"})
    assert r.status_code == 200, r.text
    jid = f.jid = r.json()["id"]

    # --- stage 1: ingest + rules-only mapping, blocked on the missing provider -------------
    f.job_blocked = _poll(c, jid, {"blocked_provider", "awaiting_review", "mapping_complete",
                                   "awaiting_record_review", "preparation_complete", "error"})
    f.files = c.get(f"/api/jobs/{jid}/files").json()
    f.profiles = c.get(f"/api/jobs/{jid}/profiles").json()
    f.mappings_blocked = c.get(f"/api/jobs/{jid}/mappings").json()
    f.proposals_open = c.get(f"/api/jobs/{jid}/custom-field-proposals").json()

    # --- stage 2: human decisions on the two proposals ---------------------------------------
    ts = next((p for p in f.proposals_open if p["source_header"] == "T-Shirt Size"), None)
    lg = next((p for p in f.proposals_open if p["source_header"] == "Legacy Payroll Code"), None)
    assert ts and lg, [p["source_header"] for p in f.proposals_open]
    f.ts_proposal, f.lg_proposal = ts, lg
    r = c.post(f"/api/jobs/{jid}/custom-field-proposals/{ts['id']}/decision",
               json={"version": ts["version"], "action": "approve", "note": "confirmed with Beta HR"})
    f.approve_status, f.approve = r.status_code, r.json()
    r = c.post(f"/api/jobs/{jid}/custom-field-proposals/{lg['id']}/decision",
               json={"version": lg["version"], "action": "ignore", "reason": "legacy system reference",
                     "note": "not needed in the target"})
    f.ignore_status, f.ignore = r.status_code, r.json()
    f.beta_fields_after = c.get("/api/tenants/beta/custom-fields").json()

    # --- stage 3: MAP re-run -> PREPARE -> record review ------------------------------------
    f.job_review = _poll(c, jid, {"awaiting_record_review", "preparation_complete", "error"})
    f.mappings_after = c.get(f"/api/jobs/{jid}/mappings").json()
    f.proposals_all = c.get(f"/api/jobs/{jid}/custom-field-proposals?status=all").json()
    f.record_issues = c.get(f"/api/jobs/{jid}/record-reviews").json()
    conflict = next((i for i in f.record_issues if i["issue_type"] == "collection_conflict"), None)
    f.conflict = conflict
    f.select_status, f.select = None, None
    if conflict is not None:
        vk = next((o["variant_key"] for o in conflict["options"]
                   if isinstance(o, dict) and (o.get("values") or {}).get("type") == "motorcycle"), None)
        f.motorcycle_variant = vk
        if vk is not None:
            r = c.post(f"/api/jobs/{jid}/record-reviews/{conflict['id']}/decision",
                       json={"version": conflict["version"], "action": "select", "value": vk,
                             "note": "confirmed: it is a motorcycle"})
            f.select_status, f.select = r.status_code, r.json()

    # --- stage 4: preparation complete -------------------------------------------------------
    f.job_prepared = _poll(c, jid, {"preparation_complete", "error"})
    f.candidates = _by_key(c.get(f"/api/jobs/{jid}/candidates").json())
    f.versions_before_reconcile = {bk: _versions(c, jid, cd["id"]) for bk, cd in f.candidates.items()}
    f.dataset = c.get(f"/api/jobs/{jid}/prepared-dataset").json()

    # --- stage 5: reconcile against the (in-process) mock target ----------------------------
    r = c.post(f"/api/jobs/{jid}/reconcile")
    f.reconcile_post_status = r.status_code
    f.job_reconciled = _poll(c, jid, {"reconciliation_complete", "awaiting_target_review", "error"})
    f.target_reviews = c.get(f"/api/jobs/{jid}/target-reviews").json()
    f.reconciliation = c.get(f"/api/jobs/{jid}/reconciliation").json()
    f.versions = {bk: _versions(c, jid, cd["id"]) for bk, cd in f.candidates.items()}
    e103 = f.candidates.get("E103")
    f.compare_e103 = (c.get(f"/api/jobs/{jid}/candidates/{e103['id']}/versions/compare?a=1&b=2").json()
                      if e103 else None)

    # --- stage 6: idempotent re-run (wait for the SECOND TARGET_RECONCILE item to finish) -----
    r = c.post(f"/api/jobs/{jid}/reconcile")
    f.rereconcile_post_status = r.status_code
    f.reconcile_items = _wait_stage_items_settled(c, jid, "TARGET_RECONCILE", expected_count=2)
    f.job_rereconciled = _poll(c, jid, {"reconciliation_complete", "awaiting_target_review", "error"})
    f.versions_after_rerun = {bk: _versions(c, jid, cd["id"]) for bk, cd in f.candidates.items()}

    f.audit = c.get(f"/api/jobs/{jid}/audit").json()
    return f


@pytest.fixture(scope="module")
def flow(tmp_path_factory):
    """NO-PROVIDER settings (provider=groq, key absent) + isolated DATA_DIR, one flow per module."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("LLM_PROVIDER", "groq")
        mp.delenv("GROQ_API_KEY", raising=False)
        # An EMPTY process-env value shadows any non-empty dotenv value, so this test can never reach
        # a live provider even on a machine whose backend/.env holds a real key.
        mp.setenv("GROQ_API_KEY", "")
        mp.setenv("DATA_DIR", str(tmp_path_factory.mktemp("m3a2_data")))
        mp.setenv("AUTO_CONTINUE", "false")
        from app import config
        config.get_settings.cache_clear()
        settings = config.get_settings()
        settings.ensure_dirs()
        assert settings.llm_provider == "groq" and settings.groq_key is None
        try:
            with TestClient(create_app()) as c:
                f = _run_flow(c)
                f.client = c
                yield f
        finally:
            config.get_settings.cache_clear()


# =========================================================================== stage 1
def test_tenant_beta_is_seeded_with_badge_colour_only(flow):
    assert "beta" in {t["id"] for t in flow.tenants}
    assert [d["key"] for d in flow.beta_fields_before] == ["badge_colour"]


def test_job_is_tenant_scoped_and_blocked_on_missing_provider(flow):
    j = flow.job_blocked
    assert j["status"] == "blocked_provider", (j["status"], j["error"])
    assert j["tenant_id"] == "beta"
    assert j["counts"]["files"] == 2 and j["counts"]["files_parsed"] == 2
    assert j["counts"]["open_proposals"] == 2


def test_two_files_stored_and_parsed(flow):
    assert len(flow.files) == 2
    assert {x["original_filename"] for x in flow.files} == {CSV, XLSX}
    for x in flow.files:
        assert x["parse_status"] == "parsed" and x["storage_status"] == "stored"
        assert x["sha256"] and len(x["sha256"]) == 64


def test_six_tables_with_roles_and_twenty_rows(flow):
    tables = flow.profiles["tables"]
    assert len(tables) == 6
    assert {t["sheet_name"]: t["table_role"] for t in tables} == EXPECTED_ROLES
    assert sum(t["n_rows"] for t in tables) == 20
    csv_table = next(t for t in tables if t["sheet_name"] is None)
    assert csv_table["original_filename"] == CSV and csv_table["n_rows"] == 4
    for t in tables:
        if t["sheet_name"] is not None:
            assert t["original_filename"] == XLSX


def test_mapping_counts_after_upload(flow):
    counts = flow.mappings_blocked["counts"]
    assert counts["accepted"] == 32
    assert counts["proposals"] == 2
    assert counts["core"] == 13
    assert counts["collection"] == 18
    assert counts["custom"] == 1
    assert counts["needs_review"] == 0 and counts["unmapped"] == 0 and counts["ignored"] == 0
    assert set(flow.mappings_blocked) >= {"accepted", "unresolved", "ignored", "proposals", "counts"}


def test_mapping_rows_carry_destination_kinds(flow):
    m = flow.mappings_blocked
    kinds = {r["destination_kind"] for r in m["accepted"]}
    assert kinds == {"CORE_FIELD", "COLLECTION_FIELD", "CUSTOM_FIELD"}
    badge = next(r for r in m["accepted"] if r["source_header"] == "Badge Colour")
    assert badge["destination_kind"] == "CUSTOM_FIELD"
    assert badge["target_field"] == "custom_attributes.badge_colour"
    assert badge["custom_definition_id"]
    reg = next(r for r in m["accepted"] if r["source_header"] == "Registration Number")
    assert reg["destination_kind"] == "COLLECTION_FIELD"
    assert reg["target_field"] == "vehicles[].registration_number"
    assert {r["destination_kind"] for r in m["proposals"]} == {"PROPOSAL"}
    assert {r["source_header"] for r in m["proposals"]} == {"T-Shirt Size", "Legacy Payroll Code"}
    for r in m["accepted"] + m["proposals"]:
        assert {"profile_id", "table_id", "destination_kind", "path_meta", "custom_definition_id",
                "proposal_id"} <= set(r)


def test_two_open_deterministic_proposals(flow):
    props = flow.proposals_open
    assert {p["source_header"] for p in props} == {"T-Shirt Size", "Legacy Payroll Code"}
    for p in props:
        assert p["status"] == "open" and p["tenant_id"] == "beta" and p["origin"] == "no_provider"
        assert p["id"] == proposal_id(flow.jid, p["profile_id"])
    ts = flow.ts_proposal
    assert ts["suggestion"] == {"key": "tshirt_size", "label": "T-Shirt Size", "type": "enum",
                                "options": ["L", "M", "XL"], "multi_value": False, "required": False,
                                "path": "custom_attributes.tshirt_size"}
    assert set(ts["observed_values"]) == {"M", "L", "XL"}
    lg = flow.lg_proposal
    assert lg["suggestion"]["type"] == "string" and lg["suggestion"]["key"] == "legacy_payroll_code"
    assert lg["non_empty_count"] == 4
    # the proposal points at the real profiled CSV column
    csv_table = next(t for t in flow.profiles["tables"] if t["sheet_name"] is None)
    headers_by_profile = {p["profile_id"]: p["header"] for p in csv_table["profiles"]}
    assert headers_by_profile[ts["profile_id"]] == "T-Shirt Size"
    assert ts["table_id"] == csv_table["table_id"]


# =========================================================================== stage 2
def test_approve_creates_tenant_definition_without_remap_yet(flow):
    assert flow.approve_status == 200, flow.approve
    a = flow.approve
    assert a["outcome"] == "resolved"
    assert a["definition"]["key"] == "tshirt_size" and a["definition"]["tenant_id"] == "beta"
    assert a["definition"]["type"] == "enum" and a["definition"]["options"] == ["L", "M", "XL"]
    assert a["proposal"]["status"] == "approved" and a["proposal"]["definition_id"] == a["definition"]["id"]
    assert a["remap_triggered"] is False and a["open_proposals_remaining"] == 1


def test_ignore_last_proposal_triggers_remap(flow):
    assert flow.ignore_status == 200, flow.ignore
    i = flow.ignore
    assert i["outcome"] == "resolved" and i["definition"] is None
    assert i["proposal"]["status"] == "ignored"
    assert i["remap_triggered"] is True and i["remap_status"] == "queued"
    assert i["open_proposals_remaining"] == 0


def test_tenant_beta_now_lists_tshirt_size(flow):
    keys = [d["key"] for d in flow.beta_fields_after]
    assert set(keys) == {"badge_colour", "tshirt_size"}
    ts = next(d for d in flow.beta_fields_after if d["key"] == "tshirt_size")
    assert ts["type"] == "enum" and ts["options"] == ["L", "M", "XL"]


# =========================================================================== stage 3
def test_job_reaches_record_review_with_updated_mapping_counts(flow):
    j = flow.job_review
    assert j["status"] == "awaiting_record_review", (j["status"], j["error"])
    counts = flow.mappings_after["counts"]
    assert counts == {"accepted": 33, "needs_review": 0, "unmapped": 0, "ignored": 1, "proposals": 0,
                      "core": 13, "collection": 18, "custom": 2}
    ts_row = next(r for r in flow.mappings_after["accepted"] if r["source_header"] == "T-Shirt Size")
    assert ts_row["destination_kind"] == "CUSTOM_FIELD"
    assert ts_row["target_field"] == "custom_attributes.tshirt_size"
    assert ts_row["actor"] == "human" and ts_row["custom_definition_id"]
    ign = flow.mappings_after["ignored"]
    assert len(ign) == 1 and ign[0]["source_header"] == "Legacy Payroll Code"
    assert ign[0]["destination_kind"] == "IGNORED" and ign[0]["target_field"] is None
    assert {p["status"] for p in flow.proposals_all} == {"approved", "ignored"}


def test_exactly_one_collection_conflict_for_e100(flow):
    assert len(flow.record_issues) == 1, [(i["issue_type"], i["candidate_key"]) for i in flow.record_issues]
    iss = flow.record_issues[0]
    assert iss["issue_type"] == "collection_conflict"
    assert iss["candidate_key"] == "E100" and iss["field"] == "vehicles[]" and iss["status"] == "open"
    assert len(iss["options"]) == 2
    types = set()
    for o in iss["options"]:
        assert o["variant_key"] and o["values"]["registration_number"] == "TN70AB1234"
        types.add(o["values"]["type"])
        assert o["sources"], "each variant must be source-backed"
        for s in o["sources"]:
            assert s["original_filename"] == XLSX and s["sheet_name"] == "Vehicles"
            assert s["table_id"] and isinstance(s["row_number"], int)
    assert types == {"motorcycle", "scooter"}
    aff = iss["affected"]
    assert aff["collection"] == "vehicles" and aff["identity_key"] == "tn70ab1234"
    assert aff["conflict_fields"] == ["type"]
    assert len(aff["options_detail"]) == 2 and aff["provenance"]
    assert iss["scope"] == {"collection": "vehicles", "identity_key": "tn70ab1234"}


def test_selecting_motorcycle_variant_resumes_preparation(flow):
    assert flow.motorcycle_variant
    assert flow.select_status == 200, flow.select
    s = flow.select
    assert s["outcome"] == "resolved" and s["resume_triggered"] is True
    assert s["open_record_issues_remaining"] == 0
    assert s["issue"]["status"] == "resolved"
    assert s["issue"]["resolution"]["action"] == "select"
    assert s["issue"]["resolution"]["value"] == flow.motorcycle_variant


# =========================================================================== stage 4
def test_preparation_complete_with_four_eligible(flow):
    j = flow.job_prepared
    assert j["status"] == "preparation_complete", (j["status"], j["error"])
    counts = j["prep_summary"]["counts"]
    assert counts["source_rows_processed"] == 20
    assert counts["candidate_employees"] == 4 and counts["eligible"] == 4
    assert counts["blocked"] == 0 and counts["excluded"] == 0
    assert set(flow.candidates) == {"E100", "E101", "E102", "E103"}
    assert all(cd["eligibility"] == "eligible" for cd in flow.candidates.values())


def test_e100_collections(flow):
    e100 = flow.candidates["E100"]
    assert {k: len(v) for k, v in e100["collections"].items()} == {
        "vehicles": 2, "education_history": 2, "emergency_contacts": 1, "dependents": 1, "addresses": 1}
    veh = _vehicle_items(e100)
    assert set(veh) == {"TN70AB1234", "TN70XY9876"}
    moto = veh["TN70AB1234"]
    assert moto["fields"]["type"]["value"] == "motorcycle" and moto["status"] == "resolved"
    assert moto["fields"]["type"]["rule"] == "human.select"
    assert moto["duplicates_collapsed"] == 0 and len(moto["sources"]) == 2
    assert moto["identity_key"] == "tn70ab1234"
    car = veh["TN70XY9876"]
    assert car["fields"]["type"]["value"] == "car" and len(car["sources"]) == 1


def test_e100_education_provenance_points_at_education_sheet(flow):
    edu = flow.candidates["E100"]["collections"]["education_history"]
    pairs = {(it["fields"]["level"]["value"], it["fields"]["institution"]["value"]) for it in edu}
    assert pairs == {("10th", "ABC School"), ("B.E.", "XYZ College")}
    for it in edu:
        for fname, header in (("level", "Level"), ("institution", "Institution")):
            prov = it["fields"][fname]["provenance"]
            assert prov, fname
            for p in prov:
                assert p["sheet_name"] == "Education" and p["original_filename"] == XLSX
                assert p["header"] == header
        for s in it["sources"]:
            assert s["sheet_name"] == "Education"
    raws = {p["raw"] for it in edu for p in it["fields"]["level"]["provenance"]}
    assert raws == {"10th", "B.E."}


def test_e101_exact_duplicate_vehicle_collapsed(flow):
    veh = _vehicle_items(flow.candidates["E101"])
    assert set(veh) == {"KA01CD5678"}
    it = veh["KA01CD5678"]
    assert it["duplicates_collapsed"] == 1 and len(it["sources"]) == 2
    assert it["status"] == "resolved" and it["fields"]["type"]["value"] == "car"
    assert {s["row_number"] for s in it["sources"]} == {4, 5}


def test_e102_has_no_vehicles_and_e103_has_one(flow):
    assert flow.candidates["E102"]["collections"].get("vehicles", []) == []
    assert {k: len(v) for k, v in flow.candidates["E102"]["collections"].items()} == {
        "education_history": 1, "emergency_contacts": 1}
    assert set(_vehicle_items(flow.candidates["E103"])) == {"KA05ZZ0002"}


def test_every_item_field_provenance_is_complete(flow):
    seen = 0
    for bk, cd in flow.candidates.items():
        for coll, items in cd["collections"].items():
            for it in items:
                assert it["sources"], (bk, coll)
                for s in it["sources"]:
                    assert {"table_id", "row_number", "original_filename", "sheet_name"} <= set(s)
                for fname, fv in it["fields"].items():
                    for p in fv["provenance"]:
                        seen += 1
                        assert PROVENANCE_KEYS <= set(p), (bk, coll, fname, p)
                        assert p["original_filename"] == XLSX
                        assert p["sheet_name"] and isinstance(p["row_number"], int) and p["header"]
                        assert "table_id" in p and "col_index" in p
    assert seen > 0


def test_custom_attributes_per_candidate(flow):
    expected = {"E100": ("Blue", "M"), "E101": ("Red", "L"), "E102": ("Green", "XL"), "E103": ("Blue", "M")}
    for bk, (badge, size) in expected.items():
        attrs = {a["key"]: a for a in flow.candidates[bk]["custom_attributes"]}
        assert {"badge_colour", "tshirt_size"} <= set(attrs), bk
        assert attrs["badge_colour"]["value"] == badge and attrs["tshirt_size"]["value"] == size
        for a in attrs.values():
            assert a["definition_id"] and a["status"] == "resolved" and a["provenance"]
        # the ignored column never reaches the record
        assert "legacy_payroll_code" not in attrs


def test_no_employee_versions_exist_before_reconciliation(flow):
    assert all(v == [] for v in flow.versions_before_reconcile.values()), flow.versions_before_reconcile


def test_prepared_dataset_is_nested_and_ready(flow):
    ds = flow.dataset
    assert ds["ready_for_target"] == 4 and ds["tenant_id"] == "beta" and ds["excluded"] == []
    emps = {e["employee_id"]: e for e in ds["employees"]}
    assert set(emps) == {"E100", "E101", "E102", "E103"}
    for e in emps.values():
        assert isinstance(e["collections"], dict) and isinstance(e["custom_attributes"], list)
        assert {"full_name", "work_email", "hire_date"} <= set(e)
        assert {a["key"] for a in e["custom_attributes"]} == {"badge_colour", "tshirt_size"}
        for a in e["custom_attributes"]:
            assert {"definition_id", "key", "value"} <= set(a)
    e100 = emps["E100"]
    assert {v["registration_number"] for v in e100["collections"]["vehicles"]} == {"TN70AB1234", "TN70XY9876"}
    assert next(v for v in e100["collections"]["vehicles"]
                if v["registration_number"] == "TN70AB1234")["type"] == "motorcycle"
    assert len(e100["collections"]["education_history"]) == 2
    assert emps["E102"]["collections"]["vehicles"] == []


# =========================================================================== stage 5
def test_reconciliation_outcomes(flow):
    assert flow.reconcile_post_status == 200
    j = flow.job_reconciled
    assert j["status"] == "reconciliation_complete", (j["status"], j["error"], flow.target_reviews)
    assert flow.target_reviews == []
    results = {r["business_key"]: r for r in flow.reconciliation["results"]}
    assert {bk: r["outcome"] for bk, r in results.items()} == {
        "E100": "READY_CREATE", "E101": "READY_CREATE", "E102": "READY_CREATE", "E103": "READY_UPDATE"}
    e103 = results["E103"]
    assert e103["target_record_id"] == "E103" and e103["target_revision"] == 1
    diff = e103["diff"]
    assert diff["vehicles[]"]["status"] == "update"
    items = {i["identity_key"]: i for i in diff["vehicles[]"]["items"]}
    assert items["ka05zz0002"]["status"] == "update" and items["ka05zz0002"]["target"] is None
    assert items["ka05zz0002"]["incoming"]["registration_number"] == "KA05ZZ0002"
    assert "custom_attributes.badge_colour" in diff and "custom_attributes.tshirt_size" in diff
    assert diff["department"]["status"] == "update" and diff["department"]["incoming"] == "Sales"
    counts = flow.reconciliation["counts"]
    assert counts["ready_create"] == 3 and counts["ready_update"] == 1 and counts["prepared"] == 4


def test_e103_has_target_baseline_v1_and_migration_v2(flow):
    v = flow.versions["E103"]
    assert [(x["version_no"], x["origin"]) for x in v] == [(2, "migration"), (1, "existing_target")]
    v1 = next(x for x in v if x["version_no"] == 1)
    v2 = next(x for x in v if x["version_no"] == 2)
    assert v1["target_revision"] == 1 and v1["parent_version_id"] is None and v1["is_current"] is False
    assert len(v1["snapshot"]["collections"]["vehicles"]) == 1
    assert v1["snapshot"]["collections"]["vehicles"][0]["registration_number"] == "KA05ZZ0001"
    assert v1["snapshot"]["department"] in (None, "")
    assert v2["is_current"] is True and v2["parent_version_id"] == v1["id"]
    assert len(v2["snapshot"]["collections"]["vehicles"]) == 2
    assert {x["registration_number"] for x in v2["snapshot"]["collections"]["vehicles"]} == {"KA05ZZ0001", "KA05ZZ0002"}
    assert v2["snapshot"]["custom_attributes"], "v2 snapshot must carry the custom attributes"
    assert {a["key"] for a in v2["snapshot"]["custom_attributes"]} == {"badge_colour", "tshirt_size"}
    assert v2["snapshot"]["department"] == "Sales"
    assert v1["record_hash"] != v2["record_hash"]
    added = [c for c in (v2["field_changes"] or []) if c["field"] == "vehicles[ka05zz0002]"]
    assert added and added[0]["kind"] == "added" and added[0]["provenance"]


def test_e103_version_compare(flow):
    cmp = flow.compare_e103
    assert cmp["business_key"] == "E103" and cmp["a_version"] == 1 and cmp["b_version"] == 2
    assert cmp["changed_collections"] == ["vehicles"]
    veh = {r["identity_key"]: r for r in cmp["collections"] if r["collection"] == "vehicles"}
    assert veh["ka05zz0002"]["kind"] == "added" and veh["ka05zz0002"]["a"] is None
    assert veh["ka05zz0002"]["b"]["registration_number"] == "KA05ZZ0002"
    assert "registration_number" in veh["ka05zz0002"]["changed_fields"]
    assert veh["ka05zz0001"]["kind"] == "unchanged" and veh["ka05zz0001"]["changed_fields"] == []
    assert "tshirt_size" in cmp["changed_custom_attributes"]
    assert "badge_colour" in cmp["changed_custom_attributes"]
    assert "department" in cmp["changed_fields"]
    dept = next(f for f in cmp["fields"] if f["field"] == "department")
    assert dept["changed"] is True and dept["kind"] == "added" and dept["b"] == "Sales"
    emp = next(f for f in cmp["fields"] if f["field"] == "employee_id")
    assert emp["changed"] is False


def test_new_employee_e100_has_exactly_one_migration_version(flow):
    v = flow.versions["E100"]
    assert [(x["version_no"], x["origin"]) for x in v] == [(1, "migration")]
    v1 = v[0]
    assert v1["parent_version_id"] is None and v1["is_current"] is True and v1["target_revision"] is None
    snap = v1["snapshot"]
    assert len(snap["collections"]["vehicles"]) == 2
    assert len(snap["collections"]["education_history"]) == 2
    assert snap["employee_id"] == "E100" and snap["full_name"] == "Priya Sharma"
    for bk in ("E101", "E102"):
        assert [(x["version_no"], x["origin"]) for x in flow.versions[bk]] == [(1, "migration")]


# =========================================================================== stage 6
def test_rerunning_reconcile_is_idempotent(flow):
    assert flow.rereconcile_post_status == 200
    items = flow.reconcile_items
    assert len(items) == 2 and {w["status"] for w in items} == {"succeeded"}, items
    assert flow.job_rereconciled["status"] == "reconciliation_complete"
    for bk in ("E100", "E101", "E102", "E103"):
        before = [(x["id"], x["version_no"]) for x in flow.versions[bk]]
        after = [(x["id"], x["version_no"]) for x in flow.versions_after_rerun[bk]]
        assert before == after, bk


# =========================================================================== audit
def test_audit_trail_covers_proposals_decisions_and_versions(flow):
    aud = flow.audit
    by_type: dict[str, list] = {}
    for a in aud:
        by_type.setdefault(a["event_type"], []).append(a)

    proposed = by_type.get("custom_field_proposed", [])
    assert {a["issue_id"] for a in proposed} >= {flow.ts_proposal["id"], flow.lg_proposal["id"]}
    assert all(a["actor"] == "system" for a in proposed)
    ts_prop = next(a for a in proposed if a["issue_id"] == flow.ts_proposal["id"])
    assert ts_prop["after"]["suggested"]["key"] == "tshirt_size"
    assert ts_prop["source_ref"]["header"] == "T-Shirt Size" and ts_prop["source_ref"]["tenant_id"] == "beta"

    created = by_type.get("custom_field_created", [])
    assert len(created) == 1
    assert created[0]["actor"] == "human" and created[0]["category"] == "human"
    assert created[0]["issue_id"] == flow.ts_proposal["id"]
    assert created[0]["after"]["key"] == "tshirt_size"
    assert created[0]["after"]["target"] == "custom_attributes.tshirt_size"
    assert created[0]["after"]["definition_id"] == flow.approve["definition"]["id"]

    ignored = by_type.get("source_field_ignored", [])
    assert len(ignored) == 1
    assert ignored[0]["actor"] == "human" and ignored[0]["issue_id"] == flow.lg_proposal["id"]
    assert ignored[0]["source_ref"]["header"] == "Legacy Payroll Code"
    assert ignored[0]["after"]["destination_kind"] == "IGNORED"

    decisions = by_type.get("record_decision", [])
    assert len(decisions) == 1
    rd = decisions[0]
    assert rd["actor"] == "human" and rd["issue_id"] == flow.conflict["id"]
    assert rd["source_ref"]["candidate_key"] == "E100" and rd["source_ref"]["field"] == "vehicles[]"
    assert rd["after"]["action"] == "select" and rd["after"]["value"] == flow.motorcycle_variant
    assert rd["source_ref"].get("provenance"), "chosen variant's source evidence should be linked"

    versions = by_type.get("employee_version", [])
    assert versions, "expected employee_version audit events"
    e103_transition = [a for a in versions if isinstance(a["after"], dict)
                       and a["after"].get("from_version") == 1 and a["after"].get("to_version") == 2
                       and (a["source_ref"] or {}).get("business_key") == "E103"]
    assert len(e103_transition) == 1
    changed = e103_transition[0]["after"]["changed_fields"]
    assert any(c.startswith("vehicles[") for c in changed), changed
    assert "department" in changed
    assert all(a["category"] == "target" for a in versions)
    # versions were created once: exactly one v1 event per employee, one v2 event for E103
    v1_events = [a for a in versions if isinstance(a["after"], dict) and a["after"].get("to_version") == 1]
    assert len(v1_events) == 4


# =========================================================================== source deep link
def test_source_deep_link_returns_exact_row_with_context(flow):
    c, jid = flow.client, flow.jid
    veh_table = next(t for t in flow.profiles["tables"] if t["sheet_name"] == "Vehicles")
    r = c.get(f"/api/jobs/{jid}/tables/{veh_table['table_id']}/rows/3?header=Vehicle%20Type&context=1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["headers"] == ["Employee ID", "Vehicle Type", "Registration Number"]
    assert body["row_number"] == 3
    assert body["highlight_header"] == "Vehicle Type" and body["highlight_col_index"] == 1
    assert body["original_filename"] == XLSX and body["sheet_name"] == "Vehicles"
    assert body["file_id"] == veh_table["file_id"] and body["total_rows"] == 6
    cells = {cell["header"]: cell["value"] for cell in body["row"]["cells"]}
    assert cells == {"Employee ID": "E100", "Vehicle Type": "Scooter", "Registration Number": "TN70AB1234"}
    assert body["row"]["cells"][1]["col_index"] == 1
    assert [x["row_number"] for x in body["context"]] == [2, 3, 4]
    assert len(body["context"]) == 3


def test_provenance_row_reference_resolves_to_the_source_cell(flow):
    """A real provenance entry from the E100 motorcycle item deep-links to the exact staged cell."""
    c, jid = flow.client, flow.jid
    moto = _vehicle_items(flow.candidates["E100"])["TN70AB1234"]
    p = next(p for p in moto["fields"]["type"]["provenance"] if p["raw"] == "Motorcycle")
    r = c.get(f"/api/jobs/{jid}/tables/{p['table_id']}/rows/{p['row_number']}?col_index={p['col_index']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["highlight_header"] == p["header"] == "Vehicle Type"
    cell = next(x for x in body["row"]["cells"] if x["col_index"] == p["col_index"])
    assert cell["value"] == "Motorcycle"


def test_source_deep_link_rejects_table_from_another_job(flow):
    c, jid = flow.client, flow.jid
    veh_table = next(t for t in flow.profiles["tables"] if t["sheet_name"] == "Vehicles")
    files = [("files", ("t.csv", (CANON + "Z900,Zed Synthetic,zed@x.example,Sales,2020-01-01,\n").encode(),
                        "text/csv"))]
    other = c.post("/api/jobs", files=files).json()["id"]
    _poll(c, other, {"blocked_provider", "mapping_complete", "awaiting_record_review",
                     "preparation_complete", "error"})
    r = c.get(f"/api/jobs/{other}/tables/{veh_table['table_id']}/rows/3?header=Vehicle%20Type&context=1")
    assert r.status_code == 404
    # and a row that does not exist in the right table is a 404 too
    r = c.get(f"/api/jobs/{jid}/tables/{veh_table['table_id']}/rows/999")
    assert r.status_code == 404
