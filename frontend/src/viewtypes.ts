import type {
  AuditEvent, Candidate, CustomFieldDefinition, CustomFieldProposal, DeliverySummary,
  MappingsResponse, ProfilesResponse,
  Reconciliation, RecordIssue, ReviewIssue, SourceFile, TargetReviewIssue, WorkItem,
} from "./types";

export interface JobBundle {
  files: SourceFile[];
  workItems: WorkItem[];
  profiles: ProfilesResponse | null;
  mappings: MappingsResponse | null;
  reviews: ReviewIssue[];
  recordReviews: RecordIssue[];
  candidates: Candidate[];
  reconciliation: Reconciliation | null;
  targetReviews: TargetReviewIssue[];
  audit: AuditEvent[];
  proposals: CustomFieldProposal[];
  tenantFields: CustomFieldDefinition[];
  delivery: DeliverySummary | null;
}

export type Section =
  | "overview" | "files" | "mapping" | "prepared" | "reviews" | "reconcile" | "delivery"
  | "metrics" | "audit" | "schema";

/** A provenance reference precise enough to open the exact persisted source cell. */
export interface SourceRef {
  table_id: string;
  row_number: number;
  header?: string | null;
  col_index?: number | null;
  original_filename?: string;
  sheet_name?: string | null;
}
