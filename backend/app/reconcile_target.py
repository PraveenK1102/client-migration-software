"""M3A: incoming-vs-existing-target reconciliation (schema-driven since M3A.2).

Separate from M2 incoming-vs-incoming reconciliation. For each ELIGIBLE prepared candidate:
look up the confirmed target employee via the HTTP gateway, persist a snapshot (with the
target revision, for M3B optimistic concurrency), and classify:

    READY_CREATE | READY_UPDATE | NO_CHANGE | REVIEW_REQUIRED | EXCLUDED

Conservative rules (section 12): never overwrite a non-empty target value with a different
non-empty incoming value automatically; never fuzzy-match by name; unsafe conflicts go to
human review. Human decisions (keep_existing / use_incoming per field or per collection item,
or exclude) are overlays; the whole thing recomputes deterministically and is idempotent.

The effective record is the complete snapshot: every core scalar field of the contract, each
structured collection (items compared by their schema-declared identity; target-only items are
always kept, incoming-only items are safe additions, same-identity items with different non-empty
values escalate) and the tenant custom attributes (compared by key). NO writes here.
"""
from __future__ import annotations

import hashlib
import json

from .prepare import effective_snapshot_from_candidate, item_identity_key
from .schema_loader import TargetSchema
from .target_gateway import TargetEmployeeGateway

RECON_VERSION = "recon.v2"


def _tid(job_id: str, candidate_id: str, field: str | None) -> str:
    return f"ti_{hashlib.sha1(f'{job_id}|{candidate_id}|{field or 'candidate'}'.encode()).hexdigest()[:12]}"


def _present(v) -> bool:
    if isinstance(v, list):
        return len(v) > 0
    return v is not None and str(v).strip() != ""


def _eq(a, b, field: str) -> bool:
    if a is None or b is None:
        return (a is None or a == "" or a == []) and (b is None or b == "" or b == [])
    if isinstance(a, list) or isinstance(b, list):
        return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    if field == "work_email" or field.endswith("email"):
        return str(a).strip().lower() == str(b).strip().lower()
    return str(a).strip() == str(b).strip()


def _target_snapshot(target: dict, schema: TargetSchema) -> dict:
    """A complete effective snapshot of a confirmed target record restricted to the contract."""
    out = {f: target.get(f) for f in schema.field_names}
    colls = target.get("collections") or {}
    out["collections"] = {}
    for c in schema.collections:
        items = []
        for it in colls.get(c.key, []) or []:
            items.append({f.name: it.get(f.name) for f in c.fields})
        out["collections"][c.key] = sorted(items, key=lambda x: json.dumps(x, sort_keys=True, default=str))
    out["custom_attributes"] = sorted(
        [{"definition_id": ca.get("definition_id"), "key": ca.get("key"), "value": ca.get("value")}
         for ca in (target.get("custom_attributes") or []) if ca.get("key")],
        key=lambda x: x["key"])
    return out


def _load_overlays(db, job_id: str) -> tuple[set[str], dict[tuple[str, str], str]]:
    """Return (excluded_candidate_ids, {(candidate_id, field_or_item_ref): 'keep_existing'|'use_incoming'})."""
    excluded: set[str] = set()
    field_decisions: dict[tuple[str, str], str] = {}
    for iss in db.get_target_review_issues(job_id, status="resolved"):
        res = json.loads(iss["resolution"]) if iss["resolution"] else {}
        action = res.get("action")
        if action == "exclude":
            excluded.add(iss["candidate_id"])
        elif action in ("keep_existing", "use_incoming"):
            aff = json.loads(iss["affected"]) if iss["affected"] else {}
            ref = iss["field"]
            if iss["issue_type"] == "collection_item_conflict":
                ref = f"{aff.get('collection')}[{aff.get('identity_key')}]"
            if ref:
                field_decisions[(iss["candidate_id"], ref)] = action
    return excluded, field_decisions


async def reconcile(db, gateway: TargetEmployeeGateway, job_id: str, schema: TargetSchema) -> dict:
    # Organization isolation: reconcile only against the SAME organization's existing target records.
    # The scoped gateway carries the job's organization on every lookup/read (never cross-org).
    gateway = gateway.for_organization(db.job_tenant(job_id))
    candidates = [c for c in db.get_candidates(job_id) if c["eligibility"] == "eligible"]
    excluded_ov, field_ov = _load_overlays(db, job_id)

    incoming = {c["id"]: effective_snapshot_from_candidate(c, schema) for c in candidates}
    emp_ids = [r.get("employee_id") for r in incoming.values() if _present(r.get("employee_id"))]
    emails = [r.get("work_email") for r in incoming.values() if _present(r.get("work_email"))]
    lookup = await gateway.lookup(emp_ids, emails)

    compare_fields = [f for f in schema.field_names if f != "employee_id"]
    snapshots: list[dict] = []
    results: list[dict] = []
    issues: list[dict] = []

    for cand in candidates:
        cid = cand["id"]
        bk = cand["business_key"]
        rec = incoming[cid]
        emp_id = rec.get("employee_id")
        email = rec.get("work_email")
        t_by_id = lookup.for_id(emp_id)
        t_by_email = lookup.for_email(email)

        matched = t_by_id or t_by_email
        match_basis = "employee_id" if t_by_id else ("work_email" if t_by_email else "none")
        target_record_id = matched.get("employee_id") if matched else None
        target_revision = matched.get("revision") if matched else None
        snapshots.append({"candidate_id": cid, "business_key": bk, "target_record_id": target_record_id,
                          "match_basis": match_basis, "target_revision": target_revision,
                          "target_payload": matched})

        incoming_snap = rec

        def result(outcome, diff=None, basis=match_basis, rid=target_record_id, rev=target_revision,
                   baseline=None, desired=None):
            results.append({"candidate_id": cid, "business_key": bk, "outcome": outcome,
                            "target_record_id": rid, "target_revision": rev, "match_basis": basis,
                            "diff": diff or {}, "baseline": baseline, "desired": desired})

        # Human exclusion wins.
        if cid in excluded_ov:
            result("EXCLUDED")
            continue

        # Identity-level conflicts (candidate-level review; only 'exclude' resolves in M3A).
        # affected carries the three parties so the UI can compare them without inferring from JSON.
        if t_by_id and t_by_email and t_by_id.get("employee_id") != t_by_email.get("employee_id"):
            issues.append({"id": _tid(job_id, cid, None), "candidate_id": cid, "business_key": bk,
                           "field": None, "issue_type": "id_email_mismatch",
                           "reason": (f"employee_id '{emp_id}' matches target {t_by_id.get('employee_id')} "
                                      f"but work_email matches target {t_by_email.get('employee_id')}."),
                           "incoming_value": email, "target_value": t_by_email.get("work_email"),
                           "match_basis": "conflict", "options": ["exclude"],
                           "affected": {"candidate_id": cid, "incoming": incoming_snap,
                                        "target_by_id": t_by_id, "target_by_email": t_by_email}})
            result("REVIEW_REQUIRED", basis="conflict",
                   rid=t_by_id.get("employee_id"), rev=t_by_id.get("revision"))
            continue
        if not t_by_id and t_by_email:
            issues.append({"id": _tid(job_id, cid, None), "candidate_id": cid, "business_key": bk,
                           "field": "work_email", "issue_type": "email_owned_by_other",
                           "reason": (f"Incoming new employee_id '{emp_id}' uses work_email owned by "
                                      f"confirmed target employee {t_by_email.get('employee_id')}."),
                           "incoming_value": email, "target_value": t_by_email.get("work_email"),
                           "match_basis": "work_email", "options": ["exclude"],
                           "affected": {"candidate_id": cid, "target_record_id": t_by_email.get("employee_id"),
                                        "incoming": incoming_snap, "target_by_email": t_by_email}})
            result("REVIEW_REQUIRED", basis="work_email",
                   rid=t_by_email.get("employee_id"), rev=t_by_email.get("revision"))
            continue
        if not t_by_id and not t_by_email:
            result("READY_CREATE", desired=incoming_snap)   # new employee -> canonical incoming state
            continue

        # Matched by employee_id: per-field comparison of the complete effective record.
        target = t_by_id
        baseline = _target_snapshot(target, schema)
        diff: dict = {}
        field_conflicts, safe_updates = [], []

        def compare_scalar(f: str, inc, tgt, *, ref: str, path_label: str, issue_field: str) -> object:
            """Returns the desired value for this field and records diff/issues. Never nulls a target."""
            decision = field_ov.get((cid, ref))
            if _eq(inc, tgt, f):
                diff[path_label] = {"incoming": inc, "target": tgt, "status": "no_change"}
                return tgt
            if not _present(tgt) and _present(inc):
                diff[path_label] = {"incoming": inc, "target": tgt, "status": "update"}
                safe_updates.append(path_label)
                return inc
            if not _present(inc) and _present(tgt):
                diff[path_label] = {"incoming": inc, "target": tgt, "status": "no_change"}  # never null target
                return tgt
            # both present and different -> conflict unless a human decided
            if decision == "use_incoming":
                diff[path_label] = {"incoming": inc, "target": tgt, "status": "update", "decision": "use_incoming"}
                safe_updates.append(path_label)
                return inc
            if decision == "keep_existing":
                diff[path_label] = {"incoming": inc, "target": tgt, "status": "no_change", "decision": "keep_existing"}
                return tgt
            diff[path_label] = {"incoming": inc, "target": tgt, "status": "conflict"}
            field_conflicts.append(path_label)
            issues.append({"id": _tid(job_id, cid, issue_field), "candidate_id": cid, "business_key": bk,
                           "field": issue_field, "issue_type": "value_conflict",
                           "reason": (f"'{path_label}' differs from the confirmed target value; automatic "
                                      f"overwrite of a non-empty target value is unsafe."),
                           "incoming_value": json.dumps(inc) if isinstance(inc, list) else str(inc),
                           "target_value": json.dumps(tgt) if isinstance(tgt, list) else str(tgt),
                           "match_basis": "employee_id",
                           "options": ["keep_existing", "use_incoming", "exclude"],
                           "affected": {"candidate_id": cid, "field": issue_field, "incoming": incoming_snap,
                                        "target": target}})
            return tgt

        desired = dict(baseline)
        desired["employee_id"] = target.get("employee_id") or emp_id
        for f in compare_fields:
            desired[f] = compare_scalar(f, rec.get(f), target.get(f), ref=f, path_label=f, issue_field=f)

        # Tenant custom attributes (compared by key; the target's may be empty).
        t_custom = {ca["key"]: ca for ca in baseline.get("custom_attributes", [])}
        i_custom = {ca["key"]: ca for ca in rec.get("custom_attributes", [])}
        desired_custom: dict[str, dict] = dict(t_custom)
        for key in sorted(set(t_custom) | set(i_custom)):
            path = f"custom_attributes.{key}"
            inc_v = (i_custom.get(key) or {}).get("value")
            tgt_v = (t_custom.get(key) or {}).get("value")
            val = compare_scalar(path, inc_v, tgt_v, ref=path, path_label=path, issue_field=path)
            if _present(val):
                desired_custom[key] = {"definition_id": (i_custom.get(key) or t_custom.get(key) or {}).get("definition_id"),
                                       "key": key, "value": val}
        desired["custom_attributes"] = sorted(desired_custom.values(), key=lambda x: x["key"])

        # Structured collections: identity-keyed item comparison.
        desired["collections"] = {}
        for c in schema.collections:
            t_items = {item_identity_key(c, it): it for it in baseline["collections"].get(c.key, [])}
            i_items = {item_identity_key(c, it): it for it in rec.get("collections", {}).get(c.key, [])}
            entries = []
            merged: dict[str, dict] = dict(t_items)
            coll_status = "no_change"
            for ident in sorted(set(t_items) | set(i_items)):
                ti, ii = t_items.get(ident), i_items.get(ident)
                ref = f"{c.key}[{ident}]"
                decision = field_ov.get((cid, ref))
                if ti is None:
                    entries.append({"identity_key": ident, "status": "update", "incoming": ii, "target": None})
                    merged[ident] = ii
                    coll_status = "update" if coll_status != "conflict" else coll_status
                    continue
                if ii is None:
                    entries.append({"identity_key": ident, "status": "no_change", "incoming": None, "target": ti})
                    continue  # target-only item is always kept
                changed = [f.name for f in c.fields if not _eq(ii.get(f.name), ti.get(f.name), f.name)]
                unsafe = [f for f in changed if _present(ti.get(f)) and _present(ii.get(f))]
                fill = [f for f in changed if not _present(ti.get(f)) and _present(ii.get(f))]
                if not changed:
                    entries.append({"identity_key": ident, "status": "no_change", "incoming": ii, "target": ti})
                    continue
                if unsafe and decision == "use_incoming":
                    new_item = dict(ti)
                    for f in changed:
                        if _present(ii.get(f)):
                            new_item[f] = ii.get(f)
                    merged[ident] = new_item
                    entries.append({"identity_key": ident, "status": "update", "incoming": ii, "target": ti,
                                    "decision": "use_incoming", "changed_fields": changed})
                    coll_status = "update" if coll_status != "conflict" else coll_status
                elif unsafe and decision == "keep_existing":
                    entries.append({"identity_key": ident, "status": "no_change", "incoming": ii, "target": ti,
                                    "decision": "keep_existing", "changed_fields": changed})
                elif unsafe:
                    coll_status = "conflict"
                    entries.append({"identity_key": ident, "status": "conflict", "incoming": ii, "target": ti,
                                    "changed_fields": unsafe})
                    label = f"{c.key}[]"
                    field_conflicts.append(f"{c.key}[{ident}]")
                    issues.append({"id": _tid(job_id, cid, f"{c.key}[{ident}]"), "candidate_id": cid,
                                   "business_key": bk, "field": label, "issue_type": "collection_item_conflict",
                                   "reason": (f"The same {c.label.lower()} item ({ident}) exists in the confirmed "
                                              f"target with different values for {unsafe}; automatic overwrite "
                                              f"of a non-empty target value is unsafe."),
                                   "incoming_value": json.dumps(ii, sort_keys=True, default=str),
                                   "target_value": json.dumps(ti, sort_keys=True, default=str),
                                   "match_basis": "employee_id",
                                   "options": ["keep_existing", "use_incoming", "exclude"],
                                   "affected": {"candidate_id": cid, "collection": c.key, "identity_key": ident,
                                                "changed_fields": unsafe, "incoming_item": ii, "target_item": ti,
                                                "incoming": incoming_snap, "target": target}})
                else:
                    new_item = dict(ti)
                    for f in fill:
                        new_item[f] = ii.get(f)
                    merged[ident] = new_item
                    entries.append({"identity_key": ident, "status": "update", "incoming": ii, "target": ti,
                                    "changed_fields": fill})
                    coll_status = "update" if coll_status != "conflict" else coll_status
            if entries:
                diff[f"{c.key}[]"] = {"status": coll_status, "items": entries}
                if coll_status == "update":
                    safe_updates.append(f"{c.key}[]")
            desired["collections"][c.key] = sorted(merged.values(),
                                                   key=lambda x: json.dumps(x, sort_keys=True, default=str))

        if field_conflicts:
            result("REVIEW_REQUIRED", diff, baseline=baseline, desired=None)  # unresolved -> no version
        elif safe_updates:
            result("READY_UPDATE", diff, baseline=baseline, desired=desired)
        else:
            result("NO_CHANGE", diff, baseline=baseline, desired=desired)

    counts = _counts(results)
    return {"snapshots": snapshots, "results": results, "issues": _dedupe(issues), "counts": counts}


def _dedupe(issues: list[dict]) -> list[dict]:
    seen = {}
    for i in issues:
        seen[i["id"]] = i
    return list(seen.values())


def _counts(results: list[dict]) -> dict:
    out = {"prepared": len(results), "ready_create": 0, "ready_update": 0, "no_change": 0,
           "review_required": 0, "excluded": 0}
    m = {"READY_CREATE": "ready_create", "READY_UPDATE": "ready_update", "NO_CHANGE": "no_change",
         "REVIEW_REQUIRED": "review_required", "EXCLUDED": "excluded"}
    for r in results:
        out[m[r["outcome"]]] += 1
    return out


def reconciliation_invariant_error(db, job_id: str, results: list[dict]) -> str | None:
    """reconciliation_complete requires: no open mapping/record/target issues, M2 blocked==0,
    and every non-excluded candidate is READY_CREATE|READY_UPDATE|NO_CHANGE."""
    open_map = len(db.get_issues(job_id, status="open"))
    open_rec = len(db.get_record_issues(job_id, status="open"))
    open_tgt = len(db.get_target_review_issues(job_id, status="open"))
    blocked = len([c for c in db.get_candidates(job_id) if c["eligibility"] == "blocked"])
    unresolved = [r for r in results if r["outcome"] == "REVIEW_REQUIRED"]
    if open_map or open_rec or open_tgt or blocked or unresolved:
        return (f"reconciliation not complete: open_mapping={open_map}, open_record={open_rec}, "
                f"open_target={open_tgt}, blocked={blocked}, review_required={len(unresolved)}")
    return None


def reconcile_single(db, job_id: str, candidate_id: str, current_target: dict,
                     schema) -> dict:
    """Re-reconcile a SINGLE candidate against a FRESH target record (used after stale-target
    refetch in M3B delivery). Returns a single reconciliation-result dict."""
    from app.prepare import effective_snapshot_from_candidate

    candidates = db.get_candidates(job_id)
    cand = next((c for c in candidates if c["id"] == candidate_id), None)
    if cand is None:
        raise ValueError(f"candidate {candidate_id} not found for job {job_id}")

    excluded_ov, field_ov = _load_overlays(db, job_id)
    incoming_snap = effective_snapshot_from_candidate(cand, schema)

    cid = cand["id"]
    bk = cand["business_key"]
    emp_id = incoming_snap.get("employee_id")
    matched = current_target
    match_basis = "employee_id"
    target_record_id = matched.get("employee_id") if matched else None
    target_revision = matched.get("revision") if matched else None
    baseline = _target_snapshot(matched, schema) if matched else {}
    desired = dict(incoming_snap)

    if cid in excluded_ov:
        return {"candidate_id": cid, "business_key": bk, "outcome": "EXCLUDED",
                "target_record_id": target_record_id, "target_revision": target_revision,
                "match_basis": match_basis, "diff": {}, "baseline": baseline, "desired": desired}

    compare_fields = [f for f in schema.field_names if f != "employee_id"]
    diff: dict = {}
    has_conflict = False
    has_change = False

    for f in compare_fields:
        inc = incoming_snap.get(f)
        tgt = baseline.get(f)
        # Check for field overlay
        ov_key = (cid, f)
        if ov_key in field_ov:
            side = field_ov[ov_key]
            winning = tgt if side == "keep_existing" else inc
            diff[f] = {"incoming": inc, "target": tgt, "status": "no_change" if winning == tgt else "update",
                       "decision": {"winning_value": winning, "winning_side": side}}
            if winning != tgt:
                has_change = True
            continue
        if _eq(inc, tgt, f):
            diff[f] = {"incoming": inc, "target": tgt, "status": "no_change"}
        elif not _present(tgt):
            diff[f] = {"incoming": inc, "target": tgt, "status": "update"}
            has_change = True
        elif not _present(inc):
            diff[f] = {"incoming": inc, "target": tgt, "status": "no_change"}
        else:
            diff[f] = {"incoming": inc, "target": tgt, "status": "conflict"}
            has_conflict = True

    # Check collections (simplified: mark as update if any difference)
    for coll_key in (schema.collections or {}):
        inc_items = (incoming_snap.get("collections") or {}).get(coll_key, [])
        tgt_items = (baseline.get("collections") or {}).get(coll_key, [])
        if inc_items != tgt_items:
            has_change = True
            diff[f"{coll_key}[]"] = {"status": "update", "items": []}

    if has_conflict:
        outcome = "REVIEW_REQUIRED"
    elif has_change:
        outcome = "READY_UPDATE"
    else:
        outcome = "NO_CHANGE"

    return {"candidate_id": cid, "business_key": bk, "outcome": outcome,
            "target_record_id": target_record_id, "target_revision": target_revision,
            "match_basis": match_basis, "diff": diff, "baseline": baseline, "desired": desired}
