"""M3A.1: immutable employee record versions derived from reconciliation (+ M3A.2 snapshots).

A *version* is the complete effective employee snapshot at a decision point — distinct
from the audit event stream (which records "what happened"). Versions are append-only and
idempotent: re-running reconciliation with an unchanged effective record creates NO new version
(dedup by snapshot hash).

Since M3A.2 a snapshot is  {…core scalar fields, "collections": {coll: [items…]},
"custom_attributes": [{definition_id, key, value}…]}  — collections and custom attributes are
part of the versioned business record and of the deterministic comparison
(:func:`compare_snapshots`: scalar field diff + per-collection item added/removed/changed +
custom-attribute diff).

Rules (see the order):
  - existing target employee:  v1 = the confirmed TARGET BASELINE (with its revision);
                               an approved/safe change -> v2 (parent v1), etc.
  - new employee (no target):  v1 = the first approved canonical migration state (no fake v0).
  - NO_CHANGE / unchanged recompute / REVIEW_REQUIRED / EXCLUDED -> no (new) version.
Mapping/configuration events (custom-field creation, ignores) never create a version by
themselves — only a change of the effective employee business record does.
Provenance for each changed field/item is carried from the persisted candidate record — never
invented. NO target writes; this only records history M3B will later use for delivery + rollback.
"""
from __future__ import annotations

import json

from .prepare import item_identity_key
from .schema_loader import TargetSchema

_VERSIONED = {"READY_CREATE", "READY_UPDATE", "NO_CHANGE"}


def _cand_record(cand: dict) -> dict:
    return json.loads(cand["record"]) if isinstance(cand.get("record"), str) else (cand.get("record") or {})


def _cand_collections(cand: dict) -> dict:
    c = cand.get("collections")
    if isinstance(c, str):
        return json.loads(c) if c else {}
    return c or {}


def _cand_custom(cand: dict) -> list:
    c = cand.get("custom_attributes")
    if isinstance(c, str):
        return json.loads(c) if c else []
    return c or []


def _norm(v):
    if v is None or v == "" or v == []:
        return None
    return v


def compare_snapshots(a: dict, b: dict, schema: TargetSchema) -> dict:
    """Deterministic comparison of two complete snapshots (scalars + collections + custom attributes)."""
    fields, changed = [], []
    for f in schema.field_names:
        av, bv = _norm(a.get(f)), _norm(b.get(f))
        is_changed = av != bv
        kind = "unchanged"
        if is_changed:
            changed.append(f)
            kind = "added" if av is None else "cleared" if bv is None else "changed"
        fields.append({"field": f, "a": av, "b": bv, "changed": is_changed, "kind": kind})

    ca = {x.get("key"): x.get("value") for x in (a.get("custom_attributes") or []) if x.get("key")}
    cb = {x.get("key"): x.get("value") for x in (b.get("custom_attributes") or []) if x.get("key")}
    custom, changed_custom = [], []
    for k in sorted(set(ca) | set(cb)):
        av, bv = _norm(ca.get(k)), _norm(cb.get(k))
        is_changed = json.dumps(av, sort_keys=True, default=str) != json.dumps(bv, sort_keys=True, default=str)
        kind = "unchanged"
        if is_changed:
            changed_custom.append(k)
            kind = "added" if av is None else "cleared" if bv is None else "changed"
        custom.append({"field": k, "a": av, "b": bv, "changed": is_changed, "kind": kind})

    coll_rows, changed_colls = [], []
    for c in schema.collections:
        ia = {item_identity_key(c, it): it for it in (a.get("collections") or {}).get(c.key, []) or []}
        ib = {item_identity_key(c, it): it for it in (b.get("collections") or {}).get(c.key, []) or []}
        any_change = False
        for ident in sorted(set(ia) | set(ib)):
            xa, xb = ia.get(ident), ib.get(ident)
            if xa is None:
                kind, cf = "added", [f.name for f in c.fields if _norm(xb.get(f.name)) is not None]
            elif xb is None:
                kind, cf = "removed", [f.name for f in c.fields if _norm(xa.get(f.name)) is not None]
            else:
                cf = [f.name for f in c.fields if _norm(xa.get(f.name)) != _norm(xb.get(f.name))]
                kind = "changed" if cf else "unchanged"
            if kind != "unchanged":
                any_change = True
            coll_rows.append({"collection": c.key, "identity_key": ident, "kind": kind, "a": xa, "b": xb,
                              "changed_fields": cf if kind != "unchanged" else []})
        if any_change:
            changed_colls.append(c.key)
    return {"fields": fields, "custom_attributes": custom, "collections": coll_rows,
            "changed_fields": changed, "changed_custom_attributes": changed_custom,
            "changed_collections": changed_colls}


def _method_for(rec_f: dict, decision: str | None) -> str | None:
    if decision:
        return f"human ({decision})"
    rule = (rec_f or {}).get("rule") or ""
    if rule.startswith("human"):
        return "human"
    if rule == "reconcile":
        return "merged"
    if rule:
        return "normalized"
    return None


def _field_changes(before: dict, after: dict, diff: dict, cand: dict, schema: TargetSchema) -> list[dict]:
    """Per-field / per-item change list with real provenance (from the candidate record)."""
    cmp = compare_snapshots(before, after, schema)
    cand_record = _cand_record(cand)
    cand_colls = _cand_collections(cand)
    cand_custom = {c.get("key"): c for c in _cand_custom(cand)}
    out: list[dict] = []
    for row in cmp["fields"]:
        if not row["changed"]:
            continue
        f = row["field"]
        rec_f = cand_record.get(f) or {}
        d = diff.get(f) or {}
        out.append({"field": f, "from": row["a"], "to": row["b"], "method": _method_for(rec_f, d.get("decision")),
                    "provenance": rec_f.get("provenance") or []})
    for row in cmp["custom_attributes"]:
        if not row["changed"]:
            continue
        k = row["field"]
        rec_f = cand_custom.get(k) or {}
        d = diff.get(f"custom_attributes.{k}") or {}
        out.append({"field": f"custom_attributes.{k}", "from": row["a"], "to": row["b"],
                    "method": _method_for(rec_f, d.get("decision")), "provenance": rec_f.get("provenance") or []})
    for row in cmp["collections"]:
        if row["kind"] == "unchanged":
            continue
        coll, ident = row["collection"], row["identity_key"]
        provs: list = []
        c = schema.get_collection(coll)
        for it in cand_colls.get(coll, []) or []:
            vals = {k: (v or {}).get("value") for k, v in (it.get("fields") or {}).items()}
            if c and item_identity_key(c, vals) == ident:
                provs = [p for fv in (it.get("fields") or {}).values() for p in (fv or {}).get("provenance", [])]
                break
        decision = None
        for e in (diff.get(f"{coll}[]") or {}).get("items", []):
            if e.get("identity_key") == ident:
                decision = e.get("decision")
        out.append({"field": f"{coll}[{ident}]", "from": row["a"], "to": row["b"], "kind": row["kind"],
                    "changed_fields": row["changed_fields"],
                    "method": ("human (use_incoming)" if decision == "use_incoming" else
                               ("merged" if provs else None)),
                    "provenance": provs})
    return out


def _has_human_decision(diff: dict) -> bool:
    for d in (diff or {}).values():
        if not isinstance(d, dict):
            continue
        if d.get("decision"):
            return True
        for e in d.get("items", []) or []:
            if isinstance(e, dict) and e.get("decision"):
                return True
    return False


def build_and_store_versions(db, job_id: str, schema: TargetSchema, results: list[dict]) -> list[dict]:
    """Create employee versions from reconciliation results. Returns the versions created this run."""
    cands = {c["id"]: c for c in db.get_candidates(job_id)}
    # candidate_id -> (decision_note, issue_id) from resolved target-review decisions
    notes: dict[str, tuple[str | None, str | None]] = {}
    for iss in db.get_target_review_issues(job_id, status="resolved"):
        res = json.loads(iss["resolution"]) if iss["resolution"] else {}
        notes[iss["candidate_id"]] = (res.get("note") or res.get("reason"), iss["id"])

    created: list[dict] = []

    def _audit_change(cid, bk, ver, changed):
        parent_no = ver["version_no"] - 1
        if ver["version_no"] == 1:
            after = {"to_version": 1, "origin": ver["origin"]}
        else:
            after = {"from_version": parent_no, "to_version": ver["version_no"],
                     "changed_fields": [c["field"] for c in changed],
                     "origin": ver["origin"], "decision_note": ver["decision_note"]}
        db.add_audit(job_id, event_type="employee_version", actor=ver["created_by"],
                     source_ref={"candidate_id": cid, "business_key": bk,
                                 "field": (changed[0]["field"] if len(changed) == 1 else None),
                                 "version_id": ver["id"]},
                     before=({"version_no": parent_no} if ver["version_no"] > 1 else None),
                     after=after, reason=ver["change_reason"])

    for r in results:
        if r["outcome"] not in _VERSIONED:
            continue
        cid, bk = r["candidate_id"], r.get("business_key")
        baseline, desired = r.get("baseline"), r.get("desired")
        cand = cands.get(cid, {})
        existing = db.get_employee_versions(job_id, cid)

        if baseline is not None:
            # Existing target employee: ensure the baseline is v1, then the approved desired state.
            if not existing:
                v = db.add_employee_version(
                    job_id, cid, snapshot=baseline, origin="existing_target", created_by="system",
                    business_key=bk, change_reason="Existing target baseline",
                    target_revision=r.get("target_revision"), dedup=True)
                if v:
                    created.append(v); _audit_change(cid, bk, v, [])
            if desired is not None and db.snapshot_hash(desired) != db.snapshot_hash(baseline):
                changed = _field_changes(baseline, desired, r.get("diff") or {}, cand, schema)
                human = _has_human_decision(r.get("diff") or {})
                note, did = notes.get(cid, (None, None))
                v = db.add_employee_version(
                    job_id, cid, snapshot=desired, origin=("human" if human else "migration"),
                    created_by=("human" if human else "system"), business_key=bk,
                    change_reason=("Human-approved migration change" if human
                                   else "Migration update (safe fill of blank target data)"),
                    decision_note=(note if human else None), decision_id=(did if human else None),
                    target_revision=r.get("target_revision"), field_changes=changed, dedup=True)
                if v:
                    created.append(v); _audit_change(cid, bk, v, changed)
        elif desired is not None:
            # New employee: v1 is the first approved canonical migration state (no fake v0).
            base_for_changes = _cand_prev_snapshot(existing) if existing else {}
            changes = (None if not existing
                       else _field_changes(base_for_changes, desired, {}, cand, schema))
            v = db.add_employee_version(
                job_id, cid, snapshot=desired, origin="migration", created_by="system",
                business_key=bk,
                change_reason=("Initial canonical migration state" if not existing
                               else "Canonical migration state updated"),
                field_changes=changes, dedup=True)
            if v:
                created.append(v)
                _audit_change(cid, bk, v, [] if v["version_no"] == 1 else (changes or []))
    return created


def _cand_prev_snapshot(existing: list[dict]) -> dict:
    try:
        return json.loads(existing[-1]["snapshot"])
    except Exception:  # pragma: no cover - defensive
        return {}
