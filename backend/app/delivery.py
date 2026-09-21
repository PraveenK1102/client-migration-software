"""M3B delivery engine — plan, execute, classify, retry, and roll back target writes.

The delivery layer sits between the reconciliation stage and the confirmed target system. It
NEVER writes to the target directly; every call goes through ``TargetEmployeeGateway``.

Lifecycle:

    1. ``plan_delivery(db, job_id, schema)``
       Reads the reconciliation results and creates one ``delivery_operations`` row per
       READY_CREATE or READY_UPDATE candidate. The plan is PERSISTED before any write begins.
       NO_CHANGE / EXCLUDED candidates get no operation. REVIEW_REQUIRED blocks delivery.

    2. ``execute_delivery_operation(db, gateway, op, settings)``
       Sends one CREATE or UPDATE via the gateway, records every attempt in the ledger,
       classifies the result, and transitions the operation status. Idempotency is per-operation
       (same key replayed on crash recovery). Retry is NOT inside this function — the durable
       work queue drives retries.

    3. ``classify_target_error(err) -> (retryable, category, retry_after)``
       Pure classification of gateway errors into retryable / terminal / stale-target.

    4. ``handle_stale_target(db, gateway, job_id, op)``
       On 409 revision_conflict: re-fetches the target, persists new evidence, invalidates
       stale target-review decisions, re-runs reconciliation for this candidate, and transitions
       the operation to STALE_TARGET.

    5. ``plan_rollback`` / ``execute_rollback_operation``
       CREATE → compensating DELETE; UPDATE → reverse patch restoring ``before_snapshot``.
       A rollback writes a new employee version with origin ``rollback`` and ``restores_version_id``
       pointing to the version before the rolled-back write.

All functions are sync-compatible with an asyncio loop (they await gateway calls). The delivery
module never holds a DB transaction across a target HTTP call.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from app.db import Database
from app.target_gateway import TargetEmployeeGateway, TargetResponseError, TargetUnavailable, TargetWriteResult

log = logging.getLogger(__name__)


class StaleRefreshTransient(Exception):
    """The stale-target re-fetch (a ``GET`` — READ evidence, never a target WRITE) failed with a
    transient error (transport/timeout, 429, or 5xx).

    The owning ``DELIVER_OP`` work item must be retried so stale handling RESUMES
    (refetch -> reconcile -> replan/review/terminalize); the old revision-bound UPDATE must NEVER be
    re-sent. Carries the optional ``Retry-After`` from a 429 so the worker can honour it. Because this
    is a read, NO ``delivery_attempts`` row is created for it — the retry is queue/audit evidence, not
    a delivery write attempt."""

    def __init__(self, category: str, retry_after: float | None = None) -> None:
        self.category = category
        self.retry_after = retry_after
        super().__init__(f"stale refresh transient failure: {category}")


def deliver_work_key(db: Database, op_id: str) -> str:
    """Generation-aware DELIVER_OP work-item key.

    The work queue's ``enqueue_work`` is INSERT-OR-IGNORE on a UNIQUE idempotency key, so re-using
    a fixed ``work:deliver:{op}`` key would SILENTLY drop the re-enqueue of a re-planned operation
    (e.g. after stale-target → human review → replan), stranding a PLANNED op with no claimable work
    item and hanging the job in ``delivering``. Binding the key to the current attempt generation
    guarantees each fresh plan gets its own claimable work item.
    """
    return f"work:deliver:{op_id}:g{db.delivery_attempt_count(op_id)}"


# ===================== delivery plan =====================================================

def plan_delivery(db: Database, job_id: str, schema: dict) -> dict:
    """Create delivery_operations rows for every deliverable reconciliation result.

    Returns ``{"planned": int, "skipped_no_change": int, "skipped_excluded": int,
    "skipped_review": int, "blocked": bool}``.
    """
    recon = db.get_target_reconciliation(job_id)
    if not recon:
        raise ValueError("no reconciliation results to plan delivery from")

    planned = skipped_nc = skipped_ex = skipped_rev = 0
    blocked = False

    for r in recon:
        outcome = r["outcome"]
        cid = r["candidate_id"]
        bk = r["business_key"]

        # An operation may already exist for this candidate. STALE_TARGET means a prior planned write
        # hit a 409 and the candidate went through refetch → (human review) → full re-reconciliation.
        # Every branch below must resolve that STALE_TARGET operation to a consistent next state.
        existing = db.get_delivery_operation_for_candidate(job_id, cid)
        existing_stale = bool(existing and existing["status"] == "STALE_TARGET")

        if outcome == "REVIEW_REQUIRED":
            skipped_rev += 1
            blocked = True
            continue

        if outcome == "NO_CHANGE":
            skipped_nc += 1
            if existing_stale:
                # The write originally planned against the old revision is no longer needed.
                db.update_delivery_operation(
                    existing["id"], status="SKIPPED_NO_CHANGE", last_error=None,
                    target_revision_after=r.get("target_revision"), work_item_id=None)
                db.add_audit(job_id, event_type="delivery_op_terminalized", actor="system",
                             source_ref=existing["id"], after={
                                 "candidate_id": cid, "from": "STALE_TARGET", "to": "SKIPPED_NO_CHANGE",
                                 "reason": "re-reconciliation after review determined no target write is needed"})
            continue

        if outcome == "EXCLUDED":
            skipped_ex += 1
            if existing_stale:
                db.update_delivery_operation(
                    existing["id"], status="SKIPPED_EXCLUDED", last_error=None, work_item_id=None)
                db.add_audit(job_id, event_type="delivery_op_terminalized", actor="system",
                             source_ref=existing["id"], after={
                                 "candidate_id": cid, "from": "STALE_TARGET", "to": "SKIPPED_EXCLUDED",
                                 "reason": "re-reconciliation after review excluded the candidate"})
            continue

        # A non-stale existing operation is already planned/in-flight/terminal — idempotent skip.
        if existing and not existing_stale:
            planned += 1
            continue

        if outcome == "READY_CREATE":
            payload = _build_create_payload(db, job_id, cid, schema)
            employee_id = payload.get("employee_id")
            versions = db.get_employee_versions(job_id, cid)
            desired_vid = versions[-1]["id"] if versions else None
            if existing_stale:
                # A stale UPDATE legitimately became a CREATE (target removed externally): refresh the
                # whole execution generation with a fresh CREATE key and clear stale success metadata.
                db.update_delivery_operation(
                    existing["id"], status="PLANNED", op_type="CREATE", payload=payload,
                    expected_target_revision=None, target_record_id=None, employee_id=employee_id,
                    before_snapshot=None, desired_version_id=desired_vid,
                    idempotency_key=f"deliver:{job_id}:{cid}:create", last_error=None,
                    target_revision_after=None, target_request_id=None, work_item_id=None)
            else:
                db.create_delivery_operation(
                    job_id=job_id, candidate_id=cid, employee_id=employee_id,
                    op_type="CREATE", payload=payload, expected_target_revision=None,
                    target_record_id=None, before_snapshot=None,
                    desired_version_id=desired_vid, idempotency_key=f"deliver:{job_id}:{cid}:create")
            planned += 1

        elif outcome == "READY_UPDATE":
            payload_patch = _build_update_patch(db, job_id, cid, r, schema)
            target_rev = r.get("target_revision")
            target_id = r.get("target_record_id") or bk
            before = _get_before_snapshot(db, job_id, cid)
            idem_key = f"deliver:{job_id}:{cid}:update:{target_rev}"
            versions = db.get_employee_versions(job_id, cid)
            desired_vid = versions[-1]["id"] if versions else None
            if existing_stale:
                # Refresh ALL evidence-bound execution fields to the CURRENT revision so the retry can
                # never reuse the stale rev-N idempotency key / before-snapshot / patch. The new key is
                # deterministic and revision-bound, so it will not collide with the abandoned attempt.
                db.update_delivery_operation(
                    existing["id"], status="PLANNED", op_type="UPDATE", payload=payload_patch,
                    expected_target_revision=target_rev, target_record_id=target_id,
                    employee_id=target_id, before_snapshot=before, desired_version_id=desired_vid,
                    idempotency_key=idem_key, last_error=None, target_revision_after=None,
                    target_request_id=None, work_item_id=None)
            else:
                db.create_delivery_operation(
                    job_id=job_id, candidate_id=cid, employee_id=target_id,
                    op_type="UPDATE", payload=payload_patch,
                    expected_target_revision=target_rev, target_record_id=target_id,
                    before_snapshot=before, desired_version_id=desired_vid,
                    idempotency_key=idem_key)
            planned += 1

    db.add_audit(job_id, event_type="delivery_plan_created", actor="system",
                 after={"planned": planned, "no_change": skipped_nc,
                                   "excluded": skipped_ex, "review_blocked": skipped_rev})
    return {"planned": planned, "skipped_no_change": skipped_nc, "skipped_excluded": skipped_ex,
            "skipped_review": skipped_rev, "blocked": blocked}


def _build_create_payload(db: Database, job_id: str, candidate_id: str, schema: dict) -> dict:
    """Full employee record for POST /employees (CREATE)."""
    from app.prepare import effective_snapshot_from_candidate
    cand = db.get_candidates(job_id)
    cand_row = next((c for c in cand if c["id"] == candidate_id), None)
    if cand_row is None:
        raise ValueError(f"candidate {candidate_id} not found")
    snap = effective_snapshot_from_candidate(cand_row, schema)
    return snap


def _build_update_patch(db: Database, job_id: str, candidate_id: str, recon_result: dict,
                        schema: dict) -> dict:
    """Intended change patch for PATCH /employees/{id} (UPDATE). Sends only fields that differ."""
    diff_raw = recon_result.get("diff")
    diff = json.loads(diff_raw) if isinstance(diff_raw, str) else (diff_raw or {})
    patch: dict = {}
    collections_patch: dict = {}

    for key, entry in diff.items():
        if not isinstance(entry, dict):
            continue
        status = entry.get("status")
        if key.endswith("[]"):
            # Collection diff
            coll_key = key.rstrip("[]")
            if status in ("update", "add"):
                items = entry.get("items", [])
                desired_items = []
                for it in items:
                    if isinstance(it, dict) and it.get("status") in ("add", "update"):
                        desired_items.append(it.get("incoming") or it)
                    elif isinstance(it, dict) and it.get("status") in ("no_change", "target_only"):
                        # Keep target-only items — never silently remove them
                        desired_items.append(it.get("target") or it.get("incoming") or it)
                    elif isinstance(it, dict):
                        desired_items.append(it.get("incoming") or it.get("target") or it)
                if desired_items:
                    collections_patch[coll_key] = desired_items
        elif status == "update":
            incoming = entry.get("incoming")
            if incoming is not None:
                patch[key] = incoming
        elif status == "conflict":
            # Conflict should have been resolved in review; use the winning value
            decision = entry.get("decision")
            if decision and decision.get("winning_value") is not None:
                patch[key] = decision["winning_value"]

    if collections_patch:
        patch["collections"] = collections_patch

    # Custom attributes
    from app.prepare import effective_snapshot_from_candidate
    cand = db.get_candidates(job_id)
    cand_row = next((c for c in cand if c["id"] == candidate_id), None)
    if cand_row:
        snap = effective_snapshot_from_candidate(cand_row, schema)
        ca = snap.get("custom_attributes")
        if ca:
            # Include custom_attributes in the patch if they differ from before
            before = _get_before_snapshot(db, job_id, candidate_id)
            before_ca = (before or {}).get("custom_attributes", [])
            before_by_key = {(c.get("key") or c.get("definition_id")): c for c in before_ca}
            new_ca = []
            for c in ca:
                ck = c.get("key") or c.get("definition_id")
                old = before_by_key.get(ck)
                if old is None or c.get("value") != old.get("value"):
                    new_ca.append(c)
            if new_ca:
                patch["custom_attributes"] = new_ca

    return patch


def _get_before_snapshot(db: Database, job_id: str, candidate_id: str) -> dict | None:
    snaps = db.get_target_snapshots(job_id)
    for s in snaps:
        if s["candidate_id"] == candidate_id and s.get("target_payload"):
            p = s["target_payload"]
            return json.loads(p) if isinstance(p, str) else p
    return None


# ===================== error classification =============================================

def classify_target_error(err: TargetResponseError) -> tuple[bool, str, float | None]:
    """Returns (retryable, category, retry_after_seconds)."""
    sc = err.status_code
    code = err.code

    # Revision conflict — special: STALE_TARGET, never blindly retry
    if sc == 409 and code == "revision_conflict":
        return False, "revision_conflict", None
    if sc == 409 and code == "already_exists":
        return False, "already_exists", None
    if sc == 409 and code == "email_in_use":
        return False, "email_in_use", None

    # Validation — deterministic failure, never retry without fixing data
    if sc == 422:
        return False, f"validation_{code or 'error'}", None

    # Auth — systemic problem, not per-request
    if sc in (401, 403):
        return False, f"auth_{sc}", None

    # Rate limited — honour Retry-After
    if sc == 429:
        ra = err.retry_after
        return True, "rate_limited", ra

    # Server errors — retryable
    if sc in (500, 502, 503, 504):
        return True, f"server_{sc}", None

    # Everything else — not retryable
    return False, f"http_{sc}", None


def classify_transport_error(err: TargetUnavailable) -> tuple[bool, str, float | None]:
    return True, "transport_error", None


# ===================== execute one delivery operation ====================================

async def execute_delivery_operation(db: Database, gateway: TargetEmployeeGateway, op: dict,
                                     settings) -> str:
    """Execute one CREATE or UPDATE. Returns the result status of the operation."""
    # Organization isolation: every target write for this operation is scoped to the job's
    # organization (carried on the X-Organization-ID header), so a CREATE/UPDATE can never touch
    # another organization's record even when two organizations share an employee id.
    gateway = gateway.for_organization(db.job_tenant(op["job_id"]))
    op_id = op["id"]
    op_type = op["op_type"]
    payload = json.loads(op["payload"]) if isinstance(op["payload"], str) else op["payload"]
    idem_key = op["idempotency_key"]
    attempt_no = db.delivery_attempt_count(op_id) + 1

    # Record attempt BEFORE the call (crash between call and commit → same idem key replays)
    atm_id = db.add_delivery_attempt(operation_id=op_id, attempt_no=attempt_no, action="DELIVER")
    db.update_delivery_operation(op_id, attempt_count=attempt_no, status="PROCESSING")

    try:
        if op_type == "CREATE":
            result = await gateway.create_employee(payload, idempotency_key=idem_key)
        elif op_type == "UPDATE":
            eid = op["employee_id"] or op["target_record_id"]
            exp_rev = op["expected_target_revision"]
            result = await gateway.update_employee(eid, payload, expected_revision=exp_rev,
                                                   idempotency_key=idem_key)
        elif op_type == "DELETE":
            # Migration-admin delete of an already-synced employee. Revision-checked (a stale delete
            # becomes STALE_TARGET, never a blind delete) and idempotent, exactly like a write.
            eid = op["employee_id"] or op["target_record_id"]
            exp_rev = op["expected_target_revision"]
            result = await gateway.delete_employee(eid, expected_revision=exp_rev,
                                                   idempotency_key=idem_key)
        else:
            raise ValueError(f"unknown op_type: {op_type}")

        # Check for crash-after-send testing flag
        if getattr(settings, "delivery_crash_after_send", False):
            log.warning("CRASH-AFTER-SEND: simulating hard exit after target accepted write")
            import os
            os._exit(42)

        # Success
        db.complete_delivery_attempt(atm_id, http_status=result.status_code, retryable=False,
                                     error_category=None, retry_after=None,
                                     target_request_id=result.request_id,
                                     response_meta={"revision": result.revision, "replayed": result.replayed},
                                     result="success")
        db.update_delivery_operation(op_id, status="SUCCEEDED",
                                     target_revision_after=result.revision,
                                     target_request_id=result.request_id,
                                     employee_id=_employee_id_from_result(result, op))
        db.add_audit(op["job_id"], event_type="delivery_succeeded", actor="system",
                     source_ref=op_id, after={
                         "op_type": op_type, "candidate_id": op["candidate_id"],
                         "revision": result.revision, "replayed": result.replayed,
                         "attempt": attempt_no})
        return "SUCCEEDED"

    except TargetResponseError as e:
        retryable, category, retry_after = classify_target_error(e)
        db.complete_delivery_attempt(atm_id, http_status=e.status_code, retryable=retryable,
                                     error_category=category, retry_after=retry_after,
                                     target_request_id=e.request_id,
                                     response_meta=e.body, result="retryable" if retryable else
                                     ("conflict" if category == "revision_conflict" else "terminal"))

        if category == "revision_conflict":
            db.update_delivery_operation(op_id, status="STALE_TARGET", last_error=str(e)[:500])
            db.add_audit(op["job_id"], event_type="stale_target_detected", actor="system",
                         source_ref=op_id, after={
                             "candidate_id": op["candidate_id"], "category": category,
                             "current_revision": (e.body or {}).get("current_revision")})
            return "STALE_TARGET"

        if retryable:
            db.update_delivery_operation(op_id, status="RETRYABLE",
                                         last_error=f"{category}: {str(e)[:300]}")
            db.add_audit(op["job_id"], event_type="delivery_retry_scheduled", actor="system",
                         source_ref=op_id, after={
                             "category": category, "attempt": attempt_no,
                             "retry_after": retry_after})
            return "RETRYABLE"

        db.update_delivery_operation(op_id, status="FAILED", last_error=f"{category}: {str(e)[:300]}")
        db.add_audit(op["job_id"], event_type="delivery_failed", actor="system",
                     source_ref=op_id, after={
                         "category": category, "attempt": attempt_no, "status_code": e.status_code})
        return "FAILED"

    except TargetUnavailable as e:
        _, category, _ = classify_transport_error(e)
        db.complete_delivery_attempt(atm_id, http_status=None, retryable=True,
                                     error_category=category, retry_after=None,
                                     target_request_id=None, response_meta=None,
                                     result="retryable")
        db.update_delivery_operation(op_id, status="RETRYABLE",
                                     last_error=f"{category}: {str(e)[:300]}")
        return "RETRYABLE"


def _employee_id_from_result(result: TargetWriteResult, op: dict) -> str | None:
    if result.employee and result.employee.get("employee_id"):
        return str(result.employee["employee_id"])
    return op.get("employee_id")


# ===================== stale-target handling ============================================

async def handle_stale_target(db: Database, gateway: TargetEmployeeGateway, job_id: str,
                              op: dict, schema: dict) -> str:
    """Re-fetch target, persist new evidence, invalidate stale decisions, re-reconcile.

    Returns one of: 'replanned' (safe to retry automatically against the new revision),
    'review_required' (needs human), 'no_change' / 'excluded' (op terminalized SKIPPED_*), or
    'failed' (op terminalized FAILED). Raises ``StaleRefreshTransient`` when the re-fetch (a READ)
    fails transiently, so the caller reschedules a durable retry that RESUMES stale handling — the
    old revision-bound UPDATE is never re-sent.
    """
    # Organization isolation: the stale re-fetch (GET) and any re-reconcile stay within the job's
    # organization — a stale refresh can never read another organization's record with the same id.
    gateway = gateway.for_organization(db.job_tenant(job_id))
    employee_id = op["employee_id"] or op["target_record_id"]
    if not employee_id:
        db.update_delivery_operation(op["id"], status="FAILED",
                                     last_error="cannot re-fetch: no employee_id", work_item_id=None)
        return "failed"

    # Re-fetch the current target record. This is a READ (GET), NOT a target write: a transient
    # transport/429/5xx failure must RESUME stale handling on a durable retry (raise
    # StaleRefreshTransient — the worker reschedules the same work item), and must NEVER fall back to
    # blindly re-sending the old revision-bound UPDATE. A non-retryable read error (auth 401/403 or
    # any other terminal status) terminalizes the operation safely, with no write and no blind retry.
    try:
        current = await gateway.get_employee(employee_id)
    except TargetUnavailable as e:
        raise StaleRefreshTransient("transport_error", None) from e
    except TargetResponseError as e:
        retryable, category, retry_after = classify_target_error(e)
        if retryable:
            raise StaleRefreshTransient(category, retry_after) from e
        db.update_delivery_operation(op["id"], status="FAILED",
                                     last_error=f"stale_refresh_failed:{category}", work_item_id=None)
        db.add_audit(job_id, event_type="stale_refresh_failed", actor="system",
                     source_ref=op["id"], after={
                         "candidate_id": op["candidate_id"], "category": category,
                         "status_code": e.status_code})
        return "failed"

    if current is None:
        db.update_delivery_operation(op["id"], status="FAILED",
                                     last_error="target employee disappeared during stale handling",
                                     work_item_id=None)
        db.add_audit(job_id, event_type="stale_refresh_failed", actor="system",
                     source_ref=op["id"], after={
                         "candidate_id": op["candidate_id"], "category": "target_missing"})
        return "failed"

    new_rev = current.get("revision")
    db.add_audit(job_id, event_type="stale_target_refetched", actor="system",
                 source_ref=op["id"], after={
                     "candidate_id": op["candidate_id"], "new_revision": new_rev,
                     "old_revision": op.get("expected_target_revision")})

    # Invalidate any target-review decisions that depended on the old revision
    _invalidate_stale_decisions(db, job_id, op["candidate_id"],
                                old_revision=op.get("expected_target_revision"))

    db.update_target_snapshot(job_id, op["candidate_id"],
                              target_payload=current, target_revision=new_rev)

    # Re-run reconciliation for THIS candidate against the new target evidence
    from app.reconcile_target import reconcile_single
    try:
        new_result = reconcile_single(db, job_id, op["candidate_id"], current, schema)
    except Exception as exc:
        db.update_delivery_operation(op["id"], status="FAILED",
                                     last_error=f"re-reconciliation failed: {str(exc)[:300]}",
                                     work_item_id=None)
        db.add_audit(job_id, event_type="stale_refresh_failed", actor="system",
                     source_ref=op["id"], after={
                         "candidate_id": op["candidate_id"], "category": "re_reconciliation_error"})
        return "failed"

    new_outcome = new_result.get("outcome", "REVIEW_REQUIRED")
    db.update_target_reconciliation_row(job_id, op["candidate_id"],
                                        outcome=new_outcome, target_revision=new_rev,
                                        diff=new_result.get("diff"))

    db.add_audit(job_id, event_type="stale_target_rereconciled", actor="system",
                 source_ref=op["id"], after={
                     "candidate_id": op["candidate_id"], "new_outcome": new_outcome,
                     "new_revision": new_rev})

    if new_outcome == "REVIEW_REQUIRED":
        db.update_delivery_operation(op["id"], status="STALE_TARGET",
                                     last_error="re-reconciliation requires human review")
        return "review_required"

    if new_outcome == "NO_CHANGE":
        db.update_delivery_operation(op["id"], status="SKIPPED_NO_CHANGE",
                                     last_error=None, target_revision_after=new_rev,
                                     work_item_id=None)
        db.add_audit(job_id, event_type="delivery_op_terminalized", actor="system",
                     source_ref=op["id"], after={
                         "candidate_id": op["candidate_id"], "from": "STALE_TARGET",
                         "to": "SKIPPED_NO_CHANGE",
                         "reason": "immediate re-reconciliation found no target write is needed"})
        return "no_change"

    if new_outcome == "EXCLUDED":
        # No target write is needed — this is a no-write terminal, NOT a failure.
        db.update_delivery_operation(op["id"], status="SKIPPED_EXCLUDED",
                                     last_error=None, work_item_id=None)
        db.add_audit(job_id, event_type="delivery_op_terminalized", actor="system",
                     source_ref=op["id"], after={
                         "candidate_id": op["candidate_id"], "from": "STALE_TARGET",
                         "to": "SKIPPED_EXCLUDED",
                         "reason": "immediate re-reconciliation excluded the candidate (no write needed)"})
        return "excluded"

    if new_outcome == "READY_UPDATE":
        new_patch = _build_update_patch(db, job_id, op["candidate_id"],
                                        new_result, schema)
        new_idem = f"deliver:{job_id}:{op['candidate_id']}:update:{new_rev}"
        # Refresh ALL evidence-bound fields to the new revision and RELEASE the execution claim so the
        # freshly-enqueued DELIVER_OP work item can take it (a different work item id).
        db.update_delivery_operation(op["id"], status="PLANNED",
                                     payload=new_patch,
                                     expected_target_revision=new_rev,
                                     target_record_id=employee_id,
                                     employee_id=employee_id,
                                     before_snapshot=current,
                                     idempotency_key=new_idem,
                                     last_error=None,
                                     target_revision_after=None,
                                     target_request_id=None,
                                     work_item_id=None)
        return "replanned"

    if new_outcome == "READY_CREATE":
        new_payload = _build_create_payload(db, job_id, op["candidate_id"], schema)
        new_idem = f"deliver:{job_id}:{op['candidate_id']}:create:{new_rev}"
        db.update_delivery_operation(op["id"], status="PLANNED", payload=new_payload,
                                     expected_target_revision=None,
                                     idempotency_key=new_idem,
                                     op_type="CREATE",
                                     last_error=None,
                                     target_revision_after=None,
                                     target_request_id=None,
                                     work_item_id=None)
        return "replanned"

    return "review_required"


def _invalidate_stale_decisions(db: Database, job_id: str, candidate_id: str,
                                old_revision: int | None) -> int:
    """Supersede target-review decisions that were made against a stale revision."""
    issues = db.get_target_review_issues(job_id)
    count = 0
    for iss in issues:
        if iss["candidate_id"] != candidate_id:
            continue
        if iss["status"] != "resolved":
            continue
        # Check if the decision was bound to the old revision
        affected = json.loads(iss.get("affected") or "{}") if isinstance(iss.get("affected"), str) else (iss.get("affected") or {})
        target_rec = affected.get("target", {}) if isinstance(affected, dict) else {}
        bound_rev = target_rec.get("revision") if isinstance(target_rec, dict) else None
        if old_revision is not None and bound_rev is not None and bound_rev == old_revision:
            db.supersede_target_review_issue(iss["id"])
            db.add_audit(job_id, event_type="stale_decision_invalidated", actor="system",
                         issue_id=iss["id"], after={
                             "reason": "target revision changed",
                             "old_revision": old_revision})
            count += 1
    return count


# ===================== rollback / compensation ==========================================

def plan_rollback(db: Database, job_id: str) -> dict:
    """Mark all SUCCEEDED delivery operations for rollback. Returns counts."""
    ops = db.get_delivery_operations(job_id, status="SUCCEEDED")
    planned = 0
    for op in ops:
        ok = db.transition_delivery_op(op["id"], "SUCCEEDED", "ROLLBACK_PLANNED")
        if ok:
            planned += 1
    if planned:
        db.add_audit(job_id, event_type="rollback_planned", actor="system",
                     after={"operations": planned})
    return {"rollback_planned": planned, "total_succeeded": len(ops)}


async def execute_rollback_operation(db: Database, gateway: TargetEmployeeGateway,
                                     op: dict, settings, schema=None) -> str:
    """Execute a compensating write for one operation. ``schema`` (the job's effective target schema)
    is used to compute the field-level rollback diff for the version history / audit UI (order §3);
    it is optional so any pre-existing caller that omits it still runs (no field_changes recorded)."""
    # Organization isolation: rollback (compensating DELETE / PUT-replace) is scoped to the job's
    # organization, so it can never delete or overwrite another organization's record.
    gateway = gateway.for_organization(db.job_tenant(op["job_id"]))
    op_id = op["id"]
    op_type = op["op_type"]
    employee_id = op["employee_id"] or op["target_record_id"]
    rev_after = op.get("target_revision_after")
    attempt_no = db.delivery_attempt_count(op_id) + 1

    atm_id = db.add_delivery_attempt(operation_id=op_id, attempt_no=attempt_no, action="ROLLBACK")

    try:
        if op_type == "CREATE":
            # Compensating DELETE: remove the exact record using id + revision evidence
            result = await gateway.delete_employee(
                employee_id, expected_revision=rev_after,
                idempotency_key=f"rollback:{op['idempotency_key']}")
            db.complete_delivery_attempt(atm_id, http_status=result.status_code, retryable=False,
                                         error_category=None, retry_after=None,
                                         target_request_id=result.request_id,
                                         response_meta={"deleted": result.deleted}, result="success")

        elif op_type == "UPDATE":
            before = json.loads(op["before_snapshot"]) if isinstance(op["before_snapshot"], str) else (op["before_snapshot"] or {})
            if not before:
                db.complete_delivery_attempt(atm_id, http_status=None, retryable=False,
                                             error_category="no_before_snapshot", retry_after=None,
                                             target_request_id=None, response_meta=None,
                                             result="terminal")
                db.update_delivery_operation(op_id, status="ROLLBACK_FAILED",
                                             last_error="no before_snapshot to restore")
                return "ROLLBACK_FAILED"

            if rev_after is None:
                db.complete_delivery_attempt(atm_id, http_status=None, retryable=False,
                                             error_category="no_revision_after", retry_after=None,
                                             target_request_id=None, response_meta=None,
                                             result="terminal")
                db.update_delivery_operation(op_id, status="ROLLBACK_FAILED",
                                             last_error="no target_revision_after for reverse patch")
                return "ROLLBACK_FAILED"

            result = await gateway.replace_employee(
                employee_id, before, expected_revision=rev_after,
                idempotency_key=f"rollback:{op['idempotency_key']}")
            db.complete_delivery_attempt(atm_id, http_status=result.status_code, retryable=False,
                                         error_category=None, retry_after=None,
                                         target_request_id=result.request_id,
                                         response_meta={"revision": result.revision}, result="success")
        else:
            raise ValueError(f"unknown op_type for rollback: {op_type}")

        # Create a rollback version and enrich the audit event with the SAME from_version/to_version/
        # origin shape the normal "employee_version" audit event uses, so the UI's existing version
        # detection renders "Restored Version N" + the fields restored (order §3) instead of a raw
        # before/after dump.
        rv = _create_rollback_version(db, op, schema)

        db.update_delivery_operation(op_id, status="ROLLED_BACK")
        after: dict = {"op_type": op_type, "candidate_id": op["candidate_id"], "attempt": attempt_no}
        if rv is not None:
            after.update({
                "from_version": rv.get("undone_version_no"), "to_version": rv["version_no"], "origin": "rollback",
                "restores_version_no": rv.get("restores_version_no"),
                "deleted_from_target": rv.get("deleted_from_target", False),
                "changed_fields": [c["field"] for c in (
                    json.loads(rv["field_changes"]) if isinstance(rv.get("field_changes"), str)
                    else (rv.get("field_changes") or []))],
            })
        db.add_audit(op["job_id"], event_type="rollback_succeeded", actor="system",
                     source_ref=op_id, after=after)
        return "ROLLED_BACK"

    except TargetResponseError as e:
        retryable, category, retry_after = classify_target_error(e)
        result_tag = "retryable" if retryable else ("conflict" if "conflict" in category else "terminal")
        db.complete_delivery_attempt(atm_id, http_status=e.status_code, retryable=retryable,
                                     error_category=category, retry_after=retry_after,
                                     target_request_id=e.request_id,
                                     response_meta=e.body, result=result_tag)

        if category == "revision_conflict":
            db.update_delivery_operation(op_id, status="ROLLBACK_FAILED",
                                         last_error=f"target changed after our write: {e}")
            db.add_audit(op["job_id"], event_type="rollback_conflict", actor="system",
                         source_ref=op_id, after={"category": category})
            return "ROLLBACK_FAILED"

        if retryable:
            db.update_delivery_operation(op_id, status="ROLLBACK_PLANNED",
                                         last_error=f"retryable: {category}")
            return "ROLLBACK_PLANNED"

        db.update_delivery_operation(op_id, status="ROLLBACK_FAILED",
                                     last_error=f"{category}: {str(e)[:300]}")
        db.add_audit(op["job_id"], event_type="rollback_failed", actor="system",
                     source_ref=op_id, after={"category": category, "attempt": attempt_no})
        return "ROLLBACK_FAILED"

    except TargetUnavailable as e:
        db.complete_delivery_attempt(atm_id, http_status=None, retryable=True,
                                     error_category="transport_error", retry_after=None,
                                     target_request_id=None, response_meta=None,
                                     result="retryable")
        db.update_delivery_operation(op_id, status="ROLLBACK_PLANNED",
                                     last_error=f"transport: {str(e)[:300]}")
        return "ROLLBACK_PLANNED"


def _rollback_field_changes(before: dict, after: dict, schema) -> list[dict]:
    """Field-level 'what got restored' for the audit/UI (order §3: 'Restored Version N' + the fields
    restored) — a plain before->after diff, no candidate/provenance needed for a compensating write."""
    if schema is None:
        return []
    from .versions import compare_snapshots
    cmp = compare_snapshots(before or {}, after or {}, schema)
    out = [{"field": r["field"], "from": r["a"], "to": r["b"], "method": "rollback"}
           for r in cmp["fields"] if r["changed"]]
    out += [{"field": f"custom_attributes.{r['field']}", "from": r["a"], "to": r["b"], "method": "rollback"}
            for r in cmp["custom_attributes"] if r["changed"]]
    return out


def _snap(v: dict) -> dict:
    s = v["snapshot"]
    return json.loads(s) if isinstance(s, str) else (s or {})


def _create_rollback_version(db: Database, op: dict, schema) -> dict | None:
    """Create a new employee version with origin=rollback and restores_version_id, PERSISTING the
    field-level diff so it is visible both in the version history and in the rollback audit event
    ('Restored Version N' + the fields restored — order §3). Returns the created version row (with
    restores_version_no/deleted_from_target added) or None if none was created."""
    job_id = op["job_id"]
    cid = op["candidate_id"]
    versions = db.get_employee_versions(job_id, cid)
    if not versions:
        return None

    desired_vid = op.get("desired_version_id")
    undone = next((v for v in versions if v["id"] == desired_vid), versions[-1])
    undone_snapshot = _snap(undone)

    if op["op_type"] == "CREATE":
        # Compensating DELETE: the employee is fully removed from the target, undoing its creation —
        # there is no "restored version" to point at, only "what existed is now gone".
        snapshot = {"_deleted": True, "employee_id": op.get("employee_id")}
        restore_target_id = versions[0]["id"]
        v = db.add_employee_version(
            job_id, cid, snapshot=snapshot, origin="rollback",
            change_reason=f"rollback of create (op {op['id'][:12]}): compensating delete",
            created_by="system", restores_version_id=restore_target_id, dedup=False,
            field_changes=_rollback_field_changes(undone_snapshot, snapshot, schema))
        if v is None:
            return None
        return {**v, "undone_version_no": undone["version_no"],
                "restores_version_no": None, "deleted_from_target": True}

    restore_target = None
    for v in versions:
        if v["id"] == desired_vid:
            if v.get("parent_version_id"):
                restore_target = next((vv for vv in versions if vv["id"] == v["parent_version_id"]), None)
            break
    if restore_target is None and len(versions) >= 1:
        restore_target = versions[0]
    if restore_target is None:
        return None

    before = json.loads(op["before_snapshot"]) if isinstance(op.get("before_snapshot"), str) \
        else (op.get("before_snapshot") or {})
    snapshot = before if before else _snap(restore_target)
    v = db.add_employee_version(
        job_id, cid, snapshot=snapshot, origin="rollback",
        change_reason=f"rollback of {op['op_type'].lower()} (op {op['id'][:12]})",
        created_by="system", restores_version_id=restore_target["id"],
        field_changes=_rollback_field_changes(undone_snapshot, snapshot, schema))
    if v is None:
        return None
    return {**v, "undone_version_no": undone["version_no"],
            "restores_version_no": restore_target["version_no"], "deleted_from_target": False}


# ===================== retry backoff ====================================================

def compute_backoff(attempt: int, settings, *, retry_after: float | None = None) -> float:
    """Bounded exponential backoff with jitter, honouring Retry-After from 429."""
    base = getattr(settings, "target_retry_base_seconds", 0.5)
    cap = getattr(settings, "target_retry_max_seconds", 30.0)
    # Exponential
    delay = min(base * (2 ** (attempt - 1)), cap)
    # Jitter: [0.5, 1.5] * delay
    delay *= 0.5 + random.random()
    # Honour Retry-After (capped)
    if retry_after is not None and retry_after > 0:
        delay = max(delay, min(retry_after, cap))
    return round(delay, 2)


# ===================== final job state ==================================================

def compute_final_job_status(db: Database, job_id: str) -> str:
    """Determine the job-level status from delivery operation states."""
    ops = db.get_delivery_operations(job_id)
    if not ops:
        return "reconciliation_complete"

    statuses = {op["status"] for op in ops}
    # SKIPPED_NO_CHANGE / SKIPPED_EXCLUDED are no-write terminals: they count as done, but never as
    # SUCCEEDED for rollback purposes.
    no_write_terminal = {"SKIPPED_NO_CHANGE", "SKIPPED_EXCLUDED"}
    terminal_ok = {"SUCCEEDED"} | no_write_terminal

    if statuses <= terminal_ok:
        return "migration_complete"
    if "STALE_TARGET" in statuses:
        return "stale_target_review_required"
    if "ROLLBACK_PLANNED" in statuses or "ROLLED_BACK" in statuses or "ROLLBACK_FAILED" in statuses:
        if "ROLLBACK_FAILED" in statuses:
            return "rollback_partial_failure"
        rollback_states = {"ROLLED_BACK", "ROLLBACK_PLANNED"} | no_write_terminal
        if statuses <= rollback_states:
            return "rollback_in_progress" if "ROLLBACK_PLANNED" in statuses else "rollback_complete"
        return "rollback_in_progress"
    if "RETRYABLE" in statuses or "PROCESSING" in statuses or "PLANNED" in statuses:
        return "delivering"
    if "FAILED" in statuses:
        non_fail = statuses - ({"FAILED"} | no_write_terminal)
        if "SUCCEEDED" in non_fail:
            return "delivery_partial_failure"
        return "error"
    return "delivering"


def finalize_delivery_status(db: Database, job_id: str) -> str:
    """Job status when there is no claimable delivery work left.

    No operations at all means every candidate reconciled to NO_CHANGE / EXCLUDED, so the migration
    is trivially complete. Otherwise derive the status from the operation states (which correctly
    yields migration_complete when all ops are SUCCEEDED / SKIPPED_NO_CHANGE / SKIPPED_EXCLUDED, and
    delivery_partial_failure / error when some FAILED)."""
    if not db.get_delivery_operations(job_id):
        return "migration_complete"
    return compute_final_job_status(db, job_id)


def build_delivery_summary(db: Database, job_id: str) -> dict:
    """Persisted summary built from live delivery + recon state.

    Keys match the operation status enum values so the frontend can read them
    directly (SUCCEEDED, FAILED, RETRYABLE, …).
    """
    ops = db.get_delivery_operations(job_id)
    recon = db.get_target_reconciliation(job_id)
    counts: dict[str, int] = {
        "PLANNED": 0, "SUCCEEDED": 0, "FAILED": 0, "RETRYABLE": 0,
        "STALE_TARGET": 0, "ROLLED_BACK": 0, "ROLLBACK_FAILED": 0, "ROLLBACK_PLANNED": 0,
        "SKIPPED_NO_CHANGE": 0, "SKIPPED_EXCLUDED": 0, "NO_CHANGE": 0, "EXCLUDED": 0,
        "PROCESSING": 0, "IN_PROGRESS": 0, "total_attempts": 0,
    }
    for op in ops:
        st = op["status"]
        if st in counts:
            counts[st] += 1
        counts["total_attempts"] += op.get("attempt_count") or 0

    for r in recon:
        if r["outcome"] == "NO_CHANGE":
            counts["NO_CHANGE"] += 1
        elif r["outcome"] == "EXCLUDED":
            counts["EXCLUDED"] += 1

    return counts
