// Mirrors the backend API response models (backend/app/models.py).

export interface SchemaField {
  name: string;
  path: string;
  label: string;
  description: string;
  value_type: string;
  required: boolean;
  nullable: boolean;
  allowed_values: string[] | null;
  group: string | null;
  kind: "scalar" | "collection_item" | "custom" | string;
  collection: string | null;
  custom_definition_id: string | null;
  multi_value: boolean;
}

export interface SchemaCollection {
  key: string;
  label: string;
  description: string;
  item_identity: string[];
  table_aliases: string[];
  indexed_column: { aliases: string[]; field: string } | null;
  multi_value_column: { aliases: string[]; field: string; delimiters: string[] } | null;
  fields: SchemaField[];
}

export interface TargetSchema {
  version: string;
  title: string;
  description: string;
  tenant_id: string | null;
  groups: { key: string; label: string }[];
  fields: SchemaField[];
  collections: SchemaCollection[];
  custom_attributes: Record<string, any>;
  custom_fields: SchemaField[];
  target_paths: string[];
  date_role_group: string[];
  boundary: Record<string, string>;
}

export interface Job {
  id: string;
  thread_id: string;
  schema_version: string;
  tenant_id: string;
  provider: string;
  model_id: string;
  adapter_kind: string;
  status: string;
  stage: string;
  error: string | null;
  summary: JobSummary | null;
  prep_summary: Record<string, any> | null;
  recon_summary: Record<string, any> | null;
  delivery_summary: Record<string, any> | null;
  created_at: string;
  updated_at: string;
  counts: Record<string, number>;
  // M3F §A: compact migration-list summary (present on GET /api/jobs and /api/jobs/{id}).
  source_filenames?: string[];
  row_count?: number | null;
  open_reviews?: number;
}

export interface JobSummary {
  result: string;
  schema_version: string;
  policy_version: string;
  model_id: string;
  adapter_kind: string;
  counts: Record<string, number>;
  accepted_mappings: { source_header: string; target_field: string; status: string; actor: string }[];
  uncovered_required_fields: string[];
}

export interface ColumnProfile {
  profile_id: string;
  table_id: string;
  col_index: number;
  header: string;
  non_empty_count: number;
  missing_count: number;
  distinct_count: number;
  observed_types: Record<string, number>;
  format_indicators: Record<string, unknown>;
  samples: string[];
}

export interface SourceTable {
  table_id: string;
  file_id: string;
  original_filename: string;
  sheet_name: string | null;
  headers: string[];
  n_rows: number;
  table_role: string;
  profiles: ColumnProfile[];
}

export interface ParsingIssue {
  table_id: string | null;
  kind: string;
  detail: string;
  severity: string;
}

export interface ProfilesResponse {
  job_id: string;
  tables: SourceTable[];
  parsing_issues: ParsingIssue[];
}

export type DestinationKind =
  | "CORE_FIELD" | "COLLECTION_FIELD" | "CUSTOM_FIELD" | "UNMAPPED" | "NEEDS_REVIEW" | "IGNORED" | "PROPOSAL" | string;

export interface MappingRow {
  profile_id: string;
  table_id: string;
  source_header: string;
  target_field: string | null;
  status: string;
  actor: string;
  method: string | null;
  reason: string | null;
  destination_kind: DestinationKind;
  path_meta: Record<string, any> | null;
  custom_definition_id: string | null;
  note: string | null;
  proposal_id: string | null;
}

export interface MappingsResponse {
  job_id: string;
  accepted: MappingRow[];
  unresolved: MappingRow[];
  ignored: MappingRow[];
  proposals: MappingRow[];
  counts: Record<string, number>;
}

export interface ReviewIssue {
  id: string;
  job_id: string;
  profile_id: string;
  table_id: string;
  source_header: string;
  issue_type: string;
  proposed_target_field: string | null;
  candidate_target_fields: string[];
  evidence_summary: Record<string, any>;
  affected_non_empty_rows: number;
  status: string;
  version: number;
  resolution: Record<string, any> | null;
}

export interface AuditEvent {
  id: string;
  event_type: string;
  category: string | null;
  actor: string;
  issue_id: string | null;
  work_item_id: string | null;
  source_ref: Record<string, any> | null;
  before: any;
  after: any;
  reason: string | null;
  schema_version: string | null;
  policy_version: string | null;
  model_version: string | null;
  ts: string;
}

export interface Health {
  status: string;
  provider: string;
  model_id: string;
  adapter_kind: string;
  configured: boolean;
  category: string;
  deterministic_available: boolean;
  requires_key: boolean;
  message: string;
  env_file: string;
  env_file_exists: boolean;
  adapter_available: boolean;
  adapter_error: string | null;
  schema_version?: string;
  default_tenant_id?: string;
}

export type DecisionAction = "approve" | "correct" | "reject" | "ignore";

export interface RecordIssue {
  id: string;
  job_id: string;
  candidate_key: string | null;
  field: string | null;
  issue_type: string;
  reason: string;
  options: any[];
  affected: Record<string, any>;
  scope: Record<string, any> | null;
  status: string;
  version: number;
  resolution: Record<string, any> | null;
}

export interface Provenance {
  table_id: string;
  row_number: number;
  original_filename: string;
  sheet_name: string | null;
  header: string | null;
  raw: any;
  col_index?: number | null;
  part?: number;
  raw_cell?: string | null;
}

export interface FieldValue {
  value: any;
  status: string;
  rule: string | null;
  reason: string | null;
  provenance: Provenance[];
}

export interface CollectionItem {
  identity_key: string;
  status: string;
  reason: string | null;
  duplicates_collapsed: number;
  fields: Record<string, FieldValue>;
  sources: { table_id: string; row_number: number; original_filename: string; sheet_name: string | null }[];
  variants: any[];
}

export interface CustomAttributeValue extends FieldValue {
  definition_id: string | null;
  key: string;
  label: string;
  type: string;
}

export interface Candidate {
  id: string;
  business_key: string | null;
  eligibility: string;
  exclude_reason: string | null;
  issue_ids: string[];
  source_refs: any[];
  record: Record<string, FieldValue>;
  collections: Record<string, CollectionItem[]>;
  custom_attributes: CustomAttributeValue[];
}

export interface PreparedDataset {
  job_id: string;
  schema_version: string;
  tenant_id: string;
  ready_for_target: number;
  employees: Record<string, any>[];
  excluded: Record<string, any>[];
  provenance_versions: Record<string, any>;
}

export type RecordAction =
  | "select" | "correct" | "confirm_convention" | "confirm_century_pivot" | "map_values"
  | "null" | "exclude" | "drop_item";

export interface DeliveryMetrics {
  operations: number;
  by_status: Record<string, number>;
  by_type: Record<string, number>;
  total_attempts: number;
}

export interface JobMetrics {
  job_id: string;
  tenant_id: string;
  status: string;
  stage: string;
  mapping: Record<string, number>;
  intelligence: Record<string, number | string>;
  preparation: Record<string, number>;
  reconciliation: Record<string, number>;
  delivery: DeliveryMetrics;
  human_decisions: number;
  model?: ModelMetrics;
  timings?: StageTimings;
}

/** M3F §I: per-stage COMPUTE time (ms) + separate human-review wait, from persisted signals. */
export interface StageTimings {
  stages: Record<string, number>; // parse_stage | mapping | preparation | reconcile | delivery
  llm_mapping_ms: number;
  llm_transforms_ms: number;
  total_compute_ms: number;
  human_review_wait_ms: number | null;
  human_review_wait_measured: boolean;
}

/** One sanitized model call (mapping or transform): counts/attempts/latency/tokens, no prompt/PII. */
export interface ModelCallRow {
  kind: string;
  model_id: string;
  adapter_kind: string;
  status: string;
  error_category: string | null;
  attempts: number;
  latency_ms: number;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  n_columns: number | null;
  n_proposals: number | null;
  created_at: string | null;
}

/** Persisted model-call observability (sanitized: counts/attempts/latency/tokens, no prompt/PII). */
export interface ModelMetrics {
  calls: number;
  ok: number;
  errors: number;
  by_kind: Record<string, number>;
  adapter_kinds: string[];
  model_ids?: string[];
  total_attempts: number;
  latency_ms_total: number;
  latency_ms_avg: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  estimated_cost_usd?: number;
  price_input_per_1m?: number;
  price_output_per_1m?: number;
  errors_by_category: Record<string, number>;
  calls_detail?: ModelCallRow[];
}

export interface PortfolioMetrics {
  jobs_total: number;
  jobs_by_status: Record<string, number>;
  mapping: Record<string, number>;
  intelligence: Record<string, number>;
  records: Record<string, number>;
  delivery: DeliveryMetrics;
  human_decisions: number;
}

// ===================== M3A: target reconciliation =====================
export interface SourceFile {
  id: string;
  job_id: string;
  original_filename: string;
  content_type: string | null;
  size_bytes: number;
  sha256: string | null;
  storage_status: string | null;
  parse_status: string | null;
  parse_error: string | null;
  created_at: string;
}

export interface ReconciliationRow {
  candidate_id: string;
  business_key: string | null;
  outcome: string;
  target_record_id: string | null;
  target_revision: number | null;
  match_basis: string;
  diff: Record<string, any> | null;
}

export interface Reconciliation {
  job_id: string;
  status: string;
  summary: Record<string, any> | null;
  counts: Record<string, number>;
  results: ReconciliationRow[];
}

export interface TargetReviewIssue {
  id: string;
  job_id: string;
  candidate_id: string;
  business_key: string | null;
  field: string | null;
  issue_type: string;
  reason: string;
  incoming_value: string | null;
  target_value: string | null;
  match_basis: string;
  options: string[];
  affected: Record<string, any>;
  status: string;
  version: number;
  resolution: Record<string, any> | null;
}

export type TargetAction = "keep_existing" | "use_incoming" | "exclude";

export interface EmployeeVersion {
  id: string;
  job_id: string;
  candidate_id: string;
  business_key: string | null;
  version_no: number;
  parent_version_id: string | null;
  origin: string;
  snapshot: Record<string, any>;
  record_hash: string;
  change_reason: string | null;
  decision_note: string | null;
  decision_id: string | null;
  target_revision: number | null;
  restores_version_id: string | null;
  field_changes: any[] | null;
  created_by: string;
  created_at: string;
  is_current: boolean;
}

export interface VersionFieldDiff {
  field: string;
  a: any;
  b: any;
  changed: boolean;
  kind: string;
}

export interface VersionCollectionItemDiff {
  collection: string;
  identity_key: string;
  kind: string;
  a: Record<string, any> | null;
  b: Record<string, any> | null;
  changed_fields: string[];
}

export interface VersionCompare {
  job_id: string;
  candidate_id: string;
  business_key: string | null;
  a_version: number;
  b_version: number;
  fields: VersionFieldDiff[];
  custom_attributes: VersionFieldDiff[];
  collections: VersionCollectionItemDiff[];
  changed_fields: string[];
  changed_collections: string[];
  changed_custom_attributes: string[];
}

export interface WorkItem {
  id: string;
  job_id: string;
  source_file_id: string | null;
  kind: string;
  status: string;
  attempt: number;
  max_attempts: number;
  worker_id: string | null;
  last_error_category: string | null;
  last_error: string | null;
  idempotency_key: string;
  created_at: string;
  updated_at: string;
}

export interface StagedCell {
  col_index: number;
  header: string;
  value: string | null;
}

export interface StagedRow {
  row_number: number;
  cells: StagedCell[];
}

export interface StagedRows {
  job_id: string;
  table_id: string;
  original_filename: string;
  sheet_name: string | null;
  headers: string[];
  total: number;
  offset: number;
  limit: number;
  rows: StagedRow[];
}

export interface SourceRowContext {
  job_id: string;
  table_id: string;
  file_id: string;
  original_filename: string;
  sheet_name: string | null;
  headers: string[];
  row_number: number;
  highlight_header: string | null;
  highlight_col_index: number | null;
  row: StagedRow | null;
  context: StagedRow[];
  total_rows: number;
}

// ===================== M3A.2: tenants + custom fields + proposals =====================
export interface Tenant {
  id: string;
  name: string;
  created_at: string;
  custom_field_count: number;
}

export interface CustomFieldDefinition {
  id: string;
  tenant_id: string;
  key: string;
  path: string;
  label: string;
  type: string;
  required: boolean;
  options: string[] | null;
  multi_value: boolean;
  description: string | null;
  aliases: string[];
  origin: string;
  origin_proposal_id: string | null;
  origin_job_id: string | null;
  created_by: string;
  created_at: string;
}

export interface CustomFieldProposal {
  id: string;
  job_id: string;
  tenant_id: string;
  profile_id: string;
  table_id: string;
  source_header: string;
  origin: string;
  suggestion: { key: string; label: string; type: string; options: string[] | null; multi_value: boolean; required: boolean; path: string };
  observed_values: string[];
  non_empty_count: number;
  status: string;
  version: number;
  resolution: Record<string, any> | null;
  definition_id: string | null;
  created_at: string;
  updated_at: string;
}

export type ProposalAction = "approve" | "map_existing" | "map_target" | "ignore";

// ===================== M3B: delivery =====================
export interface DeliveryOperation {
  id: string;
  job_id: string;
  candidate_id: string;
  employee_id: string | null;
  op_type: string;                  // CREATE | UPDATE
  payload: any;
  expected_target_revision: number | null;
  target_record_id: string | null;
  before_snapshot: any;
  desired_version_id: string | null;
  idempotency_key: string;
  status: string;                   // PLANNED | IN_PROGRESS | SUCCEEDED | FAILED | RETRYABLE | STALE_TARGET | ROLLBACK_PLANNED | ROLLBACK_IN_PROGRESS | ROLLED_BACK | ROLLBACK_FAILED | NO_CHANGE | EXCLUDED | SKIPPED_NO_CHANGE | SKIPPED_EXCLUDED
  attempt_count: number;
  last_error: string | null;
  target_revision_after: number | null;
  target_request_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface DeliveryAttempt {
  id: string;
  operation_id: string;
  attempt_no: number;
  action: string;
  started_at: string;
  completed_at: string | null;
  http_status: number | null;
  retryable: boolean | null;
  error_category: string | null;
  retry_after: number | null;
  target_request_id: string | null;
  response_meta: any;
  result: string;
  created_at: string;
}

export interface DeliverySummary {
  status: string;
  counts: Record<string, number>;
  operations: DeliveryOperation[];
}

export interface ProposalDecisionBody {
  version: number;
  action: ProposalAction;
  key?: string | null;
  label?: string | null;
  type?: string | null;
  options?: string[] | null;
  required?: boolean | null;
  multi_value?: boolean | null;
  description?: string | null;
  definition_id?: string | null;
  target_path?: string | null;
  reason?: string | null;
  note?: string | null;
}
