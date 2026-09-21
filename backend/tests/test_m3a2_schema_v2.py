"""M3A.2 schema-side tests (order G3.1 / G3.2 / G3.17).

Offline, deterministic, no provider. Covers the representative ``employee.v2`` YAML contract
(core scalars + structured collections + tenant custom-attribute contract), backward loading of
the legacy ``employee.v1.json``, path-aware resolution, destination kinds, the closed set of
target paths, child-table recognition, per-tenant effective schemas, tenant seeds, the public
serialisation and the policy's refusal to accept a path outside the effective contract.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.llm.schema import ProposalItem
from app.mapping_rules import table_context_name
from app.policy import Decision, classify_proposal
from app.profiling import ColumnProfile
from app.schema_loader import (
    CUSTOM_PATH_PREFIX,
    Collection,
    TargetField,
    TargetSchema,
    custom_definition_to_field,
    get_target_schema,
    load_schema,
    load_tenant_seeds,
    resolve_schema_path,
)

REPO = Path(__file__).resolve().parent.parent.parent
SCHEMAS = REPO / "schemas"

LEGACY_SIX = ("employee_id", "full_name", "work_email", "department", "hire_date", "contract_start_date")
REQUIRED = {"employee_id", "full_name", "work_email", "hire_date"}
EXPECTED_COLLECTIONS = {
    "addresses": ("type",),
    "emergency_contacts": ("name",),
    "dependents": ("name",),
    "education_history": ("level", "institution"),
    "vehicles": ("registration_number",),
}
VEHICLE_REG = "vehicles[].registration_number"
BOGUS_PATHS = ("salary_band", "vehicles[].colour", "cars[].registration_number",
               "vehicles", "vehicles[]", "custom_attributes.", "Employee ID")

# A synthetic persisted custom-field definition row (shape of the tenant custom_fields table).
TSHIRT_DEF = {
    "id": "cfd_test_tshirt", "tenant_id": "beta", "key": "tshirt_size", "label": "T-Shirt Size",
    "type": "enum", "options": ["L", "M", "XL"], "required": False, "multi_value": False,
    "aliases": ["tee size"], "description": "Synthetic tenant custom field.",
}


@pytest.fixture(scope="module")
def base() -> TargetSchema:
    return load_schema(SCHEMAS / "employee.v2.yaml")


@pytest.fixture(scope="module")
def effective(base: TargetSchema) -> TargetSchema:
    return base.with_custom_definitions([TSHIRT_DEF], "beta")


# ------------------------------------------------------------------------------------------
# helpers for the policy tests (same shape as tests/test_policy.py)
# ------------------------------------------------------------------------------------------
def make_profile(header, *, samples=None, observed_types=None, non_empty=5, col=0):
    return ColumnProfile(
        profile_id=f"col_{header}", table_id="tbl_1", col_index=col, header=header,
        non_empty_count=non_empty, missing_count=0, distinct_count=non_empty,
        observed_types=observed_types or {"text": non_empty},
        format_indicators={"email_ratio": 0.0, "iso_date_ratio": 0.0, "slash_date_ratio": 0.0,
                           "looks_date_like": False, "has_leading_zero_values": False},
        samples=samples or ["x", "y"],
    )


def make_item(header, target, *, alts=None, ambiguous=False, conf=0.9):
    return ProposalItem(
        source_column_id=f"col_{header}", source_header=header, proposed_target_field=target,
        alternative_target_fields=alts or [], is_ambiguous=ambiguous, ambiguity_reason=None,
        evidence=[f"header {header}"], confidence=conf,
    )


# ------------------------------------------------------------------------------------------
# G3.1 — base schema loads from YAML
# ------------------------------------------------------------------------------------------
def test_configured_base_schema_is_the_v2_yaml():
    from app.config import get_settings

    path = get_settings().schema_path
    assert path.name == "employee.v2.yaml"
    assert path.suffix == ".yaml"
    schema = get_target_schema()
    assert schema.version == "employee.v2"
    assert len(schema.fields) == 24
    assert schema.tenant_id is None
    assert schema.custom_definitions == ()


def test_resolve_schema_path_prefers_yaml_then_json():
    assert resolve_schema_path(SCHEMAS, "employee.v2").suffix == ".yaml"
    assert resolve_schema_path(SCHEMAS, "employee.v1").suffix == ".json"
    with pytest.raises(FileNotFoundError):
        resolve_schema_path(SCHEMAS, "employee.v99")


def test_v2_core_fields_required_set_and_legacy_six(base: TargetSchema):
    assert base.version == "employee.v2"
    assert len(base.fields) == 24
    names = base.field_names
    assert len(set(names)) == 24, "core field keys must be unique"
    for legacy in LEGACY_SIX:
        assert legacy in names, f"legacy field {legacy} missing from v2"
    assert {f.name for f in base.fields if f.required_in_final} == REQUIRED
    # required <-> not nullable for core scalars
    for f in base.fields:
        assert f.nullable is (not f.required_in_final)
        assert f.kind == "scalar"
        assert f.path == f.name
        assert f.collection is None
        assert f.custom_definition_id is None
    # the date-role safeguard group is preserved from v1
    assert tuple(base.date_role_group) == ("hire_date", "contract_start_date")
    assert base.identity_safeguards, "identity safeguards must be declared"
    # department enum survives (v1 had the same four labels)
    dept = base.get("department")
    assert dept is not None and dept.value_type == "enum"
    assert set(dept.enum_values) == {"Engineering", "Sales", "Finance", "People Operations"}


def test_v2_collections_identity_keys(base: TargetSchema):
    assert len(base.collections) == 5
    assert {c.key for c in base.collections} == set(EXPECTED_COLLECTIONS)
    for c in base.collections:
        assert isinstance(c, Collection)
        assert c.item_identity == EXPECTED_COLLECTIONS[c.key], c.key
        # every identity key is an item field of that collection
        for idk in c.item_identity:
            assert c.get(idk) is not None
        for f in c.fields:
            assert f.kind == "collection_item"
            assert f.collection == c.key
            assert f.path == f"{c.key}[].{f.name}"
        assert base.get_collection(c.key) is c
    assert base.get_collection("cars") is None


def test_vehicles_flattened_shape_rules(base: TargetSchema):
    vehicles = base.get_collection("vehicles")
    assert vehicles is not None
    assert vehicles.indexed_column is not None
    assert vehicles.indexed_column.field == "registration_number"
    assert set(vehicles.indexed_column.aliases) == {"vehicle", "vehicle number", "vehicle registration", "vehicle no"}
    assert vehicles.multi_value_column is not None
    assert vehicles.multi_value_column.field == "registration_number"
    assert set(vehicles.multi_value_column.aliases) == {"vehicle numbers", "vehicle registrations",
                                                         "vehicle registration numbers"}
    assert tuple(vehicles.multi_value_column.delimiters) == (";", "|")
    # the other collections declare no flattened-shape rule
    for c in base.collections:
        if c.key != "vehicles":
            assert c.indexed_column is None and c.multi_value_column is None


def test_v2_custom_attribute_contract(base: TargetSchema):
    contract = base.custom_contract
    assert contract.key == "custom_attributes"
    assert set(contract.allowed_types) == {"string", "number", "boolean", "date", "enum", "multiselect"}
    assert contract.key_pattern  # non-empty pattern
    import re
    assert re.match(contract.key_pattern, "tshirt_size")
    assert not re.match(contract.key_pattern, "T-Shirt Size")
    assert not re.match(contract.key_pattern, "1bad")


# ------------------------------------------------------------------------------------------
# G3.17 — legacy v1 JSON still loads
# ------------------------------------------------------------------------------------------
def test_legacy_v1_json_still_loads_with_exactly_six_fields():
    v1 = load_schema(SCHEMAS / "employee.v1.json")
    assert v1.version == "employee.v1"
    assert v1.field_names == LEGACY_SIX
    assert len(v1.fields) == 6
    assert v1.collections == ()
    assert v1.custom_definitions == ()
    assert {f.name for f in v1.fields if f.required_in_final} == REQUIRED
    assert set(v1.target_paths) == set(LEGACY_SIX)
    # path-aware API works on the legacy schema too, but it has no collection destinations
    assert v1.get("employee_id") is not None
    assert v1.get_path(VEHICLE_REG) is None
    assert v1.destination_kind("employee_id") == "CORE_FIELD"
    assert v1.destination_kind(VEHICLE_REG) == "UNKNOWN"
    assert tuple(v1.date_role_group) == ("hire_date", "contract_start_date")


# ------------------------------------------------------------------------------------------
# G3.2 — path-aware resolution
# ------------------------------------------------------------------------------------------
def test_get_and_get_path_resolve_collection_item_path(base: TargetSchema):
    tf = base.get(VEHICLE_REG)
    assert isinstance(tf, TargetField)
    assert tf.name == "registration_number"
    assert tf.path == VEHICLE_REG
    assert tf.kind == "collection_item"
    assert tf.collection == "vehicles"
    assert tf.required_in_final is True
    assert tf.value_type == "string"
    assert base.get_path(VEHICLE_REG) is tf
    # core scalars resolve by name and by path identically
    assert base.get("employee_id") is base.get_path("employee_id")
    assert base.get("employee_id").kind == "scalar"
    # item field names never leak as bare scalar destinations
    assert base.get("registration_number") is None
    assert base.get_path("registration_number") is None


@pytest.mark.parametrize("bogus", BOGUS_PATHS)
def test_unknown_paths_resolve_to_none(base: TargetSchema, bogus: str):
    assert base.get(bogus) is None
    assert base.get_path(bogus) is None
    assert bogus not in base.target_paths
    assert base.destination_kind(bogus) == "UNKNOWN"


def test_get_and_get_path_handle_empty(base: TargetSchema):
    assert base.get("") is None
    assert base.get(None) is None  # type: ignore[arg-type]
    assert base.get_path("") is None
    assert base.get_path(None) is None  # type: ignore[arg-type]


def test_destination_kind_for_every_layer(base: TargetSchema, effective: TargetSchema):
    assert base.destination_kind("employee_id") == "CORE_FIELD"
    assert base.destination_kind("hire_date") == "CORE_FIELD"
    assert base.destination_kind(VEHICLE_REG) == "COLLECTION_FIELD"
    assert base.destination_kind("addresses[].type") == "COLLECTION_FIELD"
    assert base.destination_kind("salary_band") == "UNKNOWN"
    assert base.destination_kind("vehicles[].colour") == "UNKNOWN"
    assert base.destination_kind(None) == "UNMAPPED"
    assert base.destination_kind("") == "UNMAPPED"
    # a custom path is CUSTOM_FIELD only in the tenant's effective schema
    custom_path = f"{CUSTOM_PATH_PREFIX}tshirt_size"
    assert base.destination_kind(custom_path) == "UNKNOWN"
    assert effective.destination_kind(custom_path) == "CUSTOM_FIELD"
    assert effective.destination_kind(f"{CUSTOM_PATH_PREFIX}badge_colour") == "UNKNOWN"
    assert effective.destination_kind("employee_id") == "CORE_FIELD"
    assert effective.destination_kind(VEHICLE_REG) == "COLLECTION_FIELD"


def test_target_paths_is_closed_and_includes_collection_paths(base: TargetSchema):
    paths = base.target_paths
    assert len(paths) == len(set(paths)), "target paths must be unique"
    expected_count = len(base.fields) + sum(len(c.fields) for c in base.collections)
    assert len(paths) == expected_count
    # every advertised path resolves and every resolved kind is a real destination
    for p in paths:
        tf = base.get_path(p)
        assert tf is not None, p
        assert tf.path == p
        assert base.destination_kind(p) in {"CORE_FIELD", "COLLECTION_FIELD"}
    for bogus in BOGUS_PATHS:
        assert bogus not in paths
    # core + collection paths present
    for legacy in LEGACY_SIX:
        assert legacy in paths
    for coll_path in (VEHICLE_REG, "vehicles[].type", "addresses[].type", "addresses[].city",
                      "emergency_contacts[].name", "dependents[].name",
                      "education_history[].level", "education_history[].institution"):
        assert coll_path in paths
    # the base schema carries no tenant custom paths
    assert not any(p.startswith(CUSTOM_PATH_PREFIX) for p in paths)
    assert set(paths) == {t.path for t in base.all_targets}


# ------------------------------------------------------------------------------------------
# child-table recognition (deterministic, alias-driven)
# ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name,expected", [
    ("Vehicles", "vehicles"),
    ("vehicle", "vehicles"),
    ("VEHICLES", "vehicles"),
    ("Employee Vehicles", "vehicles"),
    ("Emergency Contacts", "emergency_contacts"),
    ("emergency_contacts", "emergency_contacts"),
    ("Education", "education_history"),
    ("Qualifications", "education_history"),
    ("Dependents", "dependents"),
    ("Addresses", "addresses"),
    ("Employee Updates", None),
    ("Employees", None),
    ("Salary", None),
    ("", None),
    (None, None),
])
def test_collection_for_table_name(base: TargetSchema, name, expected):
    got = base.collection_for_table_name(name)
    if expected is None:
        assert got is None
    else:
        assert got is not None and got.key == expected


def test_collection_for_table_name_via_table_context_name(base: TargetSchema):
    # CSV stem with a numbering prefix -> collection; XLSX sheet name wins over file name
    assert table_context_name("03_vehicles.csv", None) == "vehicles"
    assert base.collection_for_table_name(table_context_name("03_vehicles.csv", None)).key == "vehicles"
    assert base.collection_for_table_name(table_context_name("02_hr_details.xlsx", "Vehicles")).key == "vehicles"
    assert base.collection_for_table_name(table_context_name("02_hr_details.xlsx", "Education")).key == "education_history"
    # an employee master table is never mistaken for a collection
    assert base.collection_for_table_name(table_context_name("01_employees.csv", None)) is None
    assert base.collection_for_table_name(table_context_name("legacy_hr.csv", None)) is None
    assert base.collection_for_table_name(table_context_name("employee_export.xlsx", "Employee Updates")) is None


# ------------------------------------------------------------------------------------------
# effective schema = base + tenant custom definitions
# ------------------------------------------------------------------------------------------
def test_with_custom_definitions_builds_effective_schema(base: TargetSchema, effective: TargetSchema):
    custom_path = f"{CUSTOM_PATH_PREFIX}tshirt_size"
    assert effective.tenant_id == "beta"
    assert effective.version == base.version
    assert effective.fields == base.fields, "core scalars are unchanged by tenant definitions"
    assert effective.collections == base.collections
    assert len(effective.custom_definitions) == 1

    tf = effective.get_path(custom_path)
    assert tf is not None
    assert effective.get(custom_path) is tf
    assert tf.kind == "custom"
    assert tf.name == "tshirt_size"
    assert tf.path == custom_path
    assert tf.label == "T-Shirt Size"
    assert tf.group == "custom"
    assert tf.value_type == "enum"
    assert tf.enum_values == ("L", "M", "XL")
    assert tf.custom_definition_id == "cfd_test_tshirt"
    assert tf.tenant_id == "beta"
    assert tf.required_in_final is False and tf.nullable is True
    assert tf.multi_value is False
    # the display label (lower-cased) is an alias, plus the declared aliases
    assert "t-shirt size" in tf.aliases
    assert "tee size" in tf.aliases
    assert effective.get_custom_by_id("cfd_test_tshirt") is tf
    assert effective.get_custom_by_id("cfd_missing") is None

    assert custom_path in effective.target_paths
    assert len(effective.target_paths) == len(base.target_paths) + 1
    assert effective.destination_kind(custom_path) == "CUSTOM_FIELD"
    # the base schema is untouched (frozen dataclass semantics)
    assert base.get_path(custom_path) is None
    assert custom_path not in base.target_paths
    assert base.custom_definitions == ()


def test_custom_definition_from_persisted_row_json_columns():
    """Persisted rows carry options/aliases as JSON text; both shapes must build the same field."""
    row = dict(TSHIRT_DEF, options=json.dumps(["L", "M", "XL"]), aliases=json.dumps(["tee size"]))
    tf = custom_definition_to_field(row)
    assert tf.enum_values == ("L", "M", "XL")
    assert "tee size" in tf.aliases
    assert tf.path == f"{CUSTOM_PATH_PREFIX}tshirt_size"
    # a multiselect definition is multi-valued by type
    ms = custom_definition_to_field({"id": "cfd_ms", "tenant_id": "beta", "key": "skills",
                                     "label": "Skills", "type": "multiselect", "options": ["a", "b"]})
    assert ms.multi_value is True and ms.value_type == "multiselect"
    # a required definition is not nullable
    req = custom_definition_to_field({"id": "cfd_r", "tenant_id": "beta", "key": "cost_code",
                                      "label": "Cost Code", "type": "string", "required": True})
    assert req.required_in_final is True and req.nullable is False
    assert req.enum_values is None


def test_effective_schemas_are_tenant_isolated(base: TargetSchema):
    beta = base.with_custom_definitions([TSHIRT_DEF], "beta")
    gamma = base.with_custom_definitions([], "gamma")
    custom_path = f"{CUSTOM_PATH_PREFIX}tshirt_size"
    assert beta.get_path(custom_path) is not None
    assert gamma.get_path(custom_path) is None
    assert gamma.destination_kind(custom_path) == "UNKNOWN"
    assert gamma.tenant_id == "gamma"
    assert set(gamma.target_paths) == set(base.target_paths)


# ------------------------------------------------------------------------------------------
# tenant seeds
# ------------------------------------------------------------------------------------------
def test_load_tenant_seeds_returns_beta_with_badge_colour():
    seeds = load_tenant_seeds(SCHEMAS / "tenants")
    assert seeds, "schemas/tenants must contain at least the beta seed"
    by_id = {s["tenant_id"]: s for s in seeds}
    assert "beta" in by_id
    beta = by_id["beta"]
    assert set(beta) == {"tenant_id", "name", "custom_fields"}
    assert beta["name"]
    keys = [cf["key"] for cf in beta["custom_fields"]]
    assert keys == ["badge_colour"], "beta seed defines exactly one custom field"
    badge = beta["custom_fields"][0]
    assert badge["type"] == "enum"
    assert badge["options"] == ["Red", "Blue", "Green"]
    assert "badge color" in badge["aliases"]
    # the seed row converts into an addressable custom TargetField
    tf = custom_definition_to_field(dict(badge, id="cfd_seed", tenant_id="beta"))
    assert tf.path == f"{CUSTOM_PATH_PREFIX}badge_colour"
    assert "badge color" in tf.aliases and "badge colour" in tf.aliases
    assert tf.enum_values == ("Red", "Blue", "Green")


def test_load_tenant_seeds_missing_dir_and_non_tenant_files(tmp_path: Path):
    assert load_tenant_seeds(tmp_path / "does-not-exist") == []
    (tmp_path / "notes.txt").write_text("tenant_id: nope\n", encoding="utf-8")   # wrong suffix, skipped
    (tmp_path / "empty.yaml").write_text("", encoding="utf-8")                   # no tenant_id, skipped
    (tmp_path / "no_id.yaml").write_text("name: anonymous\n", encoding="utf-8")   # no tenant_id, skipped
    (tmp_path / "zeta.json").write_text(json.dumps({"tenant_id": "zeta"}), encoding="utf-8")
    seeds = load_tenant_seeds(tmp_path)
    assert [s["tenant_id"] for s in seeds] == ["zeta"]
    assert seeds[0]["name"] == "zeta"
    assert seeds[0]["custom_fields"] == []


# ------------------------------------------------------------------------------------------
# public serialisation
# ------------------------------------------------------------------------------------------
def test_public_dict_shape_and_representative_disclaimer(base: TargetSchema, effective: TargetSchema):
    pub = base.public_dict()
    for key in ("version", "title", "description", "fields", "collections", "custom_attributes",
                "custom_fields", "target_paths", "boundary", "groups", "date_role_group"):
        assert key in pub, key
    assert pub["version"] == "employee.v2"
    desc = pub["description"].lower()
    assert "proprietary" in desc
    assert "representative" in desc
    assert "not" in desc  # "Not a reproduction of ... proprietary production schema"
    assert set(pub["boundary"]) == {"core", "collections", "custom", "unmapped"}
    assert len(pub["fields"]) == 24
    assert len(pub["collections"]) == 5
    assert pub["custom_fields"] == []
    assert pub["tenant_id"] is None
    assert pub["target_paths"] == list(base.target_paths)
    assert pub["date_role_group"] == ["hire_date", "contract_start_date"]
    assert {c["key"] for c in pub["collections"]} == set(EXPECTED_COLLECTIONS)
    for c in pub["collections"]:
        assert c["item_identity"] == list(EXPECTED_COLLECTIONS[c["key"]])
        for f in c["fields"]:
            assert f["kind"] == "collection_item" and f["collection"] == c["key"]
            assert f["path"] == f"{c['key']}[].{f['name']}"
    vehicles = next(c for c in pub["collections"] if c["key"] == "vehicles")
    assert vehicles["indexed_column"]["field"] == "registration_number"
    assert vehicles["multi_value_column"]["delimiters"] == [";", "|"]
    for f in pub["fields"]:
        assert f["kind"] == "scalar" and f["path"] == f["name"]
        assert set(f) >= {"name", "path", "label", "value_type", "required", "nullable",
                          "allowed_values", "group", "kind"}
    assert pub["custom_attributes"]["key"] == "custom_attributes"
    json.dumps(pub)  # must be JSON-serialisable for the API/UI

    eff = effective.public_dict()
    assert eff["tenant_id"] == "beta"
    assert [f["name"] for f in eff["custom_fields"]] == ["tshirt_size"]
    cf = eff["custom_fields"][0]
    assert cf["kind"] == "custom"
    assert cf["path"] == f"{CUSTOM_PATH_PREFIX}tshirt_size"
    assert cf["custom_definition_id"] == "cfd_test_tshirt"
    assert cf["allowed_values"] == ["L", "M", "XL"]
    assert cf["path"] in eff["target_paths"]
    assert eff["fields"] == pub["fields"]
    json.dumps(eff)


# ------------------------------------------------------------------------------------------
# schema-file validation (a broken contract file must fail loudly, never load half a schema)
# ------------------------------------------------------------------------------------------
def _write_yaml(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_v2_loader_rejects_identity_key_that_is_not_an_item_field(tmp_path: Path):
    p = _write_yaml(tmp_path, "bad_identity.yaml", """
version: t.v2
fields:
  - {key: employee_id, type: string, required: true}
collections:
  - key: vehicles
    item_identity: [colour]
    fields:
      - {key: registration_number, type: string}
""")
    with pytest.raises(ValueError, match="identity"):
        load_schema(p)


def test_v2_loader_rejects_duplicate_core_keys_and_collection_collisions(tmp_path: Path):
    dup = _write_yaml(tmp_path, "dup.yaml", """
version: t.v2
fields:
  - {key: employee_id, type: string}
  - {key: employee_id, type: string}
""")
    with pytest.raises(ValueError, match="duplicate"):
        load_schema(dup)
    collide = _write_yaml(tmp_path, "collide.yaml", """
version: t.v2
fields:
  - {key: vehicles, type: string}
collections:
  - key: vehicles
    item_identity: [registration_number]
    fields:
      - {key: registration_number, type: string}
""")
    with pytest.raises(ValueError, match="collides"):
        load_schema(collide)
    badtype = _write_yaml(tmp_path, "badtype.yaml", """
version: t.v2
fields:
  - {key: salary, type: money}
""")
    with pytest.raises(ValueError, match="unsupported type"):
        load_schema(badtype)


def test_minimal_v2_yaml_loads_with_defaults(tmp_path: Path):
    p = _write_yaml(tmp_path, "mini.yaml", """
version: mini.v2
fields:
  - key: employee_id
    type: string
business_rules:
  required_in_final_record: [employee_id]
collections:
  - key: pets
    item_identity: [name]
    fields:
      - {key: name, type: string, required: true}
""")
    s = load_schema(p)
    assert s.version == "mini.v2"
    assert s.get("employee_id").required_in_final is True
    assert s.get_path("pets[].name").required_in_final is True
    assert s.destination_kind("pets[].name") == "COLLECTION_FIELD"
    assert s.collection_for_table_name("Pets").key == "pets"
    assert s.custom_contract.key == "custom_attributes"       # defaults when block absent
    assert s.boundary == {}
    assert set(s.target_paths) == {"employee_id", "pets[].name"}


# ------------------------------------------------------------------------------------------
# policy: the valid destination set is the effective contract's target paths
# ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("bogus", ["salary_band", "vehicles[].colour", "cars[].registration_number",
                                   "custom_attributes.tshirt_size"])
def test_policy_never_accepts_a_path_outside_the_contract(base: TargetSchema, bogus: str):
    prof = make_profile("Some Column", samples=["A", "B"])
    res = classify_proposal(make_item("Some Column", bogus), prof, base)
    assert res.decision is Decision.NEEDS_REVIEW
    assert res.target_field is None
    assert "not a destination" in res.reason.lower()
    assert bogus not in res.candidate_target_fields
    # the reviewer is offered only real destinations
    assert set(res.candidate_target_fields) == set(base.target_paths)


def test_policy_accepts_collection_path_with_supporting_header(base: TargetSchema):
    prof = make_profile("Registration Number", samples=["TN70AB1234", "KA01CD5678"])
    res = classify_proposal(make_item("Registration Number", VEHICLE_REG), prof, base)
    assert res.decision in (Decision.AUTO_ACCEPT, Decision.NEEDS_REVIEW)
    assert "not a destination" not in res.reason.lower()
    assert res.target_field == VEHICLE_REG
    assert VEHICLE_REG in res.candidate_target_fields
    # verified behaviour: header tokens {registration, number} support the item field, so the
    # deterministic policy auto-accepts with a labelled heuristic
    assert res.decision is Decision.AUTO_ACCEPT
    assert res.evidence_summary["policy_version"] == "policy.v2"


def test_policy_collection_path_type_incompatibility_still_escalates(base: TargetSchema):
    # date-like values proposed to the string item field -> escalate on type, NOT as an unknown path
    prof = make_profile("Registration Number", samples=["2020-01-01", "2021-02-02"])
    prof.format_indicators["looks_date_like"] = True
    res = classify_proposal(make_item("Registration Number", VEHICLE_REG), prof, base)
    assert res.decision is Decision.NEEDS_REVIEW
    assert res.target_field == VEHICLE_REG
    assert "type incompatibility" in res.reason.lower()
    assert "not a destination" not in res.reason.lower()


def test_policy_custom_path_valid_only_in_tenant_effective_schema(base: TargetSchema, effective: TargetSchema):
    custom_path = f"{CUSTOM_PATH_PREFIX}tshirt_size"
    prof = make_profile("T-Shirt Size", samples=["M", "L", "XL"])
    # beta's effective schema knows the path -> a real destination
    res_beta = classify_proposal(make_item("T-Shirt Size", custom_path), prof, effective)
    assert res_beta.decision is Decision.AUTO_ACCEPT
    assert res_beta.target_field == custom_path
    assert "not a destination" not in res_beta.reason.lower()
    # the base (or another tenant's) schema does not -> never invented
    res_base = classify_proposal(make_item("T-Shirt Size", custom_path), prof, base)
    assert res_base.decision is Decision.NEEDS_REVIEW
    assert res_base.target_field is None
    assert "not a destination" in res_base.reason.lower()
