"""Bounded local worker pool (M3A → M3B, single-node).

A fixed number (MAX_FILE_WORKERS) of asyncio worker tasks pull durable work items from the
SQLite queue and execute stages OUTSIDE the claim transaction. Correctness comes from the
persisted work status + atomic claim + idempotent stage execution — not from in-memory task
ownership. This is deliberately a single-node bounded pool, NOT a distributed queue.

Stages: INGEST_FILE (per file) -> MAP -> PREPARE -> TARGET_RECONCILE -> DELIVER_OP/ROLLBACK_OP,
with RESUME_* items enqueued by human-decision endpoints. Stage transitions are enqueued
idempotently. When ``settings.auto_continue`` is True (the default), safe stages chain
automatically: preparation → reconciliation → delivery plan → target writes → final status.
"""
from __future__ import annotations

import asyncio
import json
import logging
from time import perf_counter

from langgraph.types import Command

from .blobstore import sha256_hex
from .delivery import (
    plan_delivery, execute_delivery_operation, execute_rollback_operation,
    handle_stale_target, compute_backoff, compute_final_job_status, build_delivery_summary,
    classify_target_error, classify_transport_error, deliver_work_key, finalize_delivery_status,
    StaleRefreshTransient,
)
from .ingest import parse_file
from .reconcile_target import reconcile, reconciliation_invariant_error
from .versions import build_and_store_versions

log = logging.getLogger("darwinbox.worker")


class TerminalStageError(Exception):
    """A failure that must NOT be retried (e.g. checksum mismatch, corrupt input)."""


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


class WorkerPool:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.db = ctx.db
        self.settings = ctx.settings
        self._tasks: list[asyncio.Task] = []
        self._running = False

    async def start(self) -> None:
        # Recover work left 'processing' by a previous (crashed) run. At startup NO other process can
        # hold a live lease (single-node invariant), so every processing item is reclaimed at once —
        # a crashed delivery is retried with the SAME idempotency key instead of waiting a full lease.
        reclaimed = self.db.reclaim_stale_work(force_all=True)
        if reclaimed:
            log.info("reclaimed %d stale work item(s) at startup", reclaimed)
        self._running = True
        n = max(1, self.settings.max_file_workers)
        for i in range(n):
            self._tasks.append(asyncio.create_task(self._loop(f"{self.settings.resolved_worker_id}#{i}")))
        self._tasks.append(asyncio.create_task(self._reaper()))

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()

    async def _reaper(self) -> None:
        # Periodically return crashed-but-not-restarted items to the queue.
        while self._running:
            try:
                await asyncio.sleep(max(1.0, self.settings.work_lease_seconds / 2))
                self.db.reclaim_stale_work()
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                log.exception("reaper error")

    async def _loop(self, worker_id: str) -> None:
        poll = self.settings.work_poll_interval_seconds
        while self._running:
            try:
                item = self.db.claim_next_work(worker_id, lease_seconds=self.settings.work_lease_seconds)
            except Exception:  # noqa: BLE001
                log.exception("claim error"); await asyncio.sleep(poll); continue
            if item is None:
                await asyncio.sleep(poll); continue
            await self._process(item, worker_id)

    async def _process(self, item: dict, worker_id: str) -> None:
        # M3I delete guard: a deleted migration must never be resurrected. If the job was deleted after
        # this item was enqueued (or between claim and dispatch) — its jobs row is gone, or a legacy
        # tombstone is set — drop the item as cancelled instead of executing it: no stage runs, no
        # target side effect.
        if self.db.job_is_deleted(item["job_id"]):
            self.db.cancel_work(item["id"], worker_id, reason="job_deleted")
            return
        hb = asyncio.create_task(self._heartbeat(item["id"], worker_id))
        try:
            await self._dispatch(item, worker_id)
            if item["kind"] == "INGEST_FILE":
                # Complete this file AND enqueue MAP (if it was the last one) in one atomic step,
                # so the just-finished item is counted as succeeded, not still-processing.
                if self.db.complete_ingest_and_maybe_enqueue_map(item["id"], item["job_id"], worker_id):
                    jid = item["job_id"]
                    self.db.set_job_stage(jid, status="mapping_queued", stage="ingested")
                    self.db.add_audit(jid, event_type="ingested", actor="system",
                                      after={"files": len(self.db.get_source_files(jid)),
                                             "tables": len(self.db.get_tables(jid))},
                                      schema_version=self.ctx.schema.version)
            else:
                if not self.db.complete_work(item["id"], worker_id):
                    log.warning("work=%s finished but the lease was no longer owned by %s; not completed",
                                item["id"], worker_id)
        except _SuppressedRetry:
            pass  # fail_work was already called with the right backoff; do not double-complete
        except TerminalStageError as e:
            self.db.set_work_failed(item["id"], worker_id, category="terminal", error=str(e))  # no retry
            log.warning("terminal failure work=%s job=%s: %s", item["id"], item["job_id"], e)
            if item["kind"] == "INGEST_FILE":
                # A rejected source (oversized/corrupt/unparseable) must NOT let the job proceed as
                # though complete, and must be clearly visible — never a silent hang. Fail the whole
                # job loudly: a migration does not run on a partial or unreadable employee population.
                try:
                    self._fail_job_on_ingest(item, str(e))
                except Exception:  # noqa: BLE001 - visibility backstop must never kill the worker loop
                    log.exception("failed to surface ingest failure for job=%s", item["job_id"])
        except Exception as e:  # noqa: BLE001
            status = self.db.fail_work(item["id"], worker_id, category=type(e).__name__, error=str(e))
            log.warning("stage failure work=%s job=%s kind=%s -> %s: %s",
                        item["id"], item["job_id"], item["kind"], status, e)
            # No-stuck invariant (M3B.3): an UNEXPECTED exception outside the classified
            # retryable/stale paths that EXHAUSTS the durable retry budget must not leave a delivery /
            # rollback operation non-terminal (PROCESSING / RETRYABLE / STALE_TARGET / ROLLBACK_PLANNED)
            # while its only work item is 'failed' — the job would then hang with no claimable work.
            if status == "failed" and item["kind"] in ("DELIVER_OP", "ROLLBACK_OP"):
                try:
                    self._terminalize_stuck_operation(item, category=type(e).__name__, error=str(e))
                except Exception:  # noqa: BLE001 - the no-stuck backstop must never kill the worker loop
                    log.exception("failed to terminalize stuck operation for work=%s", item["id"])
        finally:
            hb.cancel()
            try:
                await hb
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def _fail_job_on_ingest(self, item: dict, error: str) -> None:
        """Terminalize a job whose source file was rejected at ingest (e.g. row-limit exceeded, corrupt
        blob, unparseable). Sets a clear error status + stage and audits it so the operator/UI sees an
        actionable failure instead of a job that silently never advances past ingest."""
        db = self.db
        job_id = item["job_id"]
        file_id = item.get("source_file_id") or (
            json.loads(item["payload"]).get("source_file_id") if item.get("payload") else None)
        f = db.get_source_file(file_id) if file_id else None
        fname = f.get("original_filename") if f else file_id
        detail = (f.get("parse_error") if f else None) or error
        msg = (f"Ingestion failed for source file '{fname}': {detail} "
               f"Mapping/preparation/delivery will NOT run on a truncated or unreadable source.")
        db.set_job_stage(job_id, status="error", stage="ingest_failed", error=msg)
        db.add_audit(job_id, event_type="ingest_failed", actor="system",
                     source_ref={"source_file_id": file_id, "original_filename": fname},
                     reason=msg, work_item_id=item["id"])

    async def _heartbeat(self, work_id: str, worker_id: str) -> None:
        interval = max(1.0, self.settings.work_lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                self.db.heartbeat_work(work_id, worker_id, lease_seconds=self.settings.work_lease_seconds)
            except Exception:  # noqa: BLE001
                pass

    async def _dispatch(self, item: dict, worker_id: str) -> None:
        kind = item["kind"]
        if kind == "INGEST_FILE":
            await self._ingest_file(item)
        elif kind in ("MAP", "RESUME_MAPPING"):
            await self._run_mapping(item, resume=kind == "RESUME_MAPPING", worker_id=worker_id)
        elif kind in ("PREPARE", "RESUME_PREPARATION"):
            await self._run_prep(item, resume=kind == "RESUME_PREPARATION")
        elif kind in ("TARGET_RECONCILE", "RESUME_TARGET_REVIEW"):
            await self._run_reconcile(item, worker_id=worker_id)
        elif kind == "DELIVER_OP":
            await self._run_deliver_op(item, worker_id=worker_id)
        elif kind == "ROLLBACK_OP":
            await self._run_rollback_op(item, worker_id=worker_id)
        else:
            raise TerminalStageError(f"unknown work kind '{kind}'")

    # --- INGEST_FILE ------------------------------------------------------
    def _record_timing(self, job_id: str, stage: str, t0: float, detail: dict | None = None) -> None:
        """M3F §I: persist per-stage COMPUTE time (best-effort; timing must never break the pipeline).
        Measured around the compute call only, so human-review waits (which happen between graph
        invocations) are naturally excluded from the stage total."""
        try:
            self.db.add_stage_timing(job_id, stage=stage, duration_ms=(perf_counter() - t0) * 1000.0,
                                     detail=detail)
        except Exception:  # pragma: no cover - observability must not affect correctness
            logger.debug("stage timing record failed", exc_info=True)

    async def _ingest_file(self, item: dict) -> None:
        db = self.db
        job_id = item["job_id"]
        file_id = item["source_file_id"] or json.loads(item["payload"]).get("source_file_id")
        f = db.get_source_file(file_id)
        if not f:
            raise TerminalStageError(f"source_file {file_id} not found")
        if f["parse_status"] == "parsed":
            return  # already ingested (idempotent re-delivery); completion enqueues MAP if last
        db.set_source_file_status(file_id, parse_status="processing")
        data = self.ctx.blobstore.get(f["blob_key"])
        # Integrity: verify size + SHA-256 before parsing corrupt bytes.
        if len(data) != f["size_bytes"] or (f["sha256"] and sha256_hex(data) != f["sha256"]):
            db.set_source_file_status(file_id, parse_status="failed", parse_error="checksum/size mismatch")
            db.add_audit(job_id, event_type="ingest_checksum_mismatch", actor="system",
                         source_ref={"source_file_id": file_id}, work_item_id=item["id"],
                         reason="blob checksum/size verification failed")
            raise TerminalStageError("source blob failed checksum/size verification")
        _t0 = perf_counter()
        try:
            pf = await asyncio.to_thread(
                parse_file, filename=f["original_filename"], data=data, stored_name=f["blob_key"],
                max_bytes=self.settings.max_upload_bytes, max_rows=self.settings.max_rows_per_table,
                file_id=file_id)
        except Exception as e:  # parse errors are terminal for this file
            db.set_source_file_status(file_id, parse_status="failed", parse_error=str(e)[:500])
            raise TerminalStageError(f"parse failed: {e}") from e
        self._record_timing(job_id, "parse_stage", _t0, {"file_id": file_id})
        db.delete_source_data_for_file(job_id, file_id)  # idempotent re-ingest
        for table in pf.tables:
            db.add_source_table(job_id, table)
        db.add_source_rows(job_id, pf.records)
        db.add_parsing_issues(job_id, pf.issues)
        db.set_source_file_status(file_id, parse_status="parsed")
        db.add_audit(job_id, event_type="file_parsed", actor="system",
                     source_ref={"source_file_id": file_id},
                     after={"tables": len(pf.tables), "rows": len(pf.records)}, work_item_id=item["id"])
        # MAP is enqueued by complete_ingest_and_maybe_enqueue_map once this item is marked succeeded.

    # --- MAP / RESUME_MAPPING --------------------------------------------
    async def _run_mapping(self, item: dict, *, resume: bool, worker_id: str | None = None) -> None:
        job_id = item["job_id"]
        self.db.set_job_threads(job_id, map_thread=job_id)
        _t0 = perf_counter()
        if resume:
            await self.ctx.mapping_graph.ainvoke(Command(resume={"applied": True}), _cfg(job_id))
        else:
            await self.ctx.mapping_graph.ainvoke(
                {"job_id": job_id, "schema_version": self.ctx.schema.version}, _cfg(job_id))
        self._record_timing(job_id, "mapping", _t0, {"resume": resume})
        status = (self.db.get_job(job_id) or {}).get("status")
        if status == "mapping_complete":
            # First run uses the base key; a MAP re-run (e.g. after a custom-field approval) gets a
            # fresh rerun key so preparation actually recomputes with the new mapping.
            self.db.enqueue_stage_rerun(job_id, "PREPARE", f"{job_id}:prepare")
        elif status == "blocked_provider":
            # Race guard: a proposal decision that settled the LAST open proposal while this MAP item
            # was still 'processing' could not enqueue a re-run (one MAP item was active). If nothing
            # is open any more but decisions exist, chain the re-run here so the job never stalls.
            props = self.db.get_custom_field_proposals(job_id)
            if props and not any(p["status"] == "open" for p in props) \
                    and any(p["status"] in ("approved", "mapped_existing", "mapped_target", "ignored") for p in props):
                self.db.complete_work(item["id"], worker_id)   # release first so the rerun is not "already_active"
                self.db.enqueue_stage_rerun(job_id, "MAP", f"{job_id}:map")

    # --- PREPARE / RESUME_PREPARATION ------------------------------------
    async def _run_prep(self, item: dict, *, resume: bool) -> None:
        job_id = item["job_id"]
        prep_thread = f"{job_id}:prep"
        self.db.set_job_threads(job_id, prep_thread=prep_thread)
        _t0 = perf_counter()
        if resume:
            await self.ctx.preparation_graph.ainvoke(Command(resume={"applied": True}), _cfg(prep_thread))
        else:
            await self.ctx.preparation_graph.ainvoke({"job_id": job_id}, _cfg(prep_thread))
        self._record_timing(job_id, "preparation", _t0, {"resume": resume})
        # M3B: auto-chain preparation → reconciliation when autonomous (safe stages proceed
        # without manual "Run reconciliation" action). The operator-initiated path still works
        # via POST /api/jobs/{id}/reconcile for recovery/admin purposes.
        if self.settings.auto_continue:
            status = (self.db.get_job(job_id) or {}).get("status")
            if status == "preparation_complete":
                self.db.enqueue_stage_rerun(job_id, "TARGET_RECONCILE", f"{job_id}:reconcile")

    # --- TARGET_RECONCILE / RESUME_TARGET_REVIEW -------------------------
    async def _run_reconcile(self, item: dict, *, worker_id: str | None = None) -> None:
        db, job_id = self.db, item["job_id"]
        # Guard against a stale/racing reconcile: a MAP re-run (e.g. after a custom-field proposal
        # decision) can create NEW mapping/record reviews AFTER this reconcile was enqueued. Running it
        # now would trip the reconciliation invariant and error the whole job ("reconciliation not
        # complete: open_mapping=…"). Instead, defer to the review flow — resolving those reviews
        # re-drives mapping → preparation → reconcile cleanly. No error, no stuck job.
        open_map = len(db.get_issues(job_id, status="open"))
        open_rec = len(db.get_record_issues(job_id, status="open"))
        if open_map or open_rec:
            back = "awaiting_review" if open_map else "awaiting_record_review"
            db.set_job_stage(job_id, status=back, stage=back)
            db.add_audit(job_id, event_type="reconcile_deferred_open_reviews", actor="system",
                         after={"open_mapping": open_map, "open_record": open_rec},
                         work_item_id=item["id"])
            return
        db.set_job_stage(job_id, status="reconciling_target", stage="reconciling_target")
        eff = self.ctx.effective_schema_for_job(job_id)
        _t0 = perf_counter()
        res = await reconcile(db, self.ctx.gateway, job_id, eff)
        self._record_timing(job_id, "reconcile", _t0)
        db.replace_target_snapshots(job_id, res["snapshots"])
        db.replace_target_reconciliation(job_id, res["results"])
        # Append immutable employee versions for materially-changed effective records (idempotent).
        build_and_store_versions(db, job_id, eff, res["results"])
        keep = set()
        for iss in res["issues"]:
            keep.add(iss["id"])
            db.upsert_target_review_issue(
                job_id, issue_id=iss["id"], candidate_id=iss["candidate_id"],
                business_key=iss.get("business_key"), field=iss.get("field"), issue_type=iss["issue_type"],
                reason=iss["reason"], incoming_value=iss.get("incoming_value"),
                target_value=iss.get("target_value"), match_basis=iss["match_basis"],
                options=iss.get("options", []), affected=iss.get("affected", {}))
        db.supersede_target_issues_not_in(job_id, keep)
        open_target = db.get_target_review_issues(job_id, status="open")
        db.add_audit(job_id, event_type="target_reconciled", actor="system",
                     after={**res["counts"], "open_target_issues": len(open_target)},
                     work_item_id=item["id"])
        if open_target:
            db.set_job_stage(job_id, status="awaiting_target_review", stage="awaiting_target_review")
            return
        err = reconciliation_invariant_error(db, job_id, res["results"])
        if err:
            db.set_job_stage(job_id, status="error", stage="reconciliation_invariant_failed", error=err)
            db.add_audit(job_id, event_type="reconciliation_invariant_failed", actor="system", reason=err)
            return
        from .reconcile_target import RECON_VERSION
        summary = {"result": "reconciliation_complete", "recon_version": RECON_VERSION,
                   "counts": res["counts"]}
        db.set_recon_summary(job_id, summary)
        db.set_job_stage(job_id, status="reconciliation_complete", stage="reconciliation_complete")
        db.add_audit(job_id, event_type="reconciliation_complete", actor="system", after=res["counts"])
        # M3B: auto-chain reconciliation → delivery plan → delivery writes
        if self.settings.auto_continue:
            self._enqueue_delivery(job_id)

    # ===================== M3B: delivery ========================================

    def _enqueue_delivery(self, job_id: str) -> None:
        """Plan delivery operations, enqueue one durable DELIVER_OP work item per operation,
        and transition the job to 'delivering'."""
        eff = self.ctx.effective_schema_for_job(job_id)
        plan = plan_delivery(self.db, job_id, eff)
        if plan["blocked"]:
            # There are still unresolved reviews; the pipeline cannot proceed
            log.info("delivery blocked for job=%s: %d review-required", job_id, plan["skipped_review"])
            return
        # Enqueue one durable work item per PLANNED operation, keyed by attempt generation so a
        # re-planned op (post stale/review) always gets a fresh claimable item. When nothing is
        # claimable, finalize from the current operation states (handles all-no-write jobs AND a
        # re-plan that only terminalized stale ops to SKIPPED_*).
        ops = self.db.get_delivery_operations(job_id, status="PLANNED")
        if not ops:
            final = finalize_delivery_status(self.db, job_id)
            self.db.set_delivery_summary(job_id, build_delivery_summary(self.db, job_id))
            self.db.set_job_stage(job_id, status=final, stage=final)
            self.db.add_audit(job_id, event_type=f"delivery_{final}", actor="system",
                              after=build_delivery_summary(self.db, job_id))
            return
        for op in ops:
            self.db.enqueue_work(job_id=job_id, kind="DELIVER_OP",
                                 idempotency_key=deliver_work_key(self.db, op["id"]),
                                 max_attempts=self.settings.target_max_attempts,
                                 payload={"operation_id": op["id"]})
        self.db.set_job_stage(job_id, status="delivering", stage="delivering")

    async def _run_deliver_op(self, item: dict, *, worker_id: str | None = None) -> None:
        """Execute one delivery operation via the gateway."""
        db = self.db
        payload = json.loads(item["payload"]) if isinstance(item["payload"], str) else (item["payload"] or {})
        op_id = payload.get("operation_id")
        if not op_id:
            raise TerminalStageError("DELIVER_OP work item missing operation_id")
        op = db.get_delivery_operation(op_id)
        if not op:
            raise TerminalStageError(f"delivery operation {op_id} not found")
        # PLANNED / RETRYABLE / PROCESSING -> normal execution. STALE_TARGET -> RESUME stale recovery
        # on this same durable work item (a prior 409 whose refetch has not yet reached a safe next
        # state, e.g. because a transient GET failure re-queued the item). Any other status is terminal
        # or handled elsewhere -> skip. A STALE_TARGET op is NEVER re-executed with the old update.
        if op["status"] not in ("PLANNED", "RETRYABLE", "PROCESSING", "STALE_TARGET"):
            log.info("deliver_op %s already in status %s; skipping", op_id, op["status"])
            return
        if worker_id and not db.work_owned_by(item["id"], worker_id):
            log.warning("deliver_op %s: lost ownership of work item %s; aborting", op_id, item["id"])
            return
        # M3B.2 operation-level execution fence: only ONE work item may drive this operation's target
        # side effect, even when several due work items exist for it (auto-retry + manual-retry race).
        # Work-item lease ownership alone is insufficient — two distinct items can each own a lease.
        if not db.claim_operation_work(op_id, item["id"]):
            log.warning("deliver_op %s: operation held by another live work item; skipping %s",
                        op_id, item["id"])
            return

        # RESUME path: the op already hit a 409 and is mid stale-recovery. NEVER call
        # execute_delivery_operation (that would re-send the old revision-bound UPDATE); drive the
        # refetch -> reconcile -> next-state pipeline instead.
        if op["status"] == "STALE_TARGET":
            await self._drive_stale_target(item, op, worker_id)
            return

        result_status = await execute_delivery_operation(db, self.ctx.gateway, op, self.settings)

        if result_status == "RETRYABLE":
            # Backoff must use the ACTUAL latest attempt number: the local `op` dict was read before
            # execute_delivery_operation logged this attempt, so op["attempt_count"] is off-by-one.
            attempts = db.get_delivery_attempts(op_id)
            last = attempts[-1] if attempts else {}
            ra = last.get("retry_after")
            attempt_no = db.delivery_attempt_count(op_id)
            backoff = compute_backoff(attempt_no, self.settings, retry_after=ra)
            work_status = db.fail_work(item["id"], worker_id, category="retryable_delivery",
                                       error=f"attempt {attempt_no}", backoff_seconds=backoff)
            if work_status == "failed":
                # Retry budget exhausted: the work item can never be claimed again, so the operation
                # must be terminalized instead of being left RETRYABLE forever (job would hang in
                # 'delivering'). The last attempt was retryable, so a human MAY manually retry later.
                last_cat = last.get("error_category") or "unknown"
                db.update_delivery_operation(op_id, status="FAILED",
                                             last_error=f"retry_exhausted:{last_cat}", work_item_id=None)
                db.add_audit(item["job_id"], event_type="delivery_retry_exhausted", actor="system",
                             source_ref=op_id, after={
                                 "candidate_id": op["candidate_id"], "last_category": last_cat,
                                 "attempts": attempt_no})
                self._check_delivery_complete(item["job_id"])
            raise _SuppressedRetry()  # fail_work already set the work status; don't double-complete

        if result_status == "STALE_TARGET":
            # First detection: re-read the freshly-STALE_TARGET op, then drive stale recovery through
            # the SAME single path used by the resume case above.
            op = db.get_delivery_operation(op_id) or op
            await self._drive_stale_target(item, op, worker_id)
            return

        # SUCCEEDED or FAILED — check if ALL ops are done and compute final status
        self._check_delivery_complete(item["job_id"])

    async def _drive_stale_target(self, item: dict, op: dict, worker_id: str | None) -> None:
        """Drive/resume stale-target recovery for ONE operation on its owning work item.

        - transient refetch failure -> retry the work item so stale handling RESUMES (the old
          revision-bound UPDATE is NEVER re-sent); exhausting the budget terminalizes the op (FAILED);
        - ``replanned`` -> enqueue a fresh generation-keyed work item; keep the job delivering;
        - ``review_required`` -> park the job in ``stale_target_review_required`` (a legitimate human
          gate);
        - ``no_change`` / ``excluded`` / ``failed`` -> the op is already terminalized; finalize the job
          so it can NEVER hang in 'delivering' when this was the last/only operation (M3B.3 fix 1).
        """
        db = self.db
        job_id = item["job_id"]
        op_id = op["id"]
        eff = self.ctx.effective_schema_for_job(job_id)
        try:
            stale_result = await handle_stale_target(db, self.ctx.gateway, job_id, op, eff)
        except StaleRefreshTransient as exc:
            self._schedule_stale_refresh_retry(item, op, worker_id, exc)
            raise _SuppressedRetry()  # fail_work already scheduled the retry / terminalized the op

        if stale_result == "replanned":
            # handle_stale_target released the op's execution claim; enqueue a fresh, generation-keyed
            # work item for the new revision so it is guaranteed to be claimable.
            db.enqueue_work(job_id=job_id, kind="DELIVER_OP",
                            idempotency_key=deliver_work_key(db, op_id),
                            max_attempts=self.settings.target_max_attempts,
                            payload={"operation_id": op_id})
        elif stale_result == "review_required":
            db.set_job_stage(job_id, status="stale_target_review_required",
                             stage="stale_target_review_required")
        else:
            # no_change / excluded / failed: the operation reached a terminal state inside
            # handle_stale_target. Finalize the job now (fix 1) — waiting for another /deliver would
            # otherwise strand the job in 'delivering' with no runnable work.
            self._check_delivery_complete(job_id)

    def _schedule_stale_refresh_retry(self, item: dict, op: dict, worker_id: str | None,
                                      exc: "StaleRefreshTransient") -> None:
        """Durably retry the stale REFETCH (a READ — never a target write attempt) via the work queue,
        honouring a 429 ``Retry-After``. Exhausting the work budget terminalizes the operation as
        FAILED and finalizes the job, so stale recovery can never hang with no runnable work."""
        db = self.db
        job_id = item["job_id"]
        op_id = op["id"]
        attempt = item.get("attempt") or 1
        backoff = compute_backoff(max(1, attempt), self.settings, retry_after=exc.retry_after)
        work_status = db.fail_work(item["id"], worker_id, category=f"stale_refresh_{exc.category}",
                                   error=f"stale refresh {exc.category} (attempt {attempt})",
                                   backoff_seconds=backoff)
        if work_status == "failed":
            db.update_delivery_operation(op_id, status="FAILED",
                                         last_error=f"stale_refresh_retry_exhausted:{exc.category}",
                                         work_item_id=None)
            db.add_audit(job_id, event_type="stale_refresh_retry_exhausted", actor="system",
                         source_ref=op_id, after={
                             "candidate_id": op["candidate_id"], "category": exc.category,
                             "attempts": attempt})
            self._check_delivery_complete(job_id)
        elif work_status == "retryable":
            db.add_audit(job_id, event_type="stale_refresh_retry_scheduled", actor="system",
                         source_ref=op_id, after={
                             "candidate_id": op["candidate_id"], "category": exc.category,
                             "retry_after": exc.retry_after, "backoff": backoff, "attempt": attempt})
        # 'not_owned' / 'not_found': the reaper will re-surface the item; nothing to schedule here.

    async def _run_rollback_op(self, item: dict, *, worker_id: str | None = None) -> None:
        """Execute one rollback/compensation operation via the gateway."""
        db = self.db
        payload = json.loads(item["payload"]) if isinstance(item["payload"], str) else (item["payload"] or {})
        op_id = payload.get("operation_id")
        if not op_id:
            raise TerminalStageError("ROLLBACK_OP work item missing operation_id")
        op = db.get_delivery_operation(op_id)
        if not op:
            raise TerminalStageError(f"delivery operation {op_id} not found for rollback")
        if op["status"] not in ("ROLLBACK_PLANNED",):
            log.info("rollback_op %s already in status %s; skipping", op_id, op["status"])
            return
        if worker_id and not db.work_owned_by(item["id"], worker_id):
            log.warning("rollback_op %s: lost ownership of work item %s; aborting", op_id, item["id"])
            return
        # M3B.2 operation-level execution fence (rollback lane).
        if not db.claim_operation_work(op_id, item["id"], rollback=True):
            log.warning("rollback_op %s: operation held by another live rollback work item; skipping %s",
                        op_id, item["id"])
            return

        eff = self.ctx.effective_schema_for_job(item["job_id"])
        result_status = await execute_rollback_operation(db, self.ctx.gateway, op, self.settings, eff)

        if result_status == "ROLLBACK_PLANNED":
            # Retryable — backoff from the ACTUAL number of rollback attempts (was a constant 1).
            rb_attempts = [a for a in db.get_delivery_attempts(op_id) if a["action"] == "ROLLBACK"]
            attempt_no = len(rb_attempts)
            backoff = compute_backoff(max(1, attempt_no), self.settings)
            work_status = db.fail_work(item["id"], worker_id, category="retryable_rollback",
                                       error=f"rollback attempt {attempt_no}", backoff_seconds=backoff)
            if work_status == "failed":
                # Exhausted retry budget for the compensating write → terminal ROLLBACK_FAILED so the
                # job does not hang in rollback_in_progress forever.
                last_cat = (rb_attempts[-1].get("error_category") if rb_attempts else None) or "unknown"
                db.update_delivery_operation(op_id, status="ROLLBACK_FAILED",
                                             last_error=f"retry_exhausted:{last_cat}",
                                             rollback_work_item_id=None)
                db.add_audit(item["job_id"], event_type="rollback_retry_exhausted", actor="system",
                             source_ref=op_id, after={
                                 "candidate_id": op["candidate_id"], "last_category": last_cat,
                                 "attempts": attempt_no})
                self._check_rollback_complete(item["job_id"])
            raise _SuppressedRetry()

        self._check_rollback_complete(item["job_id"])

    def _check_delivery_complete(self, job_id: str) -> None:
        """After a delivery op finishes, check if all ops are done and update job status."""
        counts = self.db.delivery_counts(job_id)
        in_progress = counts.get("PLANNED", 0) + counts.get("PROCESSING", 0) + counts.get("RETRYABLE", 0)
        if in_progress > 0:
            return  # still in flight
        final_status = compute_final_job_status(self.db, job_id)
        summary = build_delivery_summary(self.db, job_id)
        self.db.set_delivery_summary(job_id, summary)
        self.db.set_job_stage(job_id, status=final_status, stage=final_status)
        self.db.add_audit(job_id, event_type=f"delivery_{final_status}", actor="system", after=summary)

    def _check_rollback_complete(self, job_id: str) -> None:
        """After a rollback op finishes, check if all rollback ops are done."""
        counts = self.db.delivery_counts(job_id)
        if counts.get("ROLLBACK_PLANNED", 0) > 0:
            return  # still in flight
        has_failed = counts.get("ROLLBACK_FAILED", 0) > 0
        status = "rollback_partial_failure" if has_failed else "rollback_complete"
        summary = build_delivery_summary(self.db, job_id)
        self.db.set_delivery_summary(job_id, summary)
        self.db.set_job_stage(job_id, status=status, stage=status)
        self.db.add_audit(job_id, event_type=f"rollback_{status}", actor="system", after=summary)

    # ===================== M3B.3: no-stuck backstop =============================
    _DELIVER_TERMINAL = {"SUCCEEDED", "FAILED", "ROLLED_BACK", "ROLLBACK_FAILED",
                         "SKIPPED_NO_CHANGE", "SKIPPED_EXCLUDED"}
    _ROLLBACK_TERMINAL = {"ROLLED_BACK", "ROLLBACK_FAILED"}

    def _terminalize_stuck_operation(self, item: dict, *, category: str, error: str) -> None:
        """Backstop for the no-stuck invariant. When a DELIVER_OP / ROLLBACK_OP work item exhausts its
        durable retry budget through the GENERIC failure path (an unexpected, unclassified exception),
        the associated operation could otherwise be left non-terminal (PROCESSING / RETRYABLE /
        STALE_TARGET / ROLLBACK_PLANNED) with no claimable work — a stuck job. Terminalize the operation
        (FAILED / ROLLBACK_FAILED), NEVER overwriting an already-valid terminal state, audit it, and
        finalize / check the job."""
        db = self.db
        job_id = item["job_id"]
        payload = json.loads(item["payload"]) if isinstance(item["payload"], str) else (item["payload"] or {})
        op_id = payload.get("operation_id")
        if not op_id:
            return
        op = db.get_delivery_operation(op_id)
        if not op:
            return
        sanitized = f"work_exhausted:{category}: {(error or '')[:200]}"
        if item["kind"] == "DELIVER_OP":
            if op["status"] in self._DELIVER_TERMINAL:
                return  # already terminal — never overwrite a valid terminal state
            db.update_delivery_operation(op_id, status="FAILED", last_error=sanitized, work_item_id=None)
            db.add_audit(job_id, event_type="delivery_work_exhausted", actor="system",
                         source_ref=op_id, after={
                             "candidate_id": op["candidate_id"], "category": category,
                             "prior_status": op["status"]})
            self._check_delivery_complete(job_id)
        else:  # ROLLBACK_OP
            if op["status"] in self._ROLLBACK_TERMINAL:
                return
            db.update_delivery_operation(op_id, status="ROLLBACK_FAILED", last_error=sanitized,
                                         rollback_work_item_id=None)
            db.add_audit(job_id, event_type="rollback_work_exhausted", actor="system",
                         source_ref=op_id, after={
                             "candidate_id": op["candidate_id"], "category": category,
                             "prior_status": op["status"]})
            self._check_rollback_complete(job_id)


class _SuppressedRetry(Exception):
    """Raised by DELIVER_OP / ROLLBACK_OP handlers when fail_work was already called to schedule
    a retry via the queue's backoff. This prevents _process from double-completing the item."""
