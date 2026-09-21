"""HTTP API (M3A single-node, + M3A.2 tenants / custom fields / collections).

The execution boundary is strict: POST /api/jobs validates, stores raw blobs, persists file
metadata, and enqueues durable per-file work items, then returns a *queued* job. It never
parses/maps/prepares/reconciles in the request. Human-review endpoints persist a decision and
enqueue the next durable work item in one atomic step — they do not run graphs synchronously.
The bounded local worker pool (see worker.py) does all stage execution.

Everything is job-scoped; there is no arbitrary file access and no generic execute endpoint.
Tenant configuration (custom-field definitions) is the only non-job resource, scoped by tenant.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from fastapi import File as FileParam

from ..blobstore import sha256_hex
from ..custom_fields import validate_definition_input
from ..ingest import validate_upload
from ..mapping_rules import RULE_VERSION, table_context_name
from ..models import (
    AuditOut,
    CandidateOut,
    ColumnProfileOut,
    CustomFieldDefinitionIn,
    CustomFieldDefinitionOut,
    CustomFieldProposalOut,
    DecisionIn,
    DecisionOut,
    EmployeeVersionOut,
    JobOut,
    MappingRowOut,
    MappingsOut,
    ParsingIssueOut,
    PreparedDatasetOut,
    ProfilesOut,
    ProposalDecisionIn,
    ProposalDecisionOut,
    ReconciliationOut,
    ReconciliationRowOut,
    RecordDecisionIn,
    RecordDecisionOut,
    RecordIssueOut,
    ReviewIssueOut,
    SchemaOut,
    SourceFileOut,
    SourceRowContextOut,
    StagedCellOut,
    StagedRowOut,
    StagedRowsOut,
    TableOut,
    TargetDecisionIn,
    TargetDecisionOut,
    TargetReviewIssueOut,
    TargetSnapshotOut,
    TenantOut,
    DeleteMigrationOut,
    EditEmployeeIn,
    EditEmployeeOut,
    DeleteEmployeeOut,
    DeliveryAttemptOut,
    DeliveryOperationOut,
    DeliverySummaryOut,
    RemoveFilesIn,
    RemoveFilesOut,
    RetryIn,
    RetryOut,
    RollbackOut,
    VersionCollectionItemDiffOut,
    VersionCompareOut,
    VersionFieldDiffOut,
    WorkItemOut,
)
from ..delivery import (plan_delivery, plan_rollback, build_delivery_summary,
                        compute_final_job_status, deliver_work_key, finalize_delivery_status)
from ..policy import POLICY_VERSION
from ..prepare import NORMALIZATION_VERSION, effective_snapshot_from_candidate
from ..schema_loader import CUSTOM_PATH_PREFIX
from ..source_records import UnsupportedFormatError, UploadTooLargeError
from ..versions import compare_snapshots

router = APIRouter(prefix="/api")

_ACCEPTED = ("auto_accepted", "approved", "corrected")


def _ctx(request: Request):
    return request.app.state.ctx


def _job_or_404(ctx, job_id: str, *, allow_deleted: bool = False) -> dict:
    job = ctx.db.get_job(job_id)
    if not job or (job.get("deleted_at") and not allow_deleted):
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return job


def _job_counts(ctx, job_id: str) -> dict[str, int]:
    db = ctx.db
    decisions = db.get_decisions(job_id)
    files = db.get_source_files(job_id)
    work = db.get_work_items(job_id)
    recon = db.get_target_reconciliation(job_id)
    tables = db.get_tables(job_id)
    accepted = [d for d in decisions if d["status"] in _ACCEPTED and d["target_field"]]

    def _wc(status: str) -> int:
        return len([w for w in work if w["status"] == status])

    def _rc(outcome: str) -> int:
        return len([r for r in recon if r["outcome"] == outcome])

    return {
        "tables": len(tables),
        "source_rows": sum(int(t.get("n_rows") or 0) for t in tables),
        "profiles": len(db.get_profiles(job_id)),
        "proposals": len(db.get_proposals(job_id)),
        "auto_accepted": len([d for d in decisions if d["status"] == "auto_accepted"]),
        "rule_accepted": len([d for d in decisions if d.get("method") == "rule"]),
        "model_accepted": len([d for d in decisions if d.get("method") == "model"]),
        "open_issues": len(db.get_issues(job_id, status="open")),
        "resolved_issues": len(db.get_issues(job_id, status="resolved")),
        "candidates": len(db.get_candidates(job_id)),
        "open_record_issues": len(db.get_record_issues(job_id, status="open")),
        # M3A: files
        "files": len(files),
        "files_parsed": len([f for f in files if f["parse_status"] == "parsed"]),
        "files_failed": len([f for f in files if f["parse_status"] == "failed"]),
        # M3A: durable work items (this job)
        "work_pending": _wc("pending"),
        "work_processing": _wc("processing"),
        "work_retryable": _wc("retryable"),
        "work_succeeded": _wc("succeeded"),
        "work_failed": _wc("failed"),
        # M3A: target reconciliation outcomes (this job)
        "ready_create": _rc("READY_CREATE"),
        "ready_update": _rc("READY_UPDATE"),
        "no_change": _rc("NO_CHANGE"),
        "review_required": _rc("REVIEW_REQUIRED"),
        "excluded_target": _rc("EXCLUDED"),
        "open_target_issues": len(db.get_target_review_issues(job_id, status="open")),
        # M3A.2: destination kinds + custom-field proposals
        "mapped_core": len([d for d in accepted if (d.get("destination_kind") or "CORE_FIELD") == "CORE_FIELD"]),
        "mapped_collection": len([d for d in accepted if d.get("destination_kind") == "COLLECTION_FIELD"]),
        "mapped_custom": len([d for d in accepted if d.get("destination_kind") == "CUSTOM_FIELD"]),
        "ignored_columns": len([d for d in decisions if d["status"] == "ignored"]),
        "unmapped_columns": len([d for d in decisions if d["status"] == "unmapped"]),
        "open_proposals": len(db.get_custom_field_proposals(job_id, status="open")),
        # M3B: delivery operation counts
        **{f"delivery_{k.lower()}": v for k, v in db.delivery_counts(job_id).items()},
    }


def _derive_row_count(summary: dict | None, prep_summary: dict | None) -> int | None:
    """Employee-record / source-row count for the migration-list card, read from the stage summary
    blobs that already ride on the jobs row (no extra query). Preparation's processed-row count is the
    most meaningful once available; the mapping stage's rows_examined is the earlier fallback."""
    if prep_summary:
        n = (prep_summary.get("counts") or {}).get("source_rows_processed")
        if isinstance(n, int):
            return n
    if summary:
        n = (summary.get("routing_metrics") or {}).get("rows_examined")
        if isinstance(n, int):
            return n
    return None


def _job_out(ctx, job: dict | str) -> JobOut:
    if isinstance(job, str):
        job = ctx.db.get_job(job) or {}
    summary = json.loads(job["summary"]) if job["summary"] else None
    prep_summary = json.loads(job["prep_summary"]) if job.get("prep_summary") else None
    counts = _job_counts(ctx, job["id"])
    filenames = [f.get("original_filename") or f.get("id") for f in ctx.db.get_source_files(job["id"])]
    open_reviews = (counts.get("open_issues", 0) + counts.get("open_record_issues", 0)
                    + counts.get("open_target_issues", 0) + counts.get("open_proposals", 0))
    row_count = _derive_row_count(summary, prep_summary)
    if row_count is None and counts.get("source_rows"):
        row_count = counts["source_rows"]
    return JobOut(
        id=job["id"], thread_id=job["thread_id"], schema_version=job["schema_version"],
        tenant_id=job.get("tenant_id") or ctx.settings.default_tenant_id,
        provider=job["provider"], model_id=job["model_id"], adapter_kind=job["adapter_kind"],
        status=job["status"], stage=job["stage"], error=job["error"],
        summary=summary,
        prep_summary=prep_summary,
        recon_summary=json.loads(job["recon_summary"]) if job.get("recon_summary") else None,
        delivery_summary=json.loads(job["delivery_summary"]) if job.get("delivery_summary") else None,
        created_at=job["created_at"], updated_at=job["updated_at"],
        counts=counts,
        source_filenames=[f for f in filenames if f],
        row_count=row_count,
        open_reviews=open_reviews,
    )


def _enqueue_stage(ctx, job_id: str, kind: str, base_key: str) -> str:
    """Enqueue a job-level stage work item (no stacking while one is active; a re-trigger gets a
    fresh rerun key). Returns 'queued' | 'already_active' | 'noop'."""
    return ctx.db.enqueue_stage_rerun(job_id, kind, base_key)


@router.get("/health")
def health(request: Request) -> dict:
    """Readiness + non-secret counters. Reports whether a credential is CONFIGURED
    (present+nonempty), not whether it authenticates — no network probe is made here."""
    ctx = _ctx(request)
    st = ctx.provider_status
    configured = st["configured"]
    if configured:
        message = f"Provider '{st['provider']}' is configured (model {st['model']}). Not yet verified by a live call."
    else:
        message = ("Groq is not configured. Deterministic processing is available; columns that require "
                   "model interpretation cannot proceed until GROQ_API_KEY is set in backend/.env.")
    return {
        "status": "ok",
        "provider": st["provider"], "model_id": st["model"], "adapter_kind": ctx.adapter_kind,
        "configured": configured, "category": st["category"],
        "deterministic_available": True, "requires_key": st["requires_key"],
        "message": message, "env_file": st["env_file"], "env_file_exists": st["env_file_exists"],
        # backward-compatible aliases
        "adapter_available": ctx.adapter is not None, "adapter_error": ctx.adapter_error,
        # M3A runtime counters (global, non-PII)
        "workers": {"max_file_workers": ctx.settings.max_file_workers,
                    "worker_id": ctx.settings.resolved_worker_id},
        "work_counts": ctx.db.work_counts(),
        "target": {"inprocess": ctx.settings.target_inprocess},
        "schema_version": ctx.schema.version,
        "default_tenant_id": ctx.settings.default_tenant_id,
    }


# ===================== observability / product metrics =====================
@router.get("/observability")
def observability(request: Request) -> dict:
    """Engineering-observability status (LangSmith tracing, non-secret) + portfolio metrics."""
    ctx = _ctx(request)
    from ..observability import aggregate_metrics, tracing_status
    return {"tracing": tracing_status(ctx.settings), "metrics": aggregate_metrics(ctx.db)}


@router.get("/metrics")
def metrics(request: Request) -> dict:
    """Portfolio-level product metrics across recent jobs (what was automatic / AI / human / blocked)."""
    ctx = _ctx(request)
    from ..observability import aggregate_metrics
    return aggregate_metrics(ctx.db)


@router.get("/jobs/{job_id}/metrics")
def job_metrics_endpoint(request: Request, job_id: str) -> dict:
    """Per-job product metrics: mapping origin, source-intelligence, records, delivery, human decisions."""
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    from ..observability import job_metrics
    return job_metrics(ctx.db, job_id)


# ===================== schema (base + effective) =====================
@router.get("/schema", response_model=SchemaOut)
def get_schema(request: Request, tenant_id: str | None = None) -> SchemaOut:
    """The representative target contract. With ``tenant_id`` the EFFECTIVE schema (core +
    collections + that tenant's custom-field definitions) is returned."""
    ctx = _ctx(request)
    schema = ctx.effective_schema(tenant_id) if tenant_id else ctx.schema
    return SchemaOut(**schema.public_dict())


@router.get("/jobs/{job_id}/schema", response_model=SchemaOut)
def get_job_schema(request: Request, job_id: str) -> SchemaOut:
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    return SchemaOut(**ctx.effective_schema_for_job(job_id).public_dict())


# ===================== tenants + custom-field definitions =====================
def _def_out(d: dict) -> CustomFieldDefinitionOut:
    return CustomFieldDefinitionOut(
        id=d["id"], tenant_id=d["tenant_id"], key=d["key"], path=f"{CUSTOM_PATH_PREFIX}{d['key']}",
        label=d["label"], type=d["type"], required=bool(d["required"]), options=d.get("options"),
        multi_value=bool(d["multi_value"]), description=d.get("description"), aliases=d.get("aliases") or [],
        origin=d["origin"], origin_proposal_id=d.get("origin_proposal_id"), origin_job_id=d.get("origin_job_id"),
        created_by=d["created_by"], created_at=d["created_at"])


@router.get("/tenants", response_model=list[TenantOut])
def list_tenants(request: Request) -> list[TenantOut]:
    ctx = _ctx(request)
    return [TenantOut(id=t["id"], name=t["name"], created_at=t["created_at"],
                      custom_field_count=len(ctx.db.get_custom_field_definitions(t["id"])))
            for t in ctx.db.list_tenants()]


@router.get("/tenants/{tenant_id}/custom-fields", response_model=list[CustomFieldDefinitionOut])
def list_custom_fields(request: Request, tenant_id: str) -> list[CustomFieldDefinitionOut]:
    ctx = _ctx(request)
    return [_def_out(d) for d in ctx.db.get_custom_field_definitions(tenant_id)]


@router.post("/tenants/{tenant_id}/custom-fields", response_model=CustomFieldDefinitionOut, status_code=201)
def create_custom_field(request: Request, tenant_id: str, body: CustomFieldDefinitionIn) -> CustomFieldDefinitionOut:
    """Register an EXISTING tenant custom field (tenant configuration). Tenant-scoped: another
    tenant never sees it. Duplicate key within the tenant -> 409."""
    ctx = _ctx(request)
    err = validate_definition_input(ctx.schema, body.key, body.label, body.type, body.options, body.multi_value)
    if err:
        raise HTTPException(status_code=400, detail=err)
    if body.job_id and (ctx.db.get_job(body.job_id) or {}).get("tenant_id", ctx.settings.default_tenant_id) != tenant_id:
        raise HTTPException(status_code=400, detail="job_id does not belong to this tenant.")
    d = ctx.db.add_custom_field_definition(
        tenant_id=tenant_id, key=body.key, label=body.label, type=body.type, required=body.required,
        options=body.options, multi_value=body.multi_value, description=body.description,
        aliases=body.aliases or [], origin="api", origin_job_id=body.job_id, created_by="human")
    if d is None:
        raise HTTPException(status_code=409, detail=f"Custom field key '{body.key}' already exists for tenant '{tenant_id}'.")
    if body.job_id:
        ctx.db.add_audit(body.job_id, event_type="custom_field_created", actor="human",
                         source_ref={"tenant_id": tenant_id},
                         after={"definition_id": d["id"], "key": d["key"], "label": d["label"], "type": d["type"],
                                "options": d.get("options"), "origin": "api"},
                         reason=body.description)
    return _def_out(d)


# ===================== jobs =====================
@router.post("/jobs", response_model=JobOut)
async def create_job(request: Request, files: list[UploadFile] = FileParam(...),
                     tenant_id: str | None = Form(None)) -> JobOut:
    """Execution boundary: validate + store blobs + persist metadata + enqueue per-file work,
    then return a queued job. NO parsing/mapping/prepare happens in this request."""
    ctx = _ctx(request)
    settings = ctx.settings
    if not files:
        raise HTTPException(status_code=400, detail="At least one file is required.")
    if len(files) > settings.max_files_per_job:
        raise HTTPException(status_code=400,
                            detail=f"Too many files (max {settings.max_files_per_job}).")
    tenant = (tenant_id or "").strip() or settings.default_tenant_id
    if len(tenant) > 64 or not all(ch.isalnum() or ch in "-_." for ch in tenant):
        raise HTTPException(status_code=400, detail="tenant_id must be 1-64 chars of [A-Za-z0-9-_.].")

    # Cheap validation only (extension + size) — NOT a full parse. An unsupported/oversized
    # file rejects the whole upload before any job is created.
    staged: list[tuple[str, bytes, str]] = []   # (filename, data, ext)
    for uf in files:
        data = await uf.read()
        try:
            ext = validate_upload(uf.filename or "upload", len(data), settings.max_upload_bytes)
        except UnsupportedFormatError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except UploadTooLargeError as e:
            raise HTTPException(status_code=413, detail=str(e))
        staged.append((uf.filename or "upload", data, ext))

    job_id = ctx.db.create_job(schema_version=ctx.schema.version, provider=ctx.provider,
                               model_id=ctx.model_id, adapter_kind=ctx.adapter_kind, tenant_id=tenant)

    for filename, data, ext in staged:
        info = ctx.blobstore.put(data, suffix=ext)              # server-generated key
        content_type = ("text/csv" if ext == ".csv"
                        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        file_id = f"file_{info.key.split('.')[0][:12]}"
        ctx.db.add_source_file(job_id, file_id=file_id, original_filename=filename,
                               stored_name=info.key, content_type=content_type,
                               size_bytes=info.size_bytes, blob_key=info.key, sha256=info.sha256,
                               storage_status="stored", parse_status="queued")
        ctx.db.enqueue_work(job_id=job_id, kind="INGEST_FILE", source_file_id=file_id,
                            idempotency_key=f"{job_id}:ingest:{file_id}",
                            max_attempts=settings.work_max_attempts)

    ctx.db.add_audit(job_id, event_type="queued", actor="system",
                     after={"files": [s[0] for s in staged], "work_items": len(staged), "tenant_id": tenant},
                     schema_version=ctx.schema.version)
    ctx.db.set_job_stage(job_id, status="queued", stage="queued")
    return _job_out(ctx, _job_or_404(ctx, job_id))


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(request: Request) -> list[JobOut]:
    ctx = _ctx(request)
    return [_job_out(ctx, j) for j in ctx.db.list_jobs()]


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(request: Request, job_id: str) -> JobOut:
    ctx = _ctx(request)
    return _job_out(ctx, _job_or_404(ctx, job_id))


@router.get("/jobs/{job_id}/files", response_model=list[SourceFileOut])
def get_files(request: Request, job_id: str) -> list[SourceFileOut]:
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    return [SourceFileOut(
        id=f["id"], job_id=f["job_id"], original_filename=f["original_filename"],
        content_type=f["content_type"], size_bytes=f["size_bytes"], sha256=f.get("sha256"),
        storage_status=f.get("storage_status"), parse_status=f.get("parse_status"),
        parse_error=f.get("parse_error"), created_at=f["created_at"]) for f in ctx.db.get_source_files(job_id)]


_DELIVERY_STARTED_MSG = (
    "This migration has already started writing to the target. Source files cannot be removed safely "
    "now. Rollback the migration or start a new migration.")
_ACTIVE_WORK_MSG = "Wait for the current step to finish, then remove the file."


def _delete_blobs(ctx, blob_keys: list[str], job_id: str, *, context: str) -> tuple[int, int]:
    """Best-effort, idempotent blob deletion AFTER the DB transaction committed. A failed blob delete
    never corrupts the migration: it is recorded as an actionable cleanup event and counted."""
    deleted = pending = 0
    for key in blob_keys:
        try:
            ctx.blobstore.delete(key)
            deleted += 1
        except Exception as e:  # noqa: BLE001 - a stuck blob must not fail the committed mutation
            pending += 1
            ctx.db.add_audit(job_id, event_type="blob_cleanup_pending", actor="system",
                             after={"blob_key": key, "context": context}, reason=str(e)[:300])
    return deleted, pending


def _remove_files_service(ctx, job: dict, file_ids: list[str], note: str | None) -> RemoveFilesOut:
    """Validate + atomically remove source files and rebuild the migration from the remaining set.
    Shared by the bulk endpoint and the single-file DELETE. Raises HTTPException on any validation or
    safety failure; nothing is mutated unless every check passes."""
    job_id = job["id"]
    ids = list(dict.fromkeys(f for f in file_ids if f))          # de-dup, drop blanks, keep order
    if not ids:
        raise HTTPException(status_code=400, detail="Provide at least one file to remove.")
    all_files = ctx.db.get_source_files(job_id)
    known = {f["id"] for f in all_files}
    unknown = [f for f in ids if f not in known]
    if unknown:  # a file id from another job (or a stale id) is rejected — never cross-job mutation
        raise HTTPException(status_code=404,
                            detail=f"File(s) not part of this migration: {', '.join(unknown)}.")
    if len(all_files) - len(ids) < 1:
        raise HTTPException(status_code=400,
                            detail="A migration needs at least one source file. Delete the migration instead.")
    if ctx.db.has_target_delivery_activity(job_id):
        raise HTTPException(status_code=409, detail=_DELIVERY_STARTED_MSG)
    if ctx.db.has_active_work(job_id):
        raise HTTPException(status_code=409, detail=_ACTIVE_WORK_MSG)

    result = ctx.db.remove_source_files(job_id, ids, actor="human", note=note,
                                        map_max_attempts=ctx.settings.work_max_attempts)
    if result["status"] == "active_work":                       # raced the worker inside the lock
        raise HTTPException(status_code=409, detail=_ACTIVE_WORK_MSG)
    deleted, pending = _delete_blobs(ctx, result["blob_keys"], job_id, context="source_files_removed")
    return RemoveFilesOut(
        job_id=job_id,
        removed_file_ids=[r["id"] for r in result["removed"]],
        removed_filenames=[r["original_filename"] for r in result["removed"]],
        remaining_files=result["remaining"],
        rebuild_enqueued=bool(result.get("map_work_id")),
        blobs_deleted=deleted,
        blob_cleanup_pending=pending,
        preserved_custom_field_definitions=result.get("preserved_custom_field_definitions", 0))


@router.post("/jobs/{job_id}/files/remove", response_model=RemoveFilesOut)
def remove_files(request: Request, job_id: str, body: RemoveFilesIn) -> RemoveFilesOut:
    """Atomically remove one or more source files from an in-progress migration and rebuild it from the
    remaining files (mapping → preparation → reconciliation recompute via the normal worker path). Only
    allowed before any target write; never deletes anything from the target system."""
    ctx = _ctx(request)
    job = _job_or_404(ctx, job_id)
    return _remove_files_service(ctx, job, body.file_ids, body.note)


@router.delete("/jobs/{job_id}/files/{file_id}", response_model=RemoveFilesOut)
def remove_file(request: Request, job_id: str, file_id: str) -> RemoveFilesOut:
    """Single-file convenience that delegates to the same atomic bulk service."""
    ctx = _ctx(request)
    job = _job_or_404(ctx, job_id)
    return _remove_files_service(ctx, job, [file_id], None)


@router.delete("/jobs/{job_id}", response_model=DeleteMigrationOut)
def delete_migration(request: Request, job_id: str) -> DeleteMigrationOut:
    """Completely and permanently delete a whole migration and ALL of its data — source files, mappings,
    prepared employees, reconciliation, versions, metrics, work items and the audit trail. Blocked while
    the target still holds migration changes that were not rolled back: a local delete must never hide a
    live target side effect. Organization custom fields and other migrations are preserved."""
    ctx = _ctx(request)
    job = ctx.db.get_job(job_id)
    if job is None or job.get("deleted_at"):
        return DeleteMigrationOut(job_id=job_id, deleted=True, blobs_deleted=0)   # idempotent: already gone
    if ctx.db.has_unreverted_target_writes(job_id):
        raise HTTPException(status_code=409, detail=(
            "This migration has already changed the target system. Roll back those changes before "
            "deleting the migration."))
    result = ctx.db.delete_job(job_id, actor="human")
    if result["status"] == "active_work":
        raise HTTPException(status_code=409, detail=_ACTIVE_WORK_MSG)
    deleted, pending = _delete_blobs(ctx, result["blob_keys"], job_id, context="migration_deleted")
    return DeleteMigrationOut(job_id=job_id, deleted=True, blobs_deleted=deleted,
                             blob_cleanup_pending=pending, org_deleted=result.get("org_deleted"))


@router.get("/jobs/{job_id}/work-items", response_model=list[WorkItemOut])
def get_work_items(request: Request, job_id: str) -> list[WorkItemOut]:
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    return [WorkItemOut(
        id=w["id"], job_id=w["job_id"], source_file_id=w["source_file_id"], kind=w["kind"],
        status=w["status"], attempt=w["attempt"], max_attempts=w["max_attempts"],
        worker_id=w["worker_id"], last_error_category=w["last_error_category"],
        last_error=w["last_error"], idempotency_key=w["idempotency_key"],
        created_at=w["created_at"], updated_at=w["updated_at"]) for w in ctx.db.get_work_items(job_id)]


@router.get("/jobs/{job_id}/profiles", response_model=ProfilesOut)
def get_profiles(request: Request, job_id: str) -> ProfilesOut:
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    eff = ctx.effective_schema_for_job(job_id)
    profiles = ctx.db.get_profiles(job_id)
    by_table: dict[str, list[ColumnProfileOut]] = {}
    for p in profiles:
        by_table.setdefault(p["table_id"], []).append(ColumnProfileOut(
            profile_id=p["id"], table_id=p["table_id"], col_index=p["col_index"], header=p["header"],
            non_empty_count=p["non_empty_count"], missing_count=p["missing_count"],
            distinct_count=p["distinct_count"], observed_types=json.loads(p["observed_types"]),
            format_indicators=json.loads(p["format_indicators"]), samples=json.loads(p["samples"]),
        ))
    tables = []
    for t in ctx.db.get_tables(job_id):
        coll = eff.collection_for_table_name(table_context_name(t["original_filename"], t["sheet_name"]))
        tables.append(TableOut(
            table_id=t["id"], file_id=t["file_id"], original_filename=t["original_filename"],
            sheet_name=t["sheet_name"], headers=json.loads(t["headers"]), n_rows=t["n_rows"],
            table_role=(f"child:{coll.key}" if coll else "employee"),
            profiles=sorted(by_table.get(t["id"], []), key=lambda x: x.col_index)))
    issues = [ParsingIssueOut(table_id=i["table_id"], kind=i["kind"], detail=i["detail"],
                              severity=i["severity"]) for i in ctx.db.get_parsing_issues(job_id)]
    return ProfilesOut(job_id=job_id, tables=tables, parsing_issues=issues)


def _table_or_404(ctx, job_id: str, table_id: str) -> dict:
    table = next((t for t in ctx.db.get_tables(job_id) if t["id"] == table_id), None)
    if table is None:
        raise HTTPException(status_code=404, detail="Table not found for this job.")
    return table


def _row_out(r: dict) -> StagedRowOut:
    cells = json.loads(r["cells"])
    return StagedRowOut(row_number=r["row_number"], cells=[
        StagedCellOut(col_index=c.get("col_index", i), header=c.get("header", ""), value=c.get("value"))
        for i, c in enumerate(cells)])


@router.get("/jobs/{job_id}/tables/{table_id}/rows", response_model=StagedRowsOut)
def get_staged_rows(request: Request, job_id: str, table_id: str,
                    offset: int = 0, limit: int = 50) -> StagedRowsOut:
    """Bounded, read-only preview of the RAW STAGED rows persisted for one table (M3A.1).
    This is source/staging data, NOT final target data. The table must belong to the job."""
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    table = _table_or_404(ctx, job_id, table_id)
    offset = max(0, offset)
    limit = max(1, min(limit, 100))                      # hard server-side cap
    total = ctx.db.count_source_rows(table_id)
    rows = [_row_out(r) for r in ctx.db.get_source_rows_page(table_id, offset=offset, limit=limit)]
    return StagedRowsOut(job_id=job_id, table_id=table_id,
                         original_filename=table["original_filename"], sheet_name=table["sheet_name"],
                         headers=json.loads(table["headers"]), total=total, offset=offset,
                         limit=limit, rows=rows)


@router.get("/jobs/{job_id}/tables/{table_id}/rows/{row_number}", response_model=SourceRowContextOut)
def get_source_row(request: Request, job_id: str, table_id: str, row_number: int,
                   context: int = 2, header: str | None = None, col_index: int | None = None) -> SourceRowContextOut:
    """Source deep-link (M3A.2): the EXACT persisted staged row that a provenance reference points
    at, plus a few neighbouring rows, with the referenced header/cell identified for highlighting.
    File -> Sheet -> Row -> Header. Read-only; the table must belong to the job."""
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    table = _table_or_404(ctx, job_id, table_id)
    headers = json.loads(table["headers"])
    context = max(0, min(context, 10))
    rows = ctx.db.get_source_row_context(table_id, row_number, context=context)
    exact = next((r for r in rows if r["row_number"] == row_number), None)
    if exact is None:
        raise HTTPException(status_code=404, detail="Source row not found in this table.")
    hi_col = col_index if col_index is not None and 0 <= col_index < len(headers) else None
    if hi_col is None and header is not None:
        hi_col = next((i for i, h in enumerate(headers) if h == header), None)
    hi_header = headers[hi_col] if hi_col is not None else header
    return SourceRowContextOut(
        job_id=job_id, table_id=table_id, file_id=table["file_id"], original_filename=table["original_filename"],
        sheet_name=table["sheet_name"], headers=headers, row_number=row_number, highlight_header=hi_header,
        highlight_col_index=hi_col, row=_row_out(exact), context=[_row_out(r) for r in rows],
        total_rows=ctx.db.count_source_rows(table_id))


# ===================== mappings =====================
def _dest_kind(eff, d: dict) -> str:
    if d["status"] == "ignored":
        return "IGNORED"
    if d["status"] in ("rejected", "unmapped"):
        return "UNMAPPED"
    return d.get("destination_kind") or eff.destination_kind(d["target_field"])


@router.get("/jobs/{job_id}/mappings", response_model=MappingsOut)
def get_mappings(request: Request, job_id: str) -> MappingsOut:
    """Every source column's disposition: mapped (with destination type), review required,
    custom-field proposal, or explicitly ignored. Nothing is silently discarded."""
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    eff = ctx.effective_schema_for_job(job_id)
    decisions = ctx.db.get_decisions(job_id)
    open_props = {p["profile_id"]: p for p in ctx.db.get_custom_field_proposals(job_id, status="open")}

    def row(d: dict, **kw) -> MappingRowOut:
        return MappingRowOut(profile_id=d["profile_id"], table_id=d["table_id"], source_header=d["source_header"],
                             target_field=d["target_field"], status=d["status"], actor=d["actor"],
                             method=d.get("method"), reason=d["reason"], destination_kind=_dest_kind(eff, d),
                             path_meta=json.loads(d["path_meta"]) if d.get("path_meta") else None,
                             custom_definition_id=d.get("custom_definition_id"), note=d.get("note"), **kw)

    accepted = [row(d) for d in decisions if d["status"] in _ACCEPTED]
    unresolved = [MappingRowOut(profile_id=i["profile_id"], table_id=i["table_id"],
                                source_header=i["source_header"], target_field=i["proposed_target_field"],
                                status="needs_review", actor="system", method="unresolved",
                                destination_kind="NEEDS_REVIEW",
                                reason=json.loads(i["evidence_summary"]).get("_", None) or i["issue_type"])
                  for i in ctx.db.get_issues(job_id, status="open")]
    unresolved += [row(d, proposal_id=(open_props.get(d["profile_id"]) or {}).get("id"))
                   for d in decisions if d["status"] in ("rejected", "unmapped")]
    ignored = [row(d) for d in decisions if d["status"] == "ignored"]
    decided_profiles = {d["profile_id"] for d in decisions}
    proposals = []
    for p in open_props.values():
        sug = json.loads(p["suggestion"])
        proposals.append(MappingRowOut(
            profile_id=p["profile_id"], table_id=p["table_id"], source_header=p["source_header"],
            target_field=sug.get("path"), status="proposal", actor="system", method="unresolved",
            destination_kind="PROPOSAL", reason=f"custom-field proposal ({p['origin']}); awaiting a human decision",
            proposal_id=p["id"]))
        if p["profile_id"] not in decided_profiles:
            pass
    counts = {"accepted": len(accepted), "needs_review": len([u for u in unresolved if u.status == "needs_review"]),
              "unmapped": len([u for u in unresolved if u.status in ("rejected", "unmapped")]),
              "ignored": len(ignored), "proposals": len(proposals),
              "core": len([a for a in accepted if a.destination_kind == "CORE_FIELD"]),
              "collection": len([a for a in accepted if a.destination_kind == "COLLECTION_FIELD"]),
              "custom": len([a for a in accepted if a.destination_kind == "CUSTOM_FIELD"])}
    return MappingsOut(job_id=job_id, accepted=accepted, unresolved=unresolved, ignored=ignored,
                       proposals=proposals, counts=counts)


def _issue_out(i: dict) -> ReviewIssueOut:
    return ReviewIssueOut(
        id=i["id"], job_id=i["job_id"], profile_id=i["profile_id"], table_id=i["table_id"],
        source_header=i["source_header"], issue_type=i["issue_type"],
        proposed_target_field=i["proposed_target_field"],
        candidate_target_fields=json.loads(i["candidate_target_fields"]),
        evidence_summary=json.loads(i["evidence_summary"]),
        affected_non_empty_rows=i["affected_non_empty_rows"], status=i["status"], version=i["version"],
        resolution=json.loads(i["resolution"]) if i["resolution"] else None,
    )


@router.get("/jobs/{job_id}/reviews", response_model=list[ReviewIssueOut])
def get_reviews(request: Request, job_id: str, status: str = "open") -> list[ReviewIssueOut]:
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    return [_issue_out(i) for i in ctx.db.get_issues(job_id, status=status)]


@router.post("/jobs/{job_id}/reviews/{issue_id}/decision", response_model=DecisionOut)
async def submit_decision(request: Request, job_id: str, issue_id: str, body: DecisionIn) -> DecisionOut:
    """Persist a mapping decision AND (if it clears the last open issue) enqueue RESUME_MAPPING
    in one atomic transaction. The worker performs the resume; the request does not run a graph."""
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    issue = ctx.db.get_issue(issue_id)
    if not issue or issue["job_id"] != job_id:
        raise HTTPException(status_code=404, detail=f"Issue '{issue_id}' not found for this job.")

    eff = ctx.effective_schema_for_job(job_id)
    valid_fields = set(eff.target_paths)          # mapping never invents an unknown target path
    if body.action == "approve" and not issue["proposed_target_field"]:
        raise HTTPException(status_code=400,
                            detail="Cannot approve an unresolved issue (no proposed target). Use 'correct'.")
    if body.action == "approve" and issue["proposed_target_field"] not in valid_fields:
        raise HTTPException(status_code=400,
                            detail=f"Proposed target '{issue['proposed_target_field']}' is not in the effective contract; use 'correct'.")
    if body.action == "correct" and (not body.corrected_target or body.corrected_target not in valid_fields):
        raise HTTPException(status_code=400,
                            detail=f"'corrected_target' must be a destination path of the effective contract "
                                   f"(core field, collection item path or tenant custom field).")

    resolution = {"action": body.action,
                  "corrected_target": body.corrected_target if body.action == "correct" else None,
                  "reason": body.reason, "note": body.note, "actor": "human"}
    outcome, enqueued = ctx.db.resolve_mapping_issue_and_enqueue(
        job_id, issue_id, expected_version=body.version, resolution=resolution)
    _raise_for_outcome(ctx.db.get_issue, issue_id, outcome)

    open_remaining = len(ctx.db.get_issues(job_id, status="open"))
    return DecisionOut(issue=_issue_out(ctx.db.get_issue(issue_id)), outcome=outcome,
                       resume_triggered=enqueued, resume_status="queued" if enqueued else None,
                       open_issues_remaining=open_remaining)


def _raise_for_outcome(getter, issue_id: str, outcome: str) -> None:
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="Issue not found.")
    if outcome == "stale":
        cur = getter(issue_id)
        raise HTTPException(status_code=409, detail={
            "message": "Stale or conflicting decision; reload the issue and retry.",
            "current_version": cur["version"] if cur else None,
            "current_status": cur["status"] if cur else None})


@router.post("/jobs/{job_id}/retry-mapping", response_model=JobOut)
async def retry_mapping(request: Request, job_id: str) -> JobOut:
    """Retry the model step for a job blocked on provider configuration (after setting the key
    and restarting). Enqueues a fresh MAP work item; rule decisions are idempotent."""
    ctx = _ctx(request)
    job = _job_or_404(ctx, job_id)
    if job["status"] != "blocked_provider":
        raise HTTPException(status_code=409, detail=f"Job is '{job['status']}', not blocked_provider.")
    if ctx.adapter is None:
        raise HTTPException(status_code=409, detail=ctx.adapter_error or "Provider still not configured.")
    _enqueue_stage(ctx, job_id, "MAP", f"{job_id}:map")
    return _job_out(ctx, _job_or_404(ctx, job_id))


# ===================== custom-field proposals =====================
def _proposal_out(p: dict) -> CustomFieldProposalOut:
    return CustomFieldProposalOut(
        id=p["id"], job_id=p["job_id"], tenant_id=p["tenant_id"], profile_id=p["profile_id"],
        table_id=p["table_id"], source_header=p["source_header"], origin=p["origin"],
        suggestion=json.loads(p["suggestion"]), observed_values=json.loads(p["observed_values"]),
        non_empty_count=p["non_empty_count"], status=p["status"], version=p["version"],
        resolution=json.loads(p["resolution"]) if p["resolution"] else None,
        definition_id=p.get("definition_id"), created_at=p["created_at"], updated_at=p["updated_at"])


@router.get("/jobs/{job_id}/custom-field-proposals", response_model=list[CustomFieldProposalOut])
def get_custom_field_proposals(request: Request, job_id: str, status: str = "open") -> list[CustomFieldProposalOut]:
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    rows = ctx.db.get_custom_field_proposals(job_id, status=None if status == "all" else status)
    return [_proposal_out(p) for p in rows]


_REMAP_OK = {"blocked_provider", "mapping_complete", "preparation_complete", "reconciliation_complete",
             "awaiting_record_review", "awaiting_target_review", "preparing_records", "error"}


@router.post("/jobs/{job_id}/custom-field-proposals/{proposal_id}/decision", response_model=ProposalDecisionOut)
async def decide_custom_field_proposal(request: Request, job_id: str, proposal_id: str,
                                       body: ProposalDecisionIn) -> ProposalDecisionOut:
    """Human decision on an unmapped source field: approve (CREATE the tenant custom field and map the
    column to it), map_existing (map to an existing tenant custom field), map_target (map to a core /
    collection path), or ignore (explicit, audited). Persisted atomically with the mapping decision,
    the audit event and — once no open proposals remain — a MAP re-run so the column is re-mapped
    against the newly effective contract. The target schema is never mutated without this approval."""
    ctx = _ctx(request)
    job = _job_or_404(ctx, job_id)
    p = ctx.db.get_custom_field_proposal(proposal_id)
    if not p or p["job_id"] != job_id:
        raise HTTPException(status_code=404, detail="Custom-field proposal not found for this job.")
    tenant = p["tenant_id"]
    eff = ctx.effective_schema(tenant)
    sug = json.loads(p["suggestion"])
    resolution = {"action": body.action, "note": body.note, "reason": body.reason, "actor": "human"}
    definition: dict | None = None
    decision_base = {"profile_id": p["profile_id"], "table_id": p["table_id"], "source_header": p["source_header"],
                     "actor": "human", "method": "human", "note": body.note}
    audit_after: dict = {"proposal_id": proposal_id, "source_header": p["source_header"], "tenant_id": tenant,
                         "note": body.note}

    if body.action == "approve":
        key = (body.key or sug.get("key") or "").strip()
        label = (body.label or sug.get("label") or p["source_header"]).strip()
        vtype = body.type or sug.get("type") or "string"
        options = body.options if body.options is not None else sug.get("options")
        multi = body.multi_value if body.multi_value is not None else bool(sug.get("multi_value"))
        required = bool(body.required) if body.required is not None else bool(sug.get("required"))
        err = validate_definition_input(eff, key, label, vtype, options, multi)
        if err:
            raise HTTPException(status_code=400, detail=err)
        if ctx.db.find_custom_field_definition(tenant, key):
            raise HTTPException(status_code=409, detail=(
                f"Custom field key '{key}' already exists for tenant '{tenant}'. Choose another key, or use "
                f"'map_existing' to map this source field to the existing definition."))
        definition = {"tenant_id": tenant, "key": key, "label": label, "type": vtype, "required": required,
                      "options": options, "multi_value": multi, "description": body.description,
                      "aliases": [p["source_header"]] if p["source_header"].strip().lower() != label.lower() else [],
                      "origin": "proposal", "origin_proposal_id": proposal_id, "origin_job_id": job_id,
                      "created_by": "human"}
        resolution.update(key=key, label=label, type=vtype, options=options, multi_value=multi, required=required)
        decision = dict(decision_base, target_field=f"{CUSTOM_PATH_PREFIX}{key}", status="approved",
                        destination_kind="CUSTOM_FIELD",
                        reason=f"Human approved custom-field proposal: '{p['source_header']}' -> {CUSTOM_PATH_PREFIX}{key}")
        new_status, audit_event = "approved", "custom_field_created"
        audit_after.update(key=key, label=label, type=vtype, options=options, multi_value=multi,
                           target=f"{CUSTOM_PATH_PREFIX}{key}", destination_kind="CUSTOM_FIELD")
    elif body.action == "map_existing":
        d = ctx.db.get_custom_field_definition(body.definition_id or "")
        if not d or d["tenant_id"] != tenant:
            raise HTTPException(status_code=400, detail="definition_id must be an existing custom field of this tenant.")
        resolution.update(definition_id=d["id"], key=d["key"])
        decision = dict(decision_base, target_field=f"{CUSTOM_PATH_PREFIX}{d['key']}", status="approved",
                        destination_kind="CUSTOM_FIELD", custom_definition_id=d["id"],
                        reason=f"Human mapped '{p['source_header']}' to existing tenant custom field '{d['key']}'")
        new_status, audit_event = "mapped_existing", "custom_field_mapped_existing"
        audit_after.update(definition_id=d["id"], key=d["key"], target=f"{CUSTOM_PATH_PREFIX}{d['key']}",
                           destination_kind="CUSTOM_FIELD")
    elif body.action == "map_target":
        path = (body.target_path or "").strip()
        tf = eff.get(path) if path else None
        if tf is None or path.startswith(CUSTOM_PATH_PREFIX):
            raise HTTPException(status_code=400, detail="target_path must be a core field or collection item path of the effective contract.")
        resolution.update(target_path=path)
        decision = dict(decision_base, target_field=path, status="corrected", destination_kind=eff.destination_kind(path),
                        reason=f"Human mapped '{p['source_header']}' to {path}")
        new_status, audit_event = "mapped_target", "issue_resolved"
        audit_after.update(target=path, destination_kind=eff.destination_kind(path), status="corrected")
    else:  # ignore
        decision = dict(decision_base, target_field=None, status="ignored", destination_kind="IGNORED",
                        reason=body.reason or "Source field explicitly ignored by consultant")
        new_status, audit_event = "ignored", "source_field_ignored"
        audit_after.update(status="ignored", destination_kind="IGNORED")

    rerun = "MAP" if job["status"] in _REMAP_OK else None
    outcome, enqueued, created = ctx.db.resolve_custom_field_proposal(
        job_id, proposal_id, expected_version=body.version, resolution=resolution, new_status=new_status,
        definition=definition, decision=decision, audit_event=audit_event, audit_after=audit_after,
        rerun_kind=rerun)
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="Custom-field proposal not found.")
    if outcome == "key_exists":
        raise HTTPException(status_code=409, detail=f"Custom field key already exists for tenant '{tenant}'.")
    if outcome == "stale":
        cur = ctx.db.get_custom_field_proposal(proposal_id)
        raise HTTPException(status_code=409, detail={
            "message": "Stale or conflicting proposal decision; reload and retry.",
            "current_version": cur["version"] if cur else None,
            "current_status": cur["status"] if cur else None})
    open_remaining = len(ctx.db.get_custom_field_proposals(job_id, status="open"))
    st = getattr(ctx.db, "_last_rerun_status", None)
    remap_status = "queued" if enqueued else ("will_rerun_after_current_mapping" if st == "already_active" else None)
    return ProposalDecisionOut(proposal=_proposal_out(ctx.db.get_custom_field_proposal(proposal_id)),
                               outcome=outcome, definition=_def_out(created) if created else None,
                               remap_triggered=enqueued, remap_status=remap_status,
                               open_proposals_remaining=open_remaining)


# ===================== preparation =====================
@router.post("/jobs/{job_id}/prepare", response_model=JobOut)
async def start_preparation(request: Request, job_id: str) -> JobOut:
    """Start (or idempotently re-run) M2 preparation. Fresh jobs auto-chain after mapping;
    this endpoint serves saved/completed jobs and re-runs. No Groq calls."""
    ctx = _ctx(request)
    job = _job_or_404(ctx, job_id)
    if job["status"] not in ("mapping_complete", "preparation_complete", "awaiting_record_review",
                             "preparing_records", "reconciling_target", "awaiting_target_review",
                             "reconciliation_complete"):
        raise HTTPException(status_code=409,
                            detail=f"Mapping is not complete (status '{job['status']}').")
    _enqueue_stage(ctx, job_id, "PREPARE", f"{job_id}:prepare")
    return _job_out(ctx, _job_or_404(ctx, job_id))


def _cand_out(c: dict) -> CandidateOut:
    return CandidateOut(
        id=c["id"], business_key=c["business_key"], eligibility=c["eligibility"],
        exclude_reason=c["exclude_reason"], issue_ids=json.loads(c["issue_ids"] or "[]"),
        source_refs=json.loads(c["source_refs"]), record=json.loads(c["record"]),
        collections=json.loads(c["collections"]) if c.get("collections") else {},
        custom_attributes=json.loads(c["custom_attributes"]) if c.get("custom_attributes") else [])


@router.get("/jobs/{job_id}/candidates", response_model=list[CandidateOut])
def get_candidates(request: Request, job_id: str) -> list[CandidateOut]:
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    return [_cand_out(c) for c in ctx.db.get_candidates(job_id)]


# ===================== M3A.1: employee version history =====================
def _candidate_or_404(ctx, job_id: str, candidate_id: str) -> dict:
    cand = next((c for c in ctx.db.get_candidates(job_id) if c["id"] == candidate_id), None)
    if cand is None:
        raise HTTPException(status_code=404, detail="Candidate not found for this job.")
    return cand


def _version_out(v: dict, current_no: int) -> EmployeeVersionOut:
    return EmployeeVersionOut(
        id=v["id"], job_id=v["job_id"], candidate_id=v["candidate_id"], business_key=v["business_key"],
        version_no=v["version_no"], parent_version_id=v["parent_version_id"], origin=v["origin"],
        snapshot=json.loads(v["snapshot"]), record_hash=v["record_hash"],
        change_reason=v["change_reason"], decision_note=v["decision_note"], decision_id=v["decision_id"],
        target_revision=v["target_revision"], restores_version_id=v["restores_version_id"],
        field_changes=json.loads(v["field_changes"]) if v["field_changes"] else None,
        created_by=v["created_by"], created_at=v["created_at"], is_current=(v["version_no"] == current_no))


@router.get("/jobs/{job_id}/candidates/{candidate_id}/versions", response_model=list[EmployeeVersionOut])
def get_employee_versions(request: Request, job_id: str, candidate_id: str) -> list[EmployeeVersionOut]:
    """Immutable, append-only version history for one employee (newest first)."""
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    _candidate_or_404(ctx, job_id, candidate_id)
    versions = ctx.db.get_employee_versions(job_id, candidate_id)
    current = versions[-1]["version_no"] if versions else 0
    return [_version_out(v, current) for v in reversed(versions)]


@router.get("/jobs/{job_id}/candidates/{candidate_id}/versions/compare", response_model=VersionCompareOut)
def compare_employee_versions(request: Request, job_id: str, candidate_id: str,
                              a: int, b: int) -> VersionCompareOut:
    """Deterministic comparison of two versions of the SAME employee: scalar fields, custom
    attributes, and per-collection item added / removed / changed."""
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    cand = _candidate_or_404(ctx, job_id, candidate_id)
    va = ctx.db.get_employee_version(job_id, candidate_id, a)
    vb = ctx.db.get_employee_version(job_id, candidate_id, b)
    if not va or not vb:
        raise HTTPException(status_code=404, detail="Version not found for this employee.")
    eff = ctx.effective_schema_for_job(job_id)
    cmp = compare_snapshots(json.loads(va["snapshot"]), json.loads(vb["snapshot"]), eff)
    return VersionCompareOut(
        job_id=job_id, candidate_id=candidate_id, business_key=cand["business_key"], a_version=a, b_version=b,
        fields=[VersionFieldDiffOut(**r) for r in cmp["fields"]],
        custom_attributes=[VersionFieldDiffOut(**r) for r in cmp["custom_attributes"]],
        collections=[VersionCollectionItemDiffOut(**r) for r in cmp["collections"]],
        changed_fields=cmp["changed_fields"], changed_collections=cmp["changed_collections"],
        changed_custom_attributes=cmp["changed_custom_attributes"])


@router.get("/jobs/{job_id}/candidates/{candidate_id}/versions/{version_no}", response_model=EmployeeVersionOut)
def get_one_employee_version(request: Request, job_id: str, candidate_id: str,
                             version_no: int) -> EmployeeVersionOut:
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    _candidate_or_404(ctx, job_id, candidate_id)
    versions = ctx.db.get_employee_versions(job_id, candidate_id)
    current = versions[-1]["version_no"] if versions else 0
    v = next((x for x in versions if x["version_no"] == version_no), None)
    if not v:
        raise HTTPException(status_code=404, detail="Version not found for this employee.")
    return _version_out(v, current)


# ===================== migration-admin employee editing / deletion =====================
_BUSY_STATUSES = {"delivering", "rollback_in_progress"}


def _reenqueue_admin_op(ctx, job_id: str, op_id: str, *, op_type: str, payload: dict,
                        revision: int | None, target_id: str | None, before: dict | None,
                        version_id: str | None, idem_key: str) -> None:
    """Re-purpose a candidate's SINGLE delivery operation (there is a UNIQUE(job_id, candidate_id)
    constraint, so we never create a second op) into a fresh PLANNED UPDATE/DELETE and enqueue a
    claimable work item. This is the same op-reuse the retry/replan paths use; the generation-aware
    work key guarantees the re-enqueue is not silently dropped, and delivery_attempts keeps the history."""
    ctx.db.update_delivery_operation(
        op_id, op_type=op_type, payload=payload, expected_target_revision=revision,
        target_record_id=target_id, employee_id=target_id, before_snapshot=before,
        desired_version_id=version_id, idempotency_key=idem_key, status="PLANNED",
        last_error=None, target_revision_after=None, target_request_id=None, work_item_id=None)
    ctx.db.enqueue_work(job_id=job_id, kind="DELIVER_OP",
                        idempotency_key=deliver_work_key(ctx.db, op_id),
                        max_attempts=ctx.settings.target_max_attempts,
                        payload={"operation_id": op_id})
    ctx.db.set_job_stage(job_id, status="delivering", stage="delivering")


def _changed_between(before: dict, after: dict) -> tuple[list[str], list[dict]]:
    """Field names + {field,old,new} entries that differ between two effective snapshots (scalars +
    organization/custom attributes; collections are handled elsewhere and not editable here)."""
    names: list[str] = []
    changes: list[dict] = []
    for f in [k for k in ({*before, *after}) if k not in ("collections", "custom_attributes")]:
        if before.get(f) != after.get(f):
            names.append(f)
            changes.append({"field": f, "from": before.get(f), "to": after.get(f)})
    b_ca = {c.get("key"): c.get("value") for c in before.get("custom_attributes", []) if isinstance(c, dict)}
    a_ca = {c.get("key"): c.get("value") for c in after.get("custom_attributes", []) if isinstance(c, dict)}
    for k in {*b_ca, *a_ca}:
        if b_ca.get(k) != a_ca.get(k):
            names.append(k)
            changes.append({"field": k, "from": b_ca.get(k), "to": a_ca.get(k)})
    return names, changes


@router.patch("/jobs/{job_id}/candidates/{candidate_id}", response_model=EditEmployeeOut)
def edit_employee(request: Request, job_id: str, candidate_id: str, body: EditEmployeeIn) -> EditEmployeeOut:
    """Migration-admin edit of an employee's SCALAR fields (core record fields + scalar organization
    fields). Every real change appends an immutable version (feeding the audit trail); when the
    employee is already synced, the change is auto-pushed to the target as a revision-checked,
    idempotent, rollback-able UPDATE via the durable delivery path — nothing is written inside this
    request. Values pass the same deterministic validation as any human correction."""
    ctx = _ctx(request)
    job = _job_or_404(ctx, job_id)
    cand = _candidate_or_404(ctx, job_id, candidate_id)
    eff = ctx.effective_schema_for_job(job_id)
    if not body.fields:
        raise HTTPException(status_code=400, detail="No fields to edit.")

    custom_keys = {c.get("key") for c in (json.loads(cand["custom_attributes"]) if cand.get("custom_attributes") else []) if isinstance(c, dict)}
    core: dict[str, Any] = {}
    custom: dict[str, Any] = {}
    for f, raw in body.fields.items():
        # employee_id is the identity key: the business key, the target match key, and the key the
        # target UPDATE is addressed by. Changing it would break matching, so it is never editable here.
        if f == "employee_id":
            raise HTTPException(status_code=400,
                                detail="Employee ID is the identity used to match the target system and cannot be changed here.")
        tf = eff.get(f)
        if tf is not None:                                  # a core scalar field
            value = "" if raw is None else str(raw)
            if value.strip() == "":
                if tf.required_in_final:
                    raise HTTPException(status_code=400, detail=f"'{f}' is required and cannot be blank.")
                core[f] = None
                continue
            err = _validate_correction(ctx, eff, f, value)
            if err:
                raise HTTPException(status_code=400, detail=f"{f}: {err}")
            from ..prepare import normalize_field
            core[f] = normalize_field(f, value, eff).value
        elif f in custom_keys:                              # a scalar organization/custom field
            custom[f] = None if raw is None else str(raw)
        else:
            raise HTTPException(status_code=400,
                                detail=f"'{f}' is not an editable scalar field on this employee.")

    before = effective_snapshot_from_candidate(cand, eff)
    ctx.db.update_candidate_scalar_fields(job_id, candidate_id, core=core, custom=custom, note=body.note)
    cand2 = ctx.db.get_candidate(job_id, candidate_id)
    after = effective_snapshot_from_candidate(cand2, eff)

    changed_fields, field_changes = _changed_between(before, after)
    if not changed_fields:
        return EditEmployeeOut(candidate_id=candidate_id, changed=False)

    ver = ctx.db.add_employee_version(
        job_id, candidate_id, snapshot=after, origin="admin_edit", created_by="human",
        business_key=cand.get("business_key"), change_reason="Migration-admin edit",
        decision_note=body.note, field_changes=field_changes, dedup=True)
    version_no = ver["version_no"] if ver else None
    ctx.db.add_audit(job_id, event_type="employee_edited", actor="human",
                     source_ref={"candidate_id": candidate_id, "business_key": cand.get("business_key")},
                     after={"changed_fields": changed_fields, "changes": field_changes,
                            "to_version": version_no, "origin": "admin_edit"},
                     reason=body.note)

    synced = ctx.db.latest_synced_target(job_id, candidate_id)
    if not synced:
        return EditEmployeeOut(candidate_id=candidate_id, changed=True, version_no=version_no,
                               changed_fields=changed_fields, pushed_to_target=False)

    if job["status"] in _BUSY_STATUSES:
        raise HTTPException(status_code=409, detail=(
            "A sync is currently in progress for this migration. The edit and its new version were "
            "saved; wait for the current sync to finish, then edit again to push it to the target."))

    core_changed = [f for f in changed_fields if eff.get(f) is not None]
    custom_changed = [f for f in changed_fields if f in custom_keys]
    patch: dict[str, Any] = {f: after.get(f) for f in core_changed}
    if custom_changed:
        patch["custom_attributes"] = [c for c in after.get("custom_attributes", [])
                                      if isinstance(c, dict) and c.get("key") in custom_changed]
    op_id = synced["op"]["id"]
    _reenqueue_admin_op(ctx, job_id, op_id, op_type="UPDATE", payload=patch,
                        revision=synced["revision"], target_id=synced["employee_id"], before=before,
                        version_id=(ver["id"] if ver else None),
                        idem_key=f"edit:{candidate_id}:{version_no}")
    return EditEmployeeOut(candidate_id=candidate_id, changed=True, version_no=version_no,
                           changed_fields=changed_fields, pushed_to_target=True, delivery_op_id=op_id)


@router.delete("/jobs/{job_id}/candidates/{candidate_id}", response_model=DeleteEmployeeOut)
def delete_employee(request: Request, job_id: str, candidate_id: str, note: str | None = None) -> DeleteEmployeeOut:
    """Migration-admin delete of an ALREADY-SYNCED employee: pushes a revision-checked, idempotent
    DELETE to the target via the durable delivery path and records a version marking the removal. A
    not-yet-synced employee cannot be deleted here (there is nothing in the target) — exclude it before
    syncing instead."""
    ctx = _ctx(request)
    job = _job_or_404(ctx, job_id)
    cand = _candidate_or_404(ctx, job_id, candidate_id)
    eff = ctx.effective_schema_for_job(job_id)
    synced = ctx.db.latest_synced_target(job_id, candidate_id)
    if not synced:
        raise HTTPException(status_code=409, detail=(
            "This employee has not been synced to the target yet, so there is nothing to delete there. "
            "Exclude the employee before syncing instead."))
    if job["status"] in _BUSY_STATUSES:
        raise HTTPException(status_code=409, detail=(
            "A sync is currently in progress for this migration. Try the delete again once it finishes."))

    before = effective_snapshot_from_candidate(cand, eff)
    ver = ctx.db.add_employee_version(
        job_id, candidate_id, snapshot=before, origin="admin_delete", created_by="human",
        business_key=cand.get("business_key"), change_reason="Migration-admin delete",
        decision_note=note,
        field_changes=[{"field": "(employee)", "from": "present in target", "to": "deleted from target"}],
        dedup=False)
    op_id = synced["op"]["id"]
    ctx.db.add_audit(job_id, event_type="employee_delete_requested", actor="human",
                     source_ref={"candidate_id": candidate_id, "business_key": cand.get("business_key")},
                     after={"employee_id": synced["employee_id"], "origin": "admin_delete"}, reason=note)
    _reenqueue_admin_op(ctx, job_id, op_id, op_type="DELETE", payload={},
                        revision=synced["revision"], target_id=synced["employee_id"], before=before,
                        version_id=(ver["id"] if ver else None),
                        idem_key=f"delete:{candidate_id}:{synced['revision']}")
    return DeleteEmployeeOut(candidate_id=candidate_id, delete_requested=True, delivery_op_id=op_id)


def _record_issue_out(i: dict) -> RecordIssueOut:
    return RecordIssueOut(
        id=i["id"], job_id=i["job_id"], candidate_key=i["candidate_key"], field=i["field"],
        issue_type=i["issue_type"], reason=i["reason"], options=json.loads(i["options"]),
        affected=json.loads(i["affected"]), scope=json.loads(i["scope"]) if i["scope"] else None,
        status=i["status"], version=i["version"],
        resolution=json.loads(i["resolution"]) if i["resolution"] else None)


@router.get("/jobs/{job_id}/record-reviews", response_model=list[RecordIssueOut])
def get_record_reviews(request: Request, job_id: str, status: str = "open") -> list[RecordIssueOut]:
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    return [_record_issue_out(i) for i in ctx.db.get_record_issues(job_id, status=status)]


@router.post("/jobs/{job_id}/record-reviews/{issue_id}/decision", response_model=RecordDecisionOut)
async def submit_record_decision(request: Request, job_id: str, issue_id: str,
                                 body: RecordDecisionIn) -> RecordDecisionOut:
    """Persist a record decision AND (if it clears the last open record issue) enqueue
    RESUME_PREPARATION atomically. The worker recomputes + resumes; the request runs no graph.
    Incoming-vs-incoming: the reviewer selects a SOURCE-BACKED value, corrects, drops a child item,
    or excludes — there is no "keep current target" here because no confirmed target is involved."""
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    issue = ctx.db.get_record_issue(issue_id)
    if not issue or issue["job_id"] != job_id:
        raise HTTPException(status_code=404, detail="Record issue not found for this job.")
    eff = ctx.effective_schema_for_job(job_id)

    field = issue["field"]
    itype = issue["issue_type"]
    is_shared_email = itype == "shared_email"
    is_collection = itype in ("collection_conflict", "collection_item_invalid")
    affected = json.loads(issue["affected"]) if issue["affected"] else {}
    affected_ids = {a.get("candidate_id") for a in affected.get("candidates", [])}
    options = json.loads(issue["options"]) if issue["options"] else []
    scope = json.loads(issue["scope"]) if issue["scope"] else {}

    # M3C column-scoped reviews (one decision resolves the whole column) accept ONLY their own action;
    # a per-row action here would be stored as "resolved" yet apply no overlay, leaving the candidate
    # blocked with no open issue (a preparation-invariant failure). Reject it up front.
    if itype == "ambiguous_date" and body.action not in ("confirm_convention", "confirm_century_pivot", "null"):
        raise HTTPException(status_code=400,
                            detail="An ambiguous date column is resolved once with 'confirm_convention' (DMY/MDY) "
                                   "— or 'null' when the field is optional.")
    if itype == "unknown_enum" and body.action not in ("map_values", "null"):
        raise HTTPException(status_code=400,
                            detail="An unknown-value column is resolved once with 'map_values' ({source_value: target}) "
                                   "— or 'null' when the field is optional.")

    if itype == "orphan_child_row":
        if body.action != "exclude":
            raise HTTPException(status_code=400, detail="Orphan child rows can only be explicitly excluded (they are never attached by guess).")
    elif body.action == "drop_item":
        if not is_collection:
            raise HTTPException(status_code=400, detail="'drop_item' applies only to collection item issues.")
    elif body.action == "null":
        tf = eff.get(field) if field else None
        if tf is None or tf.required_in_final or is_collection:
            raise HTTPException(status_code=400,
                                detail=f"Cannot null '{field}': only nullable/optional scalar target fields may be nulled.")
    elif body.action in ("select", "correct"):
        if body.value is None:
            raise HTTPException(status_code=400, detail="'value' is required for select/correct.")
        if is_shared_email:
            if body.action != "correct":
                raise HTTPException(status_code=400, detail="shared_email resolves by 'correct' or 'exclude'.")
            if not body.candidate_id or body.candidate_id not in affected_ids:
                raise HTTPException(status_code=400,
                                    detail="'candidate_id' must identify one of the affected candidates.")
            if field != "work_email":
                raise HTTPException(status_code=400, detail="shared_email correction must set work_email.")
        if itype == "collection_conflict":
            if body.action != "select":
                raise HTTPException(status_code=400, detail="A collection conflict resolves by selecting one source-backed variant, dropping the item, or excluding the employee.")
            keys = {o.get("variant_key") for o in options if isinstance(o, dict)}
            if body.value not in keys:
                raise HTTPException(status_code=400, detail="'value' must be the variant_key of one of the offered variants.")
        elif itype == "collection_item_invalid":
            if body.action != "correct":
                raise HTTPException(status_code=400, detail="An invalid collection item resolves by correcting the item field, dropping the item, or excluding the employee.")
            item_field = (body.scope or {}).get("item_field")
            coll = eff.get_collection(affected.get("collection") or "")
            if not coll or not item_field or coll.get(item_field) is None:
                raise HTTPException(status_code=400, detail="scope.item_field must name an item field of the collection.")
            err = _validate_correction(ctx, eff, f"{coll.key}[].{item_field}", body.value)
            if err:
                raise HTTPException(status_code=400, detail=err)
        elif itype == "value_conflict" and body.action == "select":
            if str(body.value) not in {str(o) for o in options}:
                raise HTTPException(status_code=400, detail="'value' must be one of the source-backed options.")
        else:
            err = _validate_correction(ctx, eff, field, body.value)
            if err:
                raise HTTPException(status_code=400, detail=err)
    if body.action == "exclude" and is_shared_email:
        if not body.candidate_id or body.candidate_id not in affected_ids:
            raise HTTPException(status_code=400,
                                detail="'candidate_id' must identify one of the affected candidates to exclude.")
    if body.action == "confirm_convention" and body.convention not in ("DMY", "MDY"):
        raise HTTPException(status_code=400, detail="'convention' must be DMY or MDY.")
    if body.action == "confirm_century_pivot" and body.pivot is None:
        raise HTTPException(status_code=400,
                            detail="'pivot' (a four-digit cutoff year) is required for confirm_century_pivot.")
    if body.action == "map_values":
        if not body.value_map:
            raise HTTPException(status_code=400, detail="'value_map' is required for map_values.")
        tf = eff.get(field) if field else None
        allowed = set(tf.enum_values or ()) if tf else set()
        # Deterministic proof gate (order §5): every mapped TARGET must be a real enum value; a human
        # can no more invent a target value than the model can.
        bad = sorted({str(v) for v in body.value_map.values() if v not in allowed})
        if bad:
            raise HTTPException(status_code=400,
                                detail=f"map_values targets must each be one of {sorted(allowed)}; invalid: {bad}")
        unmapped = set(scope.get("unmapped_values") or
                       [d.get("value") for d in affected.get("distinct_values", [])])
        stray = sorted({str(s) for s in body.value_map if s not in unmapped})
        if stray:
            raise HTTPException(status_code=400,
                                detail=f"map_values may only map this column's unmapped values; stray: {stray}")

    resolution = {"action": body.action, "value": body.value, "convention": body.convention,
                  "value_map": body.value_map, "pivot": body.pivot,
                  "scope": body.scope, "candidate_id": body.candidate_id, "reason": body.reason,
                  "note": body.note, "actor": "human"}
    outcome, enqueued = ctx.db.resolve_record_issue_and_enqueue(
        job_id, issue_id, expected_version=body.version, resolution=resolution)
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="Record issue not found.")
    if outcome == "stale":
        cur = ctx.db.get_record_issue(issue_id)
        raise HTTPException(status_code=409, detail={
            "message": "Stale or conflicting record decision; reload and retry.",
            "current_version": cur["version"] if cur else None,
            "current_status": cur["status"] if cur else None})

    open_remaining = len(ctx.db.get_record_issues(job_id, status="open"))
    return RecordDecisionOut(
        issue=_record_issue_out(ctx.db.get_record_issue(issue_id)), outcome=outcome,
        resume_triggered=enqueued, resume_status="queued" if enqueued else None,
        open_record_issues_remaining=open_remaining)


def _validate_correction(ctx, eff, field: str | None, value: str) -> str | None:
    """A human correction must pass the same deterministic syntactic checks as source data."""
    from ..prepare import normalize_field
    tf = eff.get(field) if field else None
    if tf is None:
        return None
    fv = normalize_field(field, value, eff)
    if fv.status == "resolved" and fv.value not in (None, "", []):
        return None
    if tf.value_type == "email":
        return "Corrected email is not valid."
    if tf.value_type == "date":
        return "Corrected date is not a valid unambiguous calendar date."
    if tf.value_type in ("enum", "multiselect"):
        return f"Value must be one of {list(tf.enum_values or ())}."
    if tf.value_type == "phone":
        return "Corrected phone number does not look like a phone number."
    if tf.value_type == "number":
        return "Corrected value must be a number."
    if tf.value_type == "boolean":
        return "Corrected value must be true/false (or yes/no)."
    return None if str(value).strip() else "Value cannot be empty."


@router.get("/jobs/{job_id}/prepared-dataset", response_model=PreparedDatasetOut)
def get_prepared_dataset(request: Request, job_id: str) -> PreparedDatasetOut:
    """Internal read endpoint: ONLY validated eligible target objects (core scalars + structured
    collections + tenant custom attributes), with a separate disposition view. Excluded/unresolved
    records are never in the payload."""
    ctx = _ctx(request)
    job = _job_or_404(ctx, job_id)
    eff = ctx.effective_schema_for_job(job_id)
    candidates = ctx.db.get_candidates(job_id)
    employees, excluded = [], []
    for c in candidates:
        if c["eligibility"] == "eligible":
            employees.append(effective_snapshot_from_candidate(c, eff))
        elif c["eligibility"] == "excluded":
            excluded.append({"business_key": c["business_key"], "reason": c["exclude_reason"],
                             "source_refs": json.loads(c["source_refs"])})
    prep = json.loads(job["prep_summary"]) if job.get("prep_summary") else {}
    return PreparedDatasetOut(
        job_id=job_id, schema_version=eff.version, tenant_id=eff.tenant_id or ctx.settings.default_tenant_id,
        ready_for_target=len(employees), employees=employees, excluded=excluded,
        provenance_versions={"schema_version": eff.version, "policy_version": POLICY_VERSION,
                             "rules_version": RULE_VERSION, "normalization_version": NORMALIZATION_VERSION,
                             "prep_result": prep.get("result")})


# ===================== M3A: target reconciliation =====================
@router.post("/jobs/{job_id}/reconcile", response_model=JobOut)
async def start_reconciliation(request: Request, job_id: str) -> JobOut:
    """Operator-initiated M2->M3A step. Enqueues TARGET_RECONCILE (idempotent). Preparation
    must be complete; reconciliation reads the confirmed target (via the gateway) and never writes."""
    ctx = _ctx(request)
    job = _job_or_404(ctx, job_id)
    # M3B.2: a job parked in stale_target_review_required (a delivery 409 whose re-reconciliation
    # needs human review) is resolved by re-running the full reconciliation, which re-fetches the
    # current target, surfaces the conflict as a target-review issue, and — once the human resolves
    # it — produces READY_UPDATE against the new revision so delivery can replan.
    if job["status"] not in ("preparation_complete", "reconciling_target", "awaiting_target_review",
                             "reconciliation_complete", "stale_target_review_required"):
        raise HTTPException(status_code=409,
                            detail=f"Preparation is not complete (status '{job['status']}').")
    _enqueue_stage(ctx, job_id, "TARGET_RECONCILE", f"{job_id}:reconcile")
    return _job_out(ctx, _job_or_404(ctx, job_id))


@router.get("/jobs/{job_id}/reconciliation", response_model=ReconciliationOut)
def get_reconciliation(request: Request, job_id: str) -> ReconciliationOut:
    ctx = _ctx(request)
    job = _job_or_404(ctx, job_id)
    rows = [ReconciliationRowOut(
        candidate_id=r["candidate_id"], business_key=r["business_key"], outcome=r["outcome"],
        target_record_id=r["target_record_id"], target_revision=r["target_revision"],
        match_basis=r["match_basis"], diff=json.loads(r["diff"]) if r["diff"] else None)
        for r in ctx.db.get_target_reconciliation(job_id)]
    counts = {k: v for k, v in _job_counts(ctx, job_id).items()
              if k in ("ready_create", "ready_update", "no_change", "review_required",
                       "excluded_target", "open_target_issues")}
    counts["prepared"] = len(rows)
    return ReconciliationOut(
        job_id=job_id, status=job["status"], counts=counts,
        summary=json.loads(job["recon_summary"]) if job.get("recon_summary") else None, results=rows)


@router.get("/jobs/{job_id}/target-snapshots", response_model=list[TargetSnapshotOut])
def get_target_snapshots(request: Request, job_id: str) -> list[TargetSnapshotOut]:
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    return [TargetSnapshotOut(
        candidate_id=s["candidate_id"], business_key=s["business_key"],
        target_record_id=s["target_record_id"], match_basis=s["match_basis"],
        target_revision=s["target_revision"],
        target_payload=json.loads(s["target_payload"]) if s["target_payload"] else None,
        fetched_at=s["fetched_at"]) for s in ctx.db.get_target_snapshots(job_id)]


def _target_issue_out(i: dict) -> TargetReviewIssueOut:
    return TargetReviewIssueOut(
        id=i["id"], job_id=i["job_id"], candidate_id=i["candidate_id"], business_key=i["business_key"],
        field=i["field"], issue_type=i["issue_type"], reason=i["reason"],
        incoming_value=i["incoming_value"], target_value=i["target_value"], match_basis=i["match_basis"],
        options=json.loads(i["options"]), affected=json.loads(i["affected"]),
        status=i["status"], version=i["version"],
        resolution=json.loads(i["resolution"]) if i["resolution"] else None)


@router.get("/jobs/{job_id}/target-reviews", response_model=list[TargetReviewIssueOut])
def get_target_reviews(request: Request, job_id: str, status: str = "open") -> list[TargetReviewIssueOut]:
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    return [_target_issue_out(i) for i in ctx.db.get_target_review_issues(job_id, status=status)]


@router.post("/jobs/{job_id}/target-reviews/{issue_id}/decision", response_model=TargetDecisionOut)
async def submit_target_decision(request: Request, job_id: str, issue_id: str,
                                 body: TargetDecisionIn) -> TargetDecisionOut:
    """Persist a target-reconciliation decision (keep_existing = keep the CURRENT TARGET value /
    use_incoming / exclude) AND (if it clears the last open target issue) enqueue RESUME_TARGET_REVIEW
    atomically. The worker recomputes reconciliation with the overlay; the request never writes to
    the target. keep_existing is recorded as an explicit resolution (which value wins), audited, and
    carried into the version logic (an unchanged effective record creates no new version)."""
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    issue = ctx.db.get_target_review_issue(issue_id)
    if not issue or issue["job_id"] != job_id:
        raise HTTPException(status_code=404, detail="Target review issue not found for this job.")

    allowed = json.loads(issue["options"])
    if body.action not in allowed:
        raise HTTPException(status_code=400,
                            detail=f"Action '{body.action}' not permitted for this issue (allowed: {allowed}).")

    winner = {"keep_existing": issue["target_value"], "use_incoming": issue["incoming_value"]}.get(body.action)
    resolution = {"action": body.action, "field": issue["field"], "reason": body.reason,
                  "note": body.note, "actor": "human",
                  "winning_value": winner,
                  "winning_side": {"keep_existing": "target", "use_incoming": "incoming"}.get(body.action)}
    outcome, enqueued = ctx.db.resolve_target_issue_and_enqueue(
        job_id, issue_id, expected_version=body.version, resolution=resolution)
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="Target review issue not found.")
    if outcome == "stale":
        cur = ctx.db.get_target_review_issue(issue_id)
        raise HTTPException(status_code=409, detail={
            "message": "Stale or conflicting target decision; reload and retry.",
            "current_version": cur["version"] if cur else None,
            "current_status": cur["status"] if cur else None})

    open_remaining = len(ctx.db.get_target_review_issues(job_id, status="open"))
    return TargetDecisionOut(
        issue=_target_issue_out(ctx.db.get_target_review_issue(issue_id)), outcome=outcome,
        resume_triggered=enqueued, resume_status="queued" if enqueued else None,
        open_target_issues_remaining=open_remaining)


def _safe_json_or_str(val):
    """Parse a JSON string if it looks like JSON; otherwise return the raw string."""
    if val is None:
        return None
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, ValueError):
            return val
    return val


def _audit_category(event_type: str | None, actor: str | None) -> str:
    """Map an audit event to a UI filter bucket (ingestion|mapping|preparation|human|target|delivery|error)."""
    et = event_type or ""
    if actor == "human":
        return "human"
    if et.endswith("_failed") or et in ("model_error", "ingest_checksum_mismatch"):
        return "error"
    # M3B: delivery-specific audit events
    if et.startswith("delivery") or et.startswith("rollback") or et.startswith("stale_") \
            or et in ("delivery_plan_created", "migration_complete"):
        return "delivery"
    if et.startswith("target") or et.startswith("employee") or "reconcil" in et:
        return "target"
    if et in ("queued", "ingested", "file_parsed") or et.startswith("ingest"):
        return "ingestion"
    if "preparation" in et or et in ("records_prepared", "record_review_prepared"):
        return "preparation"
    return "mapping"


@router.get("/jobs/{job_id}/audit", response_model=list[AuditOut])
def get_audit(request: Request, job_id: str) -> list[AuditOut]:
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    out = []
    for a in ctx.db.get_audit(job_id):
        out.append(AuditOut(
            id=a["id"], event_type=a["event_type"],
            category=_audit_category(a["event_type"], a["actor"]),
            actor=a["actor"], issue_id=a["issue_id"],
            work_item_id=a.get("work_item_id"),
            source_ref=_safe_json_or_str(a.get("source_ref")),
            before=json.loads(a["before"]) if a["before"] else None,
            after=json.loads(a["after"]) if a["after"] else None,
            reason=a["reason"], schema_version=a["schema_version"], policy_version=a["policy_version"],
            model_version=a["model_version"], ts=a["ts"],
        ))
    return out


# ===================== M3B: delivery, retry, rollback ================================

def _op_out(op: dict) -> DeliveryOperationOut:
    return DeliveryOperationOut(
        id=op["id"], job_id=op["job_id"], candidate_id=op["candidate_id"],
        employee_id=op.get("employee_id"), op_type=op["op_type"],
        payload=json.loads(op["payload"]) if isinstance(op.get("payload"), str) else op.get("payload"),
        expected_target_revision=op.get("expected_target_revision"),
        target_record_id=op.get("target_record_id"),
        before_snapshot=json.loads(op["before_snapshot"]) if isinstance(op.get("before_snapshot"), str) else op.get("before_snapshot"),
        desired_version_id=op.get("desired_version_id"),
        idempotency_key=op["idempotency_key"],
        status=op["status"], attempt_count=op.get("attempt_count") or 0,
        last_error=op.get("last_error"),
        target_revision_after=op.get("target_revision_after"),
        target_request_id=op.get("target_request_id"),
        created_at=op["created_at"], updated_at=op["updated_at"])


def _attempt_out(a: dict) -> DeliveryAttemptOut:
    rm = a.get("response_meta")
    return DeliveryAttemptOut(
        id=a["id"], operation_id=a["operation_id"], attempt_no=a["attempt_no"],
        action=a["action"], started_at=a["started_at"],
        completed_at=a.get("completed_at"), http_status=a.get("http_status"),
        retryable=bool(a["retryable"]) if a.get("retryable") is not None else None,
        error_category=a.get("error_category"), retry_after=a.get("retry_after"),
        target_request_id=a.get("target_request_id"),
        response_meta=json.loads(rm) if isinstance(rm, str) else rm,
        result=a["result"], created_at=a["created_at"])


@router.get("/jobs/{job_id}/delivery", response_model=DeliverySummaryOut)
def get_delivery(request: Request, job_id: str) -> DeliverySummaryOut:
    ctx = _ctx(request)
    job = _job_or_404(ctx, job_id)
    ops = ctx.db.get_delivery_operations(job_id)
    counts = build_delivery_summary(ctx.db, job_id)
    return DeliverySummaryOut(
        status=job.get("status", ""),
        counts=counts,
        operations=[_op_out(op) for op in ops])


@router.get("/jobs/{job_id}/delivery/operations", response_model=list[DeliveryOperationOut])
def get_delivery_operations(request: Request, job_id: str, status: str | None = None) -> list[DeliveryOperationOut]:
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    ops = ctx.db.get_delivery_operations(job_id, status=status)
    return [_op_out(op) for op in ops]


@router.get("/jobs/{job_id}/delivery/operations/{op_id}", response_model=DeliveryOperationOut)
def get_delivery_operation(request: Request, job_id: str, op_id: str) -> DeliveryOperationOut:
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    op = ctx.db.get_delivery_operation(op_id)
    if not op or op["job_id"] != job_id:
        raise HTTPException(status_code=404, detail="Delivery operation not found.")
    return _op_out(op)


@router.get("/jobs/{job_id}/delivery/operations/{op_id}/attempts", response_model=list[DeliveryAttemptOut])
def get_delivery_attempts(request: Request, job_id: str, op_id: str) -> list[DeliveryAttemptOut]:
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    op = ctx.db.get_delivery_operation(op_id)
    if not op or op["job_id"] != job_id:
        raise HTTPException(status_code=404, detail="Delivery operation not found for this job.")
    return [_attempt_out(a) for a in ctx.db.get_delivery_attempts(op_id)]


@router.post("/jobs/{job_id}/deliver", response_model=JobOut)
async def start_delivery(request: Request, job_id: str) -> JobOut:
    """Admin/recovery: explicitly trigger delivery planning + execution for a reconciled job."""
    ctx = _ctx(request)
    job = _job_or_404(ctx, job_id)
    allowed = {"reconciliation_complete", "stale_target_review_required",
               "delivery_partial_failure", "error"}
    if job["status"] not in allowed:
        raise HTTPException(status_code=400, detail=f"Job status '{job['status']}' does not allow delivery; "
                            f"must be one of {allowed}.")
    eff = ctx.effective_schema_for_job(job_id)
    plan = plan_delivery(ctx.db, job_id, eff)
    if plan["blocked"]:
        raise HTTPException(status_code=400, detail="Delivery blocked: unresolved review issues remain.")
    # Enqueue only operations that are actually claimable (PLANNED). Existing SUCCEEDED / terminalized
    # (SKIPPED_*) / FAILED ops are not re-run. When nothing is claimable, finalize from the current
    # operation states rather than hardcoding a status (may be migration_complete, delivery_partial_
    # failure, etc.) so a re-deliver after stale terminalization does not hang in 'delivering'.
    ops = ctx.db.get_delivery_operations(job_id, status="PLANNED")
    if not ops:
        ctx.db.set_delivery_summary(job_id, build_delivery_summary(ctx.db, job_id))
        final = finalize_delivery_status(ctx.db, job_id)
        ctx.db.set_job_stage(job_id, status=final, stage=final)
        return _job_out(ctx, job_id)
    for op in ops:
        ctx.db.enqueue_work(job_id=job_id, kind="DELIVER_OP",
                            idempotency_key=deliver_work_key(ctx.db, op["id"]),
                            max_attempts=ctx.settings.target_max_attempts,
                            payload={"operation_id": op["id"]})
    ctx.db.set_job_stage(job_id, status="delivering", stage="delivering")
    return _job_out(ctx, job_id)


@router.post("/jobs/{job_id}/delivery/retry", response_model=RetryOut)
async def retry_delivery(request: Request, job_id: str, body: RetryIn) -> RetryOut:
    """Manually retry a delivery operation as a NEW attempt of the SAME logical op (same/refreshed
    idempotency key — never a duplicate employee).

    M3B.2 policy — decided from PERSISTED ATTEMPT EVIDENCE, not a partial category list:
      Allowed:
        - status RETRYABLE; or
        - status FAILED whose LAST attempt is retryable=true (auto-retry budget exhausted on a
          transient error such as 500/429/transport).
      Rejected (clear 400):
        - STALE_TARGET (must go through refetch -> reconcile -> human review -> full replan);
        - any FAILED op whose last attempt is retryable=false — validation 4xx, auth 401/403,
          identity conflicts (already_exists / email_in_use), or any other non-retryable status;
        - any other operation status.
    """
    ctx = _ctx(request)
    _job_or_404(ctx, job_id)
    op = ctx.db.get_delivery_operation(body.operation_id)
    if not op or op["job_id"] != job_id:
        raise HTTPException(status_code=404, detail="Delivery operation not found.")

    status = op["status"]
    if status == "STALE_TARGET":
        raise HTTPException(status_code=400, detail=(
            "STALE_TARGET cannot be retried through generic retry. It must proceed via "
            "refetch -> reconcile -> human review (if needed) -> full replan."))
    if status not in ("RETRYABLE", "FAILED"):
        raise HTTPException(status_code=400,
                            detail=f"Cannot retry operation in status '{status}'.")
    if status == "FAILED":
        attempts = ctx.db.get_delivery_attempts(op["id"])
        last = attempts[-1] if attempts else None
        if not (last and last.get("retryable")):
            last_cat = (last.get("error_category") if last else None) or "unknown"
            raise HTTPException(status_code=400, detail=(
                f"Cannot retry: the last delivery attempt was non-retryable (category: {last_cat}). "
                "Fix the underlying data/credentials or reconcile the target first."))

    # Allowed → start a fresh work generation. Release the operation execution fence so the new work
    # item can claim it, reset the op to a claimable state, and enqueue a generation-unique work item.
    ctx.db.release_operation_work(op["id"])
    ctx.db.update_delivery_operation(op["id"], status="PLANNED", last_error=None)
    gen = ctx.db.delivery_attempt_count(op["id"])
    enqueued = ctx.db.enqueue_work(job_id=job_id, kind="DELIVER_OP",
                                   idempotency_key=f"work:deliver:{op['id']}:retry:{gen}",
                                   max_attempts=ctx.settings.target_max_attempts,
                                   payload={"operation_id": op["id"]})
    ctx.db.add_audit(job_id, event_type="delivery_retry_manual", actor="human",
                     source_ref=op["id"], reason=body.reason)
    if enqueued and ctx.db.get_job(job_id).get("status") not in ("delivering",):
        ctx.db.set_job_stage(job_id, status="delivering", stage="delivering")
    return RetryOut(operation_id=op["id"], enqueued=enqueued, new_status="PLANNED")


@router.post("/jobs/{job_id}/rollback", response_model=RollbackOut)
async def start_rollback(request: Request, job_id: str) -> RollbackOut:
    """Plan and enqueue rollback for all SUCCEEDED delivery operations."""
    ctx = _ctx(request)
    job = _job_or_404(ctx, job_id)
    result = plan_rollback(ctx.db, job_id)
    if result["rollback_planned"] == 0:
        raise HTTPException(status_code=400, detail="No succeeded operations to roll back.")
    # Enqueue rollback work items
    ops = ctx.db.get_delivery_operations(job_id, status="ROLLBACK_PLANNED")
    for op in ops:
        ctx.db.enqueue_work(job_id=job_id, kind="ROLLBACK_OP",
                            idempotency_key=f"work:rollback:{op['id']}",
                            max_attempts=ctx.settings.target_max_attempts,
                            payload={"operation_id": op["id"]})
    ctx.db.set_job_stage(job_id, status="rollback_in_progress", stage="rollback_in_progress")
    return RollbackOut(planned=result["rollback_planned"], total_succeeded=result["total_succeeded"],
                       job_status="rollback_in_progress")
