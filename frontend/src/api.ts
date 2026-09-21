import type {
  AuditEvent,
  Candidate,
  CustomFieldDefinition,
  CustomFieldProposal,
  DecisionAction,
  DeliveryAttempt,
  DeliverySummary,
  EmployeeVersion,
  Health,
  Job,
  MappingsResponse,
  JobMetrics,
  PortfolioMetrics,
  PreparedDataset,
  ProfilesResponse,
  ProposalDecisionBody,
  Reconciliation,
  RecordAction,
  RecordIssue,
  ReviewIssue,
  SourceFile,
  SourceRowContext,
  StagedRows,
  TargetAction,
  TargetReviewIssue,
  TargetSchema,
  Tenant,
  VersionCompare,
  WorkItem,
} from "./types";

const BASE = "/api";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, init);
  if (!res.ok) {
    let detail: unknown = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? body;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json() as Promise<T>;
}

const json = (body: unknown): RequestInit => ({
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export const api = {
  health: () => req<Health>("/health"),
  schema: (tenantId?: string | null) =>
    req<TargetSchema>(`/schema${tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : ""}`),
  getJobSchema: (id: string) => req<TargetSchema>(`/jobs/${id}/schema`),
  listJobs: () => req<Job[]>("/jobs"),
  getJob: (id: string) => req<Job>(`/jobs/${id}`),
  getProfiles: (id: string) => req<ProfilesResponse>(`/jobs/${id}/profiles`),
  getMappings: (id: string) => req<MappingsResponse>(`/jobs/${id}/mappings`),
  getReviews: (id: string) => req<ReviewIssue[]>(`/jobs/${id}/reviews`),
  getAudit: (id: string) => req<AuditEvent[]>(`/jobs/${id}/audit`),
  getMetrics: () => req<PortfolioMetrics>("/metrics"),
  getJobMetrics: (id: string) => req<JobMetrics>(`/jobs/${id}/metrics`),
  getCandidates: (id: string) => req<Candidate[]>(`/jobs/${id}/candidates`),
  getRecordReviews: (id: string) => req<RecordIssue[]>(`/jobs/${id}/record-reviews`),
  getPreparedDataset: (id: string) => req<PreparedDataset>(`/jobs/${id}/prepared-dataset`),
  startPreparation: (id: string) => req<Job>(`/jobs/${id}/prepare`, { method: "POST" }),
  retryMapping: (id: string) => req<Job>(`/jobs/${id}/retry-mapping`, { method: "POST" }),

  // M3A: files / staged data / target reconciliation
  getFiles: (id: string) => req<SourceFile[]>(`/jobs/${id}/files`),
  getWorkItems: (id: string) => req<WorkItem[]>(`/jobs/${id}/work-items`),
  getStagedRows: (id: string, tableId: string, offset = 0, limit = 50) =>
    req<StagedRows>(`/jobs/${id}/tables/${tableId}/rows?offset=${offset}&limit=${limit}`),
  getSourceRow: (id: string, tableId: string, rowNumber: number,
                 opts: { header?: string | null; col_index?: number | null; context?: number } = {}) => {
    const q = new URLSearchParams();
    q.set("context", String(opts.context ?? 2));
    if (opts.header) q.set("header", opts.header);
    if (opts.col_index != null) q.set("col_index", String(opts.col_index));
    return req<SourceRowContext>(`/jobs/${id}/tables/${tableId}/rows/${rowNumber}?${q.toString()}`);
  },
  getReconciliation: (id: string) => req<Reconciliation>(`/jobs/${id}/reconciliation`),
  getTargetReviews: (id: string) => req<TargetReviewIssue[]>(`/jobs/${id}/target-reviews`),
  startReconciliation: (id: string) => req<Job>(`/jobs/${id}/reconcile`, { method: "POST" }),
  submitTargetDecision: (
    jobId: string,
    issueId: string,
    body: { version: number; action: TargetAction; reason?: string | null; note?: string | null }
  ) =>
    req<{ outcome: string; resume_triggered: boolean; resume_status: string | null; open_target_issues_remaining: number }>(
      `/jobs/${jobId}/target-reviews/${issueId}/decision`, json(body)),

  // M3A.1: immutable employee versions
  getEmployeeVersions: (jobId: string, candidateId: string) =>
    req<EmployeeVersion[]>(`/jobs/${jobId}/candidates/${candidateId}/versions`),
  compareVersions: (jobId: string, candidateId: string, a: number, b: number) =>
    req<VersionCompare>(`/jobs/${jobId}/candidates/${candidateId}/versions/compare?a=${a}&b=${b}`),

  // Migration-admin employee editing / deletion
  editEmployee: (jobId: string, candidateId: string, body: { fields: Record<string, string | null>; note?: string | null }) =>
    req<{ candidate_id: string; changed: boolean; version_no: number | null; changed_fields: string[]; pushed_to_target: boolean; delivery_op_id: string | null }>(
      `/jobs/${jobId}/candidates/${candidateId}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  deleteEmployee: (jobId: string, candidateId: string, note?: string) =>
    req<{ candidate_id: string; delete_requested: boolean; delivery_op_id: string | null }>(
      `/jobs/${jobId}/candidates/${candidateId}${note ? `?note=${encodeURIComponent(note)}` : ""}`, { method: "DELETE" }),

  // M3A.2: tenants, custom-field definitions, proposals
  listTenants: () => req<Tenant[]>("/tenants"),
  getTenantCustomFields: (tenantId: string) =>
    req<CustomFieldDefinition[]>(`/tenants/${encodeURIComponent(tenantId)}/custom-fields`),
  createTenantCustomField: (tenantId: string, body: Record<string, unknown>) =>
    req<CustomFieldDefinition>(`/tenants/${encodeURIComponent(tenantId)}/custom-fields`, json(body)),
  getProposals: (id: string, status: "open" | "all" = "open") =>
    req<CustomFieldProposal[]>(`/jobs/${id}/custom-field-proposals?status=${status}`),
  decideProposal: (jobId: string, proposalId: string, body: ProposalDecisionBody) =>
    req<{ outcome: string; remap_triggered: boolean; remap_status: string | null; open_proposals_remaining: number;
          definition: CustomFieldDefinition | null }>(
      `/jobs/${jobId}/custom-field-proposals/${proposalId}/decision`, json(body)),

  // M3B: delivery
  getDelivery: (id: string) => req<DeliverySummary>(`/jobs/${id}/delivery`),
  getDeliveryAttempts: (jobId: string, opId: string) =>
    req<DeliveryAttempt[]>(`/jobs/${jobId}/delivery/operations/${opId}/attempts`),
  startDelivery: (id: string) => req<Job>(`/jobs/${id}/deliver`, { method: "POST" }),
  retryDeliveryOp: (jobId: string, operationId: string, reason?: string) =>
    req<{ operation_id: string; enqueued: boolean; new_status: string }>(
      `/jobs/${jobId}/delivery/retry`, json({ operation_id: operationId, reason })),
  startRollback: (id: string) =>
    req<{ planned: number; total_succeeded: number; job_status: string }>(
      `/jobs/${id}/rollback`, { method: "POST" }),

  createJob: (files: File[], tenantId?: string | null) => {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    if (tenantId && tenantId.trim()) fd.append("tenant_id", tenantId.trim());
    return req<Job>("/jobs", { method: "POST", body: fd });
  },

  // M3I: safe source-file removal (rebuilds from the remaining set) + migration delete.
  removeFiles: (jobId: string, fileIds: string[], note?: string | null) =>
    req<{
      job_id: string; removed_file_ids: string[]; removed_filenames: string[]; remaining_files: number;
      rebuild_enqueued: boolean; blobs_deleted: number; blob_cleanup_pending: number;
      preserved_custom_field_definitions: number;
    }>(`/jobs/${jobId}/files/remove`, json({ file_ids: fileIds, note: note ?? null })),
  deleteMigration: (jobId: string) =>
    req<{ job_id: string; deleted: boolean; blobs_deleted: number; blob_cleanup_pending: number }>(
      `/jobs/${jobId}`, { method: "DELETE" }),

  submitDecision: (
    jobId: string,
    issueId: string,
    body: { version: number; action: DecisionAction; corrected_target?: string | null; reason?: string | null; note?: string | null }
  ) =>
    req<{ outcome: string; resume_triggered: boolean; resume_status: string | null; open_issues_remaining: number }>(
      `/jobs/${jobId}/reviews/${issueId}/decision`, json(body)),

  submitRecordDecision: (
    jobId: string,
    issueId: string,
    body: {
      version: number;
      action: RecordAction;
      value?: string | null;
      convention?: string | null;
      value_map?: Record<string, string> | null;   // map_values (column-scoped unknown enum)
      pivot?: number | null;                        // confirm_century_pivot (two-digit year)
      scope?: Record<string, any> | null;
      candidate_id?: string | null;
      reason?: string | null;
      note?: string | null;
    }
  ) =>
    req<{ outcome: string; resume_triggered: boolean; resume_status: string | null; open_record_issues_remaining: number }>(
      `/jobs/${jobId}/record-reviews/${issueId}/decision`, json(body)),
};
