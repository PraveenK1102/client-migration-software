"""Explicit API request/response models."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class SchemaFieldOut(BaseModel):
    name: str
    path: str
    label: str
    description: str
    value_type: str
    required: bool
    nullable: bool
    allowed_values: list[str] | None = None
    group: str | None = None
    kind: str = "scalar"               # scalar | collection_item | custom
    collection: str | None = None
    custom_definition_id: str | None = None
    multi_value: bool = False


class SchemaCollectionOut(BaseModel):
    key: str
    label: str
    description: str
    item_identity: list[str]
    table_aliases: list[str]
    indexed_column: dict | None = None
    multi_value_column: dict | None = None
    fields: list[SchemaFieldOut]


class SchemaOut(BaseModel):
    version: str
    title: str
    description: str
    tenant_id: str | None = None
    groups: list[dict] = []
    fields: list[SchemaFieldOut]                 # CORE scalar fields
    collections: list[SchemaCollectionOut] = []  # structured one-to-many entities
    custom_attributes: dict = {}                 # the custom-attribute contract
    custom_fields: list[SchemaFieldOut] = []     # the tenant's custom-field definitions (effective schema)
    target_paths: list[str] = []
    date_role_group: list[str]
    boundary: dict = {}


class JobOut(BaseModel):
    id: str
    thread_id: str
    schema_version: str
    tenant_id: str = "default"
    provider: str
    model_id: str
    adapter_kind: str
    status: str
    stage: str
    error: str | None = None
    summary: dict | None = None
    prep_summary: dict | None = None
    recon_summary: dict | None = None
    delivery_summary: dict | None = None
    created_at: str
    updated_at: str
    counts: dict[str, int] = {}
    # M3F §A: compact, backwards-compatible summary so the migration LIST can render a human-readable
    # card without issuing per-job follow-up requests (source filenames, employee-record count, and the
    # number of open human decisions across every review family).
    source_filenames: list[str] = []
    row_count: int | None = None
    open_reviews: int = 0


class ReadinessOut(BaseModel):
    status: str
    provider: str
    model_id: str
    adapter_kind: str
    configured: bool                 # a key is present and nonempty (NOT proof it authenticates)
    category: str                    # configured | configuration_missing | ..._inherited_empty_override
    deterministic_available: bool    # rules-only mapping + preparation work without a key
    requires_key: bool
    message: str
    env_file: str
    env_file_exists: bool


class ColumnProfileOut(BaseModel):
    profile_id: str
    table_id: str
    col_index: int
    header: str
    non_empty_count: int
    missing_count: int
    distinct_count: int
    observed_types: dict[str, int]
    format_indicators: dict[str, Any]
    samples: list[str]


class TableOut(BaseModel):
    table_id: str
    file_id: str
    original_filename: str
    sheet_name: str | None
    headers: list[str]
    n_rows: int
    table_role: str = "employee"        # employee | child:<collection>
    profiles: list[ColumnProfileOut]


class ParsingIssueOut(BaseModel):
    table_id: str | None
    kind: str
    detail: str
    severity: str


class ProfilesOut(BaseModel):
    job_id: str
    tables: list[TableOut]
    parsing_issues: list[ParsingIssueOut]


class MappingRowOut(BaseModel):
    profile_id: str
    table_id: str
    source_header: str
    target_field: str | None
    status: str            # auto_accepted | approved | corrected | rejected | unmapped | ignored | needs_review | proposal
    actor: str
    method: str | None = None            # rule | model | human | none | unresolved (decision origin)
    reason: str | None = None
    destination_kind: str = "UNMAPPED"   # CORE_FIELD | COLLECTION_FIELD | CUSTOM_FIELD | UNMAPPED | NEEDS_REVIEW | IGNORED | PROPOSAL
    path_meta: dict | None = None
    custom_definition_id: str | None = None
    note: str | None = None
    proposal_id: str | None = None


class MappingsOut(BaseModel):
    job_id: str
    accepted: list[MappingRowOut]
    unresolved: list[MappingRowOut]
    ignored: list[MappingRowOut] = []
    proposals: list[MappingRowOut] = []
    counts: dict[str, int] = {}


class ReviewIssueOut(BaseModel):
    id: str
    job_id: str
    profile_id: str
    table_id: str
    source_header: str
    issue_type: str
    proposed_target_field: str | None
    candidate_target_fields: list[str]
    evidence_summary: dict
    affected_non_empty_rows: int
    status: str
    version: int
    resolution: dict | None = None


class DecisionIn(BaseModel):
    version: int
    action: Literal["approve", "correct", "reject", "ignore"]
    corrected_target: str | None = None
    reason: str | None = None
    note: str | None = None            # optional human decision note (persisted + audited)


class DecisionOut(BaseModel):
    issue: ReviewIssueOut
    outcome: str                 # resolved | noop | stale
    resume_triggered: bool
    resume_status: str | None = None
    open_issues_remaining: int


class RecordIssueOut(BaseModel):
    id: str
    job_id: str
    candidate_key: str | None
    field: str | None
    issue_type: str
    reason: str
    options: list
    affected: dict
    scope: dict | None
    status: str
    version: int
    resolution: dict | None = None


class RecordDecisionIn(BaseModel):
    version: int
    action: Literal["select", "correct", "confirm_convention", "confirm_century_pivot",
                    "map_values", "null", "exclude", "drop_item"]
    value: str | None = None            # for select/correct (select: an option value or a variant_key)
    convention: str | None = None       # for confirm_convention (DMY|MDY)
    value_map: dict | None = None       # for map_values (column-scoped unknown_enum): {source_value: target_enum}
    pivot: int | None = None            # for confirm_century_pivot (two-digit-year cutoff year, e.g. 2049)
    scope: dict | None = None           # e.g. {"table_id": "..."} for a scoped date convention;
                                        # {"collection","identity_key","item_field"} for collection items
    candidate_id: str | None = None     # for exclude of a specific keyless candidate
    reason: str | None = None
    note: str | None = None             # optional human decision note (persisted + audited)


class RecordDecisionOut(BaseModel):
    issue: RecordIssueOut
    outcome: str
    resume_triggered: bool
    resume_status: str | None = None
    open_record_issues_remaining: int


class CandidateOut(BaseModel):
    id: str
    business_key: str | None
    eligibility: str
    exclude_reason: str | None
    issue_ids: list[str]
    source_refs: list
    record: dict                              # core scalar fields: {field: {value,status,rule,reason,provenance}}
    collections: dict[str, list] = {}         # {collection: [ {identity_key, status, fields:{...}, sources:[...]} ]}
    custom_attributes: list = []              # [ {definition_id,key,label,type,value,status,rule,provenance} ]


class PreparedDatasetOut(BaseModel):
    job_id: str
    schema_version: str
    tenant_id: str = "default"
    ready_for_target: int
    employees: list[dict]               # validated eligible target objects only (scalars + collections + custom_attributes)
    excluded: list[dict]                # disposition view (separate from the payload)
    provenance_versions: dict


class StagedCellOut(BaseModel):
    col_index: int
    header: str
    value: str | None


class StagedRowOut(BaseModel):
    row_number: int
    cells: list[StagedCellOut]


class StagedRowsOut(BaseModel):
    job_id: str
    table_id: str
    original_filename: str
    sheet_name: str | None
    headers: list[str]
    total: int
    offset: int
    limit: int
    rows: list[StagedRowOut]


class SourceRowContextOut(BaseModel):
    """Exact persisted source row (+ neighbours) for a provenance deep-link. File -> Sheet -> Row -> Header."""
    job_id: str
    table_id: str
    file_id: str
    original_filename: str
    sheet_name: str | None
    headers: list[str]
    row_number: int
    highlight_header: str | None = None
    highlight_col_index: int | None = None
    row: StagedRowOut | None = None
    context: list[StagedRowOut] = []
    total_rows: int


class AuditOut(BaseModel):
    id: str
    event_type: str
    category: str | None = None       # ingestion|mapping|preparation|human|target|error (UI filter)
    actor: str
    issue_id: str | None
    work_item_id: str | None = None
    source_ref: Any | None
    before: Any | None
    after: Any | None
    reason: str | None
    schema_version: str | None
    policy_version: str | None
    model_version: str | None
    ts: str


# ===================== M3A: durable work + files =====================
class SourceFileOut(BaseModel):
    id: str
    job_id: str
    original_filename: str
    content_type: str | None
    size_bytes: int
    sha256: str | None
    storage_status: str | None
    parse_status: str | None          # uploaded | queued | processing | parsed | failed
    parse_error: str | None
    created_at: str


class WorkItemOut(BaseModel):
    id: str
    job_id: str
    source_file_id: str | None
    kind: str
    status: str                        # pending|processing|retryable|succeeded|failed|cancelled
    attempt: int
    max_attempts: int
    worker_id: str | None
    last_error_category: str | None
    last_error: str | None
    idempotency_key: str
    created_at: str
    updated_at: str


# ===================== M3A: target reconciliation =====================
class TargetSnapshotOut(BaseModel):
    candidate_id: str
    business_key: str | None
    target_record_id: str | None
    match_basis: str
    target_revision: int | None
    target_payload: dict | None
    fetched_at: str


class ReconciliationRowOut(BaseModel):
    candidate_id: str
    business_key: str | None
    outcome: str                       # READY_CREATE|READY_UPDATE|NO_CHANGE|REVIEW_REQUIRED|EXCLUDED
    target_record_id: str | None
    target_revision: int | None
    match_basis: str
    diff: dict | None = None


class ReconciliationOut(BaseModel):
    job_id: str
    status: str
    summary: dict | None = None
    counts: dict[str, int] = {}
    results: list[ReconciliationRowOut] = []


class TargetReviewIssueOut(BaseModel):
    id: str
    job_id: str
    candidate_id: str
    business_key: str | None
    field: str | None
    issue_type: str                    # value_conflict | email_owned_by_other | id_email_mismatch | collection_item_conflict
    reason: str
    incoming_value: str | None
    target_value: str | None
    match_basis: str
    options: list
    affected: dict
    status: str
    version: int
    resolution: dict | None = None


class TargetDecisionIn(BaseModel):
    version: int
    action: Literal["keep_existing", "use_incoming", "exclude"]
    reason: str | None = None
    note: str | None = None            # optional human decision note (persisted + audited)


class TargetDecisionOut(BaseModel):
    issue: TargetReviewIssueOut
    outcome: str                       # resolved | noop | stale
    resume_triggered: bool
    resume_status: str | None = None
    open_target_issues_remaining: int


# ===================== M3A.1: immutable employee versions =====================
class EmployeeVersionOut(BaseModel):
    id: str
    job_id: str
    candidate_id: str
    business_key: str | None
    version_no: int
    parent_version_id: str | None
    origin: str                        # existing_target | migration | human | rollback
    snapshot: dict                     # scalars + collections + custom_attributes
    record_hash: str
    change_reason: str | None
    decision_note: str | None
    decision_id: str | None
    target_revision: int | None
    restores_version_id: str | None
    field_changes: list | None
    created_by: str
    created_at: str
    is_current: bool = False


class VersionFieldDiffOut(BaseModel):
    field: str
    a: Any | None
    b: Any | None
    changed: bool
    kind: str                          # unchanged | changed | added | cleared


class VersionCollectionItemDiffOut(BaseModel):
    collection: str
    identity_key: str
    kind: str                          # unchanged | added | removed | changed
    a: dict | None = None
    b: dict | None = None
    changed_fields: list[str] = []


class VersionCompareOut(BaseModel):
    job_id: str
    candidate_id: str
    business_key: str | None
    a_version: int
    b_version: int
    fields: list[VersionFieldDiffOut]                 # core scalar fields
    custom_attributes: list[VersionFieldDiffOut] = [] # field = custom key
    collections: list[VersionCollectionItemDiffOut] = []
    changed_fields: list[str]                         # scalar field names that changed
    changed_collections: list[str] = []
    changed_custom_attributes: list[str] = []


# ===================== M3A.2: tenants + custom fields + proposals =====================
class TenantOut(BaseModel):
    id: str
    name: str
    created_at: str
    custom_field_count: int = 0


class CustomFieldDefinitionOut(BaseModel):
    id: str
    tenant_id: str
    key: str
    path: str
    label: str
    type: str
    required: bool
    options: list | None = None
    multi_value: bool
    description: str | None = None
    aliases: list[str] = []
    origin: str
    origin_proposal_id: str | None = None
    origin_job_id: str | None = None
    created_by: str
    created_at: str


class CustomFieldDefinitionIn(BaseModel):
    key: str
    label: str
    type: str
    required: bool = False
    options: list[str] | None = None
    multi_value: bool = False
    description: str | None = None
    aliases: list[str] | None = None
    job_id: str | None = None          # optional: audit the creation under this job


class CustomFieldProposalOut(BaseModel):
    id: str
    job_id: str
    tenant_id: str
    profile_id: str
    table_id: str
    source_header: str
    origin: str                        # no_provider | model_unmapped
    suggestion: dict                   # {key,label,type,options,multi_value,required,path}
    observed_values: list
    non_empty_count: int
    status: str                        # open | approved | mapped_existing | mapped_target | ignored | superseded
    version: int
    resolution: dict | None = None
    definition_id: str | None = None
    created_at: str
    updated_at: str


class ProposalDecisionIn(BaseModel):
    version: int
    action: Literal["approve", "map_existing", "map_target", "ignore"]
    # approve (create tenant custom field) — editable suggestion:
    key: str | None = None
    label: str | None = None
    type: str | None = None
    options: list[str] | None = None
    required: bool | None = None
    multi_value: bool | None = None
    description: str | None = None
    # map_existing:
    definition_id: str | None = None
    # map_target:
    target_path: str | None = None
    reason: str | None = None
    note: str | None = None


class ProposalDecisionOut(BaseModel):
    proposal: CustomFieldProposalOut
    outcome: str                       # resolved | noop | stale
    definition: CustomFieldDefinitionOut | None = None
    remap_triggered: bool
    remap_status: str | None = None
    open_proposals_remaining: int


# ===================== M3B delivery models =============================================

class DeliveryOperationOut(BaseModel):
    id: str
    job_id: str
    candidate_id: str
    employee_id: str | None
    op_type: str
    payload: Any
    expected_target_revision: int | None
    target_record_id: str | None
    before_snapshot: Any | None
    desired_version_id: str | None
    idempotency_key: str
    status: str
    attempt_count: int
    last_error: str | None
    target_revision_after: int | None
    target_request_id: str | None
    created_at: str
    updated_at: str


class DeliveryAttemptOut(BaseModel):
    id: str
    operation_id: str
    attempt_no: int
    action: str
    started_at: str
    completed_at: str | None
    http_status: int | None
    retryable: bool | None
    error_category: str | None
    retry_after: float | None
    target_request_id: str | None
    response_meta: Any | None
    result: str
    created_at: str


class DeliverySummaryOut(BaseModel):
    status: str
    counts: dict
    operations: list[DeliveryOperationOut]


class RetryIn(BaseModel):
    operation_id: str
    reason: str | None = None


class RetryOut(BaseModel):
    operation_id: str
    enqueued: bool
    new_status: str


class RollbackOut(BaseModel):
    planned: int
    total_succeeded: int
    job_status: str


# --- M3I: safe source-file removal + migration delete ------------------------------------------
class RemoveFilesIn(BaseModel):
    file_ids: list[str]
    note: str | None = None


class RemoveFilesOut(BaseModel):
    job_id: str
    removed_file_ids: list[str]
    removed_filenames: list[str]
    remaining_files: int
    rebuild_enqueued: bool
    blobs_deleted: int
    blob_cleanup_pending: int = 0
    preserved_custom_field_definitions: int = 0


class DeleteMigrationOut(BaseModel):
    job_id: str
    deleted: bool
    blobs_deleted: int
    blob_cleanup_pending: int = 0
    org_deleted: str | None = None       # tenant_id removed when this was the org's last migration


# --- Migration-admin employee editing / deletion --------------------------------------------------
class EditEmployeeIn(BaseModel):
    fields: dict[str, Any]                 # {field_name: new_value} — scalar core or scalar org fields
    note: str | None = None               # optional reason, persisted on the new version + audit


class EditEmployeeOut(BaseModel):
    candidate_id: str
    changed: bool                          # False when the edit was a no-op (values unchanged)
    version_no: int | None = None
    changed_fields: list[str] = []
    pushed_to_target: bool = False         # True when an UPDATE was enqueued to the live target
    delivery_op_id: str | None = None


class DeleteEmployeeOut(BaseModel):
    candidate_id: str
    delete_requested: bool
    delivery_op_id: str | None = None
