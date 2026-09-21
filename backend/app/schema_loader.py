"""Loads the versioned target schema (``schemas/employee.vN.yaml|json``) and builds the
per-tenant *effective* schema.

The target contract is configuration, not code: field paths, labels, types, requiredness, enum
values, structure kind (scalar / collection item / custom), collection item schemas, identity
keys, validation metadata and the few explicitly-declared header aliases all come from the file.
Changing the contract means editing the schema file, not Python conditionals.

Effective schema for a job  =  core scalar fields  +  structured collections  +  the job tenant's
custom-field definitions (persisted separately per tenant).  Mapping may target ONLY destinations
in the effective contract; every destination is addressed by a *path*:

    employee_id                          core scalar
    vehicles[].registration_number       collection item field
    custom_attributes.tshirt_size        tenant custom attribute (also identified by definition id)

The representative v2 contract is defined by this prototype under the assignment's permission; it
is not a reproduction of Darwinbox's proprietary production schema. The legacy v1 JSON format
(``properties`` + ``x-`` keys) still loads for backward compatibility.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path

from .config import get_settings

SCALAR_TYPES = ("string", "email", "date", "enum", "phone", "number", "boolean", "multiselect")
CUSTOM_PATH_PREFIX = "custom_attributes."
_COLLECTION_PATH = re.compile(r"^([a-z][a-z0-9_]*)\[\]\.([a-z][a-z0-9_]*)$")


@dataclass(frozen=True)
class TargetField:
    """One addressable destination in the effective contract (scalar, collection item or custom)."""

    name: str                       # canonical key (scalar name, item key, or custom key)
    description: str
    value_type: str                 # one of SCALAR_TYPES
    required_in_final: bool
    nullable: bool
    enum_values: tuple[str, ...] | None
    disambiguation_keywords: tuple[str, ...]
    aliases: tuple[str, ...] = ()   # explicitly-declared unambiguous header synonyms
    label: str = ""
    group: str | None = None
    kind: str = "scalar"            # scalar | collection_item | custom
    path: str = ""                  # full destination path (== name for scalars)
    collection: str | None = None   # collection key for collection_item fields
    custom_definition_id: str | None = None
    tenant_id: str | None = None
    multi_value: bool = False
    # M3C: explicitly-declared, tenant-agnostic LEXICAL value synonyms for an enum/boolean field,
    # e.g. gender {female: [F], male: [M]}. Stored as a hashable tuple of (canonical, (aliases,)).
    # These are deterministic lexical aliases only — NOT a business-taxonomy translation. A
    # taxonomy collapse (e.g. "Research & Development" -> "Engineering") is NEVER declared here.
    value_aliases: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def __post_init__(self) -> None:
        if not self.path:
            object.__setattr__(self, "path", self.name)
        if not self.label:
            object.__setattr__(self, "label", self.name.replace("_", " ").capitalize())

    def value_alias_map(self) -> dict[str, str]:
        """Flat {declared_alias -> canonical_enum_label} for this field (empty when none declared)."""
        out: dict[str, str] = {}
        for canonical, aliases in self.value_aliases:
            for a in aliases:
                out[a] = canonical
        return out


@dataclass(frozen=True)
class IndexedColumnRule:
    aliases: tuple[str, ...]
    field: str


@dataclass(frozen=True)
class MultiValueColumnRule:
    aliases: tuple[str, ...]
    field: str
    delimiters: tuple[str, ...] = (";", "|")


@dataclass(frozen=True)
class Collection:
    key: str
    label: str
    description: str
    item_identity: tuple[str, ...]
    table_aliases: tuple[str, ...]
    fields: tuple[TargetField, ...]
    indexed_column: IndexedColumnRule | None = None
    multi_value_column: MultiValueColumnRule | None = None

    def get(self, item_key: str) -> TargetField | None:
        for f in self.fields:
            if f.name == item_key:
                return f
        return None

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)


@dataclass(frozen=True)
class CustomContract:
    key: str = "custom_attributes"
    label: str = "Tenant custom attributes"
    description: str = ""
    key_pattern: str = r"^[a-z][a-z0-9_]{1,63}$"
    allowed_types: tuple[str, ...] = ("string", "number", "boolean", "date", "enum", "multiselect")


@dataclass(frozen=True)
class SiblingRoleGroup:
    """M3F §F — a family of same-shape target fields a generic header cannot choose between.

    Purely a POLICY input (like ``date_role_group`` and ``disambiguation_keywords``); it is NOT part of
    the deterministic alias surface (see ``alias_audit``), so declaring it does not change the frozen
    alias-inventory hash. ``generic`` tokens name the shared concept words (e.g. ``email``/``mail``) that
    identify the FAMILY but not the specific member; ``qualifiers`` are the distinctive words that DO pin
    a member (e.g. ``corporate``/``work`` -> work_email). A header carrying only ``generic`` tokens and no
    ``qualifier`` is escalated to human review even when the model is confident and the type corroborates.
    """

    key: str
    members: tuple[str, ...]
    generic: tuple[str, ...] = ()
    # hashable {member -> (qualifier tokens,)} stored as a tuple of pairs (mirrors value_aliases).
    qualifiers: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def qualifier_map(self) -> dict[str, tuple[str, ...]]:
        return {m: q for m, q in self.qualifiers}


@dataclass(frozen=True)
class TargetSchema:
    version: str
    title: str
    description: str
    fields: tuple[TargetField, ...]                     # CORE scalar fields only
    date_role_group: tuple[str, ...]
    identity_safeguards: tuple[str, ...]
    collections: tuple[Collection, ...] = ()
    groups: tuple[dict, ...] = ()
    custom_contract: CustomContract = CustomContract()
    custom_definitions: tuple[TargetField, ...] = ()   # tenant custom fields (effective schema only)
    tenant_id: str | None = None
    boundary: dict = field(default_factory=dict)
    # M3C: schema-driven temporal constraints per date field name, used ONLY to resolve a
    # two-digit-year century when exactly one candidate survives (never a hidden library pivot):
    #   {"date_of_birth": {"not_future": True, "max_age_years": 100, "min_age_years": 14}, ...}
    temporal_constraints: dict = field(default_factory=dict)
    # M3F §F: generic sibling/role ambiguity groups (policy-only; not an alias, not in the hash).
    sibling_role_groups: tuple[SiblingRoleGroup, ...] = ()

    # --- core scalar API (unchanged for M1/M2/M3A callers) -------------------------------
    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    def get(self, name: str) -> TargetField | None:
        """Resolve a destination by scalar name OR by full path (collection item / custom)."""
        if not name:
            return None
        for f in self.fields:
            if f.name == name:
                return f
        return self.get_path(name)

    # --- path-aware API ------------------------------------------------------------------
    def get_collection(self, key: str) -> Collection | None:
        for c in self.collections:
            if c.key == key:
                return c
        return None

    def get_path(self, path: str) -> TargetField | None:
        m = _COLLECTION_PATH.match(path or "")
        if m:
            coll = self.get_collection(m.group(1))
            return coll.get(m.group(2)) if coll else None
        if (path or "").startswith(CUSTOM_PATH_PREFIX):
            key = path[len(CUSTOM_PATH_PREFIX):]
            for d in self.custom_definitions:
                if d.name == key:
                    return d
            return None
        for f in self.fields:
            if f.name == path:
                return f
        return None

    def get_custom_by_id(self, definition_id: str) -> TargetField | None:
        for d in self.custom_definitions:
            if d.custom_definition_id == definition_id:
                return d
        return None

    @property
    def all_targets(self) -> tuple[TargetField, ...]:
        out: list[TargetField] = list(self.fields)
        for c in self.collections:
            out.extend(c.fields)
        out.extend(self.custom_definitions)
        return tuple(out)

    @property
    def target_paths(self) -> tuple[str, ...]:
        """Every valid mapping destination in the effective contract."""
        return tuple(t.path for t in self.all_targets)

    def destination_kind(self, path: str | None) -> str:
        """CORE_FIELD | COLLECTION_FIELD | CUSTOM_FIELD for a valid path; UNMAPPED for None."""
        if not path:
            return "UNMAPPED"
        tf = self.get_path(path)
        if tf is None:
            return "UNKNOWN"
        return {"scalar": "CORE_FIELD", "collection_item": "COLLECTION_FIELD",
                "custom": "CUSTOM_FIELD"}[tf.kind]

    def sibling_group_for(self, target: str | None) -> SiblingRoleGroup | None:
        """The sibling/role group a target field belongs to, if any (M3F §F policy input)."""
        if not target:
            return None
        for g in self.sibling_role_groups:
            if target in g.members:
                return g
        return None

    def collection_for_table_name(self, name: str | None) -> Collection | None:
        """Deterministic: a child table / sheet whose name is a declared collection alias."""
        key = _compact(name or "")
        if not key:
            return None
        for c in self.collections:
            if key == _compact(c.key) or key == _compact(c.label) or any(key == _compact(a) for a in c.table_aliases):
                return c
        return None

    def with_custom_definitions(self, definitions: list[dict], tenant_id: str | None) -> "TargetSchema":
        """Build the effective schema for a tenant from persisted custom-field definition rows."""
        defs = tuple(custom_definition_to_field(d) for d in definitions)
        return replace(self, custom_definitions=defs, tenant_id=tenant_id)

    # --- serialization -------------------------------------------------------------------
    def public_dict(self, *, for_model: bool = False) -> dict:
        """Compact, model/UI-safe description of the (effective) schema.

        ``for_model=True`` returns a LEANER view for the bounded model prompt: only what the model
        needs to map a column to a target path (name/path/label/description/type/required/enum/kind/
        collection). It drops purely-deterministic or UI-only metadata (temporal_constraints,
        date_role_group, groups, boundary, value_aliases, the custom-attributes contract, per-field
        nullable/group/custom_definition_id). This keeps the request well under tight token/TPM
        budgets (order §22 — bounded model-visible context) without changing the deterministic layer.
        """
        def fd(f: TargetField) -> dict:
            if for_model:
                # M3E: keep the model-visible view TPM-safe on the Groq free tier (8k TPM). Send only
                # what the model needs to choose a target path — path/label/type/allowed_values, a
                # SHORT disambiguation hint (not the full description), and non-default flags — and drop
                # null keys. This roughly halves the schema token cost vs the previous for_model view.
                d = {"path": f.path, "label": f.label, "value_type": f.value_type}
                if f.required_in_final:
                    d["required"] = True
                if f.enum_values:
                    d["allowed_values"] = list(f.enum_values)
                hint = _short_hint(f.description)
                if hint:
                    d["hint"] = hint
                if f.kind != "scalar":
                    d["kind"] = f.kind
                if f.collection:
                    d["collection"] = f.collection
                if f.multi_value:
                    d["multi_value"] = True
                return d
            d = {"name": f.name, "path": f.path, "label": f.label, "description": f.description,
                 "value_type": f.value_type, "required": f.required_in_final, "nullable": f.nullable,
                 "allowed_values": list(f.enum_values) if f.enum_values else None,
                 "group": f.group, "kind": f.kind, "collection": f.collection,
                 "custom_definition_id": f.custom_definition_id, "multi_value": f.multi_value}
            if f.value_aliases:
                d["value_aliases"] = {canon: list(aliases) for canon, aliases in f.value_aliases}
            return d

        if for_model:
            # Collections stay lean too: key/label + item fields only (no per-collection description
            # or item_identity — not needed to pick a target path, and costly at free-tier TPM).
            return {
                "target_fields": [fd(f) for f in self.fields],
                "collections": [{"key": c.key, "label": c.label,
                                 "fields": [fd(f) for f in c.fields]} for c in self.collections],
                "custom_fields": [fd(f) for f in self.custom_definitions],
                "target_paths": list(self.target_paths),
            }
        return {
            "version": self.version,
            "title": self.title,
            "description": self.description,
            "tenant_id": self.tenant_id,
            "groups": [dict(g) for g in self.groups],
            "fields": [fd(f) for f in self.fields],
            "collections": [{
                "key": c.key, "label": c.label, "description": c.description,
                "item_identity": list(c.item_identity), "table_aliases": list(c.table_aliases),
                "indexed_column": ({"aliases": list(c.indexed_column.aliases), "field": c.indexed_column.field}
                                   if c.indexed_column else None),
                "multi_value_column": ({"aliases": list(c.multi_value_column.aliases),
                                        "field": c.multi_value_column.field,
                                        "delimiters": list(c.multi_value_column.delimiters)}
                                       if c.multi_value_column else None),
                "fields": [fd(f) for f in c.fields],
            } for c in self.collections],
            "custom_attributes": {"key": self.custom_contract.key, "label": self.custom_contract.label,
                                  "description": self.custom_contract.description,
                                  "key_pattern": self.custom_contract.key_pattern,
                                  "allowed_types": list(self.custom_contract.allowed_types)},
            "custom_fields": [fd(f) for f in self.custom_definitions],
            "target_paths": list(self.target_paths),
            "date_role_group": list(self.date_role_group),
            "temporal_constraints": dict(self.temporal_constraints),
            "boundary": dict(self.boundary),
        }


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _short_hint(desc: str, n: int = 80) -> str | None:
    """The first sentence of a field description, truncated — a compact disambiguation hint for the
    model-visible schema that keeps the request within the free-tier token budget."""
    d = (desc or "").strip()
    if not d:
        return None
    first = d.split(". ")[0].strip()
    return (first[:n] + "…") if len(first) > n else first


def custom_definition_to_field(d: dict) -> TargetField:
    """A persisted tenant custom-field definition row -> an addressable TargetField."""
    options = d.get("options")
    if isinstance(options, str):
        options = json.loads(options) if options else None
    aliases = d.get("aliases")
    if isinstance(aliases, str):
        aliases = json.loads(aliases) if aliases else []
    vt = d.get("type") or "string"
    return TargetField(
        name=d["key"], description=d.get("description") or f"Tenant custom field '{d.get('label') or d['key']}'.",
        value_type=vt, required_in_final=bool(d.get("required")), nullable=not bool(d.get("required")),
        enum_values=tuple(options) if options else None,
        disambiguation_keywords=tuple(_kw(d.get("label") or d["key"])),
        aliases=tuple(a.lower() for a in ([d.get("label")] if d.get("label") else []) + list(aliases or [])),
        label=d.get("label") or d["key"], group="custom", kind="custom",
        path=f"{CUSTOM_PATH_PREFIX}{d['key']}", custom_definition_id=d.get("id"),
        tenant_id=d.get("tenant_id"), multi_value=bool(d.get("multi_value")) or vt == "multiselect")


def _kw(label: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", (label or "").lower()) if t]


# ------------------------------------------------------------------------------------------
# Loading
# ------------------------------------------------------------------------------------------
def _read(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml
        return yaml.safe_load(text)
    return json.loads(text)


def _field_from_v2(spec: dict, *, required: set[str] | None = None, group_default: str | None = None,
                   kind: str = "scalar", collection: str | None = None) -> TargetField:
    key = spec["key"]
    vt = spec.get("type", "string")
    if vt not in SCALAR_TYPES:
        raise ValueError(f"schema field '{key}': unsupported type '{vt}'")
    enum_vals = spec.get("enum")
    is_required = bool(spec.get("required")) or (required is not None and key in required)
    path = f"{collection}[].{key}" if collection else key
    va_raw = spec.get("value_aliases") or {}
    value_aliases = tuple(
        (str(canon), tuple(str(a) for a in (aliases or [])))
        for canon, aliases in va_raw.items()
    )
    return TargetField(
        name=key, description=spec.get("description", ""), value_type=vt,
        required_in_final=is_required, nullable=not is_required,
        enum_values=tuple(str(v) for v in enum_vals if v is not None) if enum_vals else None,
        disambiguation_keywords=tuple(str(k).lower() for k in spec.get("disambiguation_keywords", [])),
        aliases=tuple(str(a).lower() for a in spec.get("aliases", [])),
        label=spec.get("label", ""), group=spec.get("group", group_default), kind=kind, path=path,
        collection=collection, value_aliases=value_aliases)


def _load_sibling_role_groups(rules: dict) -> tuple[SiblingRoleGroup, ...]:
    """Parse business_rules.sibling_role_groups (M3F §F). Tokens are lower-cased; qualifiers are a
    {member -> tuple} map flattened to a hashable tuple of pairs so TargetSchema stays frozen."""
    out: list[SiblingRoleGroup] = []
    for g in rules.get("sibling_role_groups", []) or []:
        members = tuple(str(m) for m in g.get("members", []))
        generic = tuple(str(t).lower() for t in g.get("generic", []))
        quals_raw = g.get("qualifiers", {}) or {}
        quals = tuple((str(m), tuple(str(t).lower() for t in (quals_raw.get(m) or [])))
                      for m in members)
        out.append(SiblingRoleGroup(key=str(g.get("key", "")), members=members,
                                    generic=generic, qualifiers=quals))
    return tuple(out)


def _load_v2(raw: dict, path: Path) -> TargetSchema:
    rules = raw.get("business_rules", {})
    required = set(rules.get("required_in_final_record", []))
    fields = [_field_from_v2(spec, required=required) for spec in raw.get("fields", [])]
    names = [f.name for f in fields]
    if len(set(names)) != len(names):
        raise ValueError("schema: duplicate core field keys")

    collections: list[Collection] = []
    for c in raw.get("collections", []):
        ckey = c["key"]
        if ckey in names:
            raise ValueError(f"schema: collection '{ckey}' collides with a core field")
        items = [_field_from_v2(spec, kind="collection_item", collection=ckey, group_default=ckey)
                 for spec in c.get("fields", [])]
        item_names = {f.name for f in items}
        identity = tuple(c.get("item_identity", []))
        for idk in identity:
            if idk not in item_names:
                raise ValueError(f"schema: collection '{ckey}' identity '{idk}' is not an item field")
        idx = c.get("indexed_column")
        mv = c.get("multi_value_column")
        collections.append(Collection(
            key=ckey, label=c.get("label", ckey), description=c.get("description", ""),
            item_identity=identity, table_aliases=tuple(str(a).lower() for a in c.get("table_aliases", [])),
            fields=tuple(items),
            indexed_column=IndexedColumnRule(tuple(str(a).lower() for a in idx["aliases"]), idx["field"]) if idx else None,
            multi_value_column=(MultiValueColumnRule(tuple(str(a).lower() for a in mv["aliases"]), mv["field"],
                                                     tuple(mv.get("delimiters", [";", "|"]))) if mv else None)))

    ca = raw.get("custom_attributes", {}) or {}
    contract = CustomContract(
        key=ca.get("key", "custom_attributes"), label=ca.get("label", "Tenant custom attributes"),
        description=ca.get("description", ""), key_pattern=ca.get("key_pattern", CustomContract.key_pattern),
        allowed_types=tuple(ca.get("allowed_types", CustomContract.allowed_types)))

    return TargetSchema(
        version=raw.get("version", path.stem), title=raw.get("title", path.stem),
        description=raw.get("description", ""), fields=tuple(fields),
        date_role_group=tuple(rules.get("date_role_group", {}).get("fields", ())),
        identity_safeguards=tuple(rules.get("identity_safeguards", ())),
        collections=tuple(collections), groups=tuple(raw.get("groups", [])),
        custom_contract=contract, boundary=dict(raw.get("boundary", {})),
        temporal_constraints=dict(rules.get("temporal_constraints", {}) or {}),
        sibling_role_groups=_load_sibling_role_groups(rules))


def _load_v1(raw: dict, path: Path) -> TargetSchema:
    """Legacy JSON-Schema-flavoured v1 format (kept loadable for backward compatibility)."""
    rules = raw.get("x-business-rules", {})
    required = set(rules.get("required-in-final-record", raw.get("required", [])))
    nullable = set(rules.get("nullable-fields", []))
    fields: list[TargetField] = []
    for name, spec in raw["properties"].items():
        enum_vals = spec.get("enum")
        fields.append(TargetField(
            name=name, description=spec.get("description", ""), value_type=spec.get("x-value-type", "string"),
            required_in_final=name in required, nullable=(name in nullable) or (name not in required),
            enum_values=tuple(v for v in enum_vals if v is not None) if enum_vals else None,
            disambiguation_keywords=tuple(k.lower() for k in spec.get("x-disambiguation-keywords", [])),
            aliases=tuple(a.lower() for a in spec.get("x-aliases", []))))
    return TargetSchema(
        version=raw.get("version", path.stem), title=raw.get("title", path.stem),
        description=raw.get("description", ""), fields=tuple(fields),
        date_role_group=tuple(rules.get("date-role-group", {}).get("fields", ())),
        identity_safeguards=tuple(rules.get("identity-safeguards", ())))


def load_schema(path: Path) -> TargetSchema:
    raw = _read(path)
    if "properties" in raw and "fields" not in raw:
        return _load_v1(raw, path)
    return _load_v2(raw, path)


def resolve_schema_path(schemas_dir: Path, version: str) -> Path:
    """Find ``<version>.yaml|.yml|.json`` under the schemas directory."""
    for ext in (".yaml", ".yml", ".json"):
        p = schemas_dir / f"{version}{ext}"
        if p.exists():
            return p
    raise FileNotFoundError(f"No schema file for version '{version}' under {schemas_dir}")


@lru_cache
def get_target_schema() -> TargetSchema:
    """Load and cache the configured BASE target schema (core + collections; no tenant fields)."""
    return load_schema(get_settings().schema_path)


# ------------------------------------------------------------------------------------------
# Tenant custom-field seeds (schemas/tenants/*.yaml|json)
# ------------------------------------------------------------------------------------------
def load_tenant_seeds(tenants_dir: Path) -> list[dict]:
    """Return [{tenant_id, name, custom_fields:[...]}] from the seed directory (may be empty)."""
    out: list[dict] = []
    if not tenants_dir.exists():
        return out
    for p in sorted(tenants_dir.iterdir()):
        if p.suffix.lower() not in (".yaml", ".yml", ".json") or not p.is_file():
            continue
        raw = _read(p) or {}
        if not raw.get("tenant_id"):
            continue
        out.append({"tenant_id": str(raw["tenant_id"]), "name": raw.get("name") or str(raw["tenant_id"]),
                    "custom_fields": list(raw.get("custom_fields", []) or [])})
    return out
