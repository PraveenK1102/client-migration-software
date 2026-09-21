/**
 * Single source of user-facing copy for a NON-TECHNICAL implementation consultant.
 *
 * Backend identifiers (tenant_id, job_id, candidate, reconciliation, awaiting_record_review, …) stay
 * unchanged in the API/DB/tests; this module maps them to plain product language for the UI. Anything
 * technical only appears behind an explicit "Technical details" disclosure.
 */

// ── Terminology ────────────────────────────────────────────────────────────────────────────────
export const ORG = "Organization"; // never "Tenant" / "Customer" in the primary UI
export const SOURCE_SYSTEM = "Source HR system";

/** A human Organization name from the internal tenant_id slug (never shows the word "tenant"). */
export function orgLabel(tenantId: string | null | undefined): string {
  if (!tenantId || tenantId === "default") return "Default organization";
  return tenantId.replace(/[-_.]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Turn a free-text organization name the consultant typed (e.g. "Acme Pvt Ltd") into a valid
 * tenant_id the backend accepts (only [A-Za-z0-9-_.]). Spaces become hyphens; other stray characters
 * are dropped. This is the inverse of orgLabel, so "Acme Pvt Ltd" ⇄ "Acme-Pvt-Ltd" round-trips, and
 * it's idempotent on an already-valid id picked from the suggestions. Returns "" for a blank input. */
export function slugifyOrg(input: string): string {
  return input.trim().replace(/\s+/g, "-").replace(/[^A-Za-z0-9\-_.]/g, "").replace(/^[-_.]+|[-_.]+$/g, "").slice(0, 64);
}

// ── Sidebar / navigation labels (addendum §4) ───────────────────────────────────────────────────
export const NAV_LABELS = {
  migrations: "Migrations",
  new: "New migration",
  overview: "Overview",
  files: "Files",                       // was "Source Files"
  mapping: "Field mapping",             // was "Mapping"
  prepared: "Employees",               // was "Prepared Employees"
  reviews: "Needs review",              // was "Reviews"
  reconcile: "Compare with target",    // was "Target Reconciliation"
  delivery: "Sync",                     // was "Delivery"
  metrics: "Metrics",
  audit: "Activity log",               // was "Audit Trail"
  schema: "Target fields",             // was "Target schema"
} as const;

// ── Process stations (addendum §5) — the "bus route" the migration travels ───────────────────────
export const STATION_LABELS = {
  files_received: "Files received",
  read_files: "Read files",
  understand: "Understand data",
  match_fields: "Match fields",
  clean_validate: "Clean & validate",
  compare_target: "Compare with target",
  sync: "Sync",
  complete: "Complete",
} as const;

// ── Status language (addendum §6) — raw job.status -> product phrase ──────────────────────────────
const STATUS_LABEL: Record<string, string> = {
  created: "Waiting to start",
  queued: "Waiting to start",
  mapping_queued: "Matching fields",
  ingesting: "Reading files",
  profiling: "Understanding data",
  analyzing_source: "Understanding data",
  assessing: "Matching fields",
  applying_decisions: "Matching fields",
  mapping: "Matching fields",
  processing: "Working…",
  blocked_provider: "Needs attention",
  mapping_complete: "Fields matched",
  awaiting_review: "Needs your review",
  preparing_records: "Cleaning & validating",
  awaiting_record_review: "Needs your review",
  preparation_complete: "Ready to compare",
  reconciling_target: "Comparing with target",
  awaiting_target_review: "Needs your review",
  reconciliation_complete: "Ready to sync",
  ready_for_delivery: "Ready to sync",
  delivering: "Syncing",
  migration_complete: "Completed",
  delivery_partial_failure: "Sync needs attention",
  stale_target_review_required: "Needs your review",
  rollback_in_progress: "Undoing sync",
  rollback_complete: "Sync undone",
  rollback_partial_failure: "Undo needs attention",
  error: "Needs attention",
};

export function humanStatus(status: string | null | undefined): string {
  if (!status) return "—";
  return STATUS_LABEL[status] ?? status.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export type Tone = "green" | "amber" | "red" | "blue" | "neutral";
export function statusTone(status: string | null | undefined): Tone {
  if (!status) return "neutral";
  if (["migration_complete", "reconciliation_complete", "preparation_complete", "mapping_complete",
       "rollback_complete", "ready_for_delivery"].includes(status)) return "green";
  if (["awaiting_review", "awaiting_record_review", "awaiting_target_review",
       "stale_target_review_required", "blocked_provider"].includes(status)) return "amber";
  if (["error", "delivery_partial_failure", "rollback_partial_failure"].includes(status)) return "red";
  if (["delivering", "reconciling_target", "preparing_records", "mapping", "mapping_queued",
       "profiling", "analyzing_source", "assessing", "applying_decisions", "processing",
       "rollback_in_progress", "ingesting"].includes(status)) return "blue";
  return "neutral";
}

// ── Reconciliation outcome language (addendum §14) ───────────────────────────────────────────────
export const OUTCOME_LABEL: Record<string, string> = {
  READY_CREATE: "New employee",
  READY_UPDATE: "Update to existing employee record",
  NO_CHANGE: "No changes",
  REVIEW_REQUIRED: "Needs review",
  EXCLUDED: "Excluded",
};

// ── Field categories (M3G §E1) — human names + self-explanations, never "core/structured/custom" ──
export const FIELD_GROUP = {
  standard: {
    label: "Standard employee fields",
    hint: "Common fields such as name, employee ID, hire date, work email",
  },
  related: {
    label: "Related records",
    hint: "Repeating information such as addresses, dependents, education",
  },
  organization: {
    label: "Organization fields",
    hint: "Fields configured only for this organization",
  },
  not_mapped: {
    label: "Not mapped",
    hint: "Source columns not migrated to a target field",
  },
} as const;

/** Destination kind (CORE_FIELD/COLLECTION_FIELD/CUSTOM_FIELD/…) -> human field-category label. */
export function destinationLabel(kind: string | null | undefined): string {
  switch (kind) {
    case "CORE_FIELD": return FIELD_GROUP.standard.label;
    case "COLLECTION_FIELD": return FIELD_GROUP.related.label;
    case "CUSTOM_FIELD": return FIELD_GROUP.organization.label;
    case "NEEDS_REVIEW": return "Needs review";
    case "PROPOSAL": return "New organization field";
    case "IGNORED": return "Not migrated";
    case "UNMAPPED": return "Not mapped";
    default: return (kind || "Not mapped").toLowerCase();
  }
}

// ── Mapping status / method (M3G §G) — no developer labels (AUTO_ACCEPT/NEEDS_REVIEW) in primary UI ─
/** raw mapping status -> product badge: Automatic / AI suggested / Confirmed by you / Needs review / …. */
export function mappingStatusLabel(status: string | null | undefined, method?: string | null): string {
  switch (status) {
    case "auto_accepted": return method === "model" ? "AI suggested" : "Automatic";
    case "approved": case "corrected": return "Confirmed by you";
    case "needs_review": return "Needs review";
    case "proposal": return "Needs review";
    case "ignored": return "Not migrated";
    case "unmapped": return "Not mapped";
    default: return (status || "—").replace(/_/g, " ");
  }
}

/** raw mapping method (rule/model/human) -> who decided, in plain language. */
export function methodLabel(method: string | null | undefined): string {
  switch ((method || "").toLowerCase()) {
    case "rule": return "Rule";
    case "model": return "AI + guardrails";
    case "human": return "Confirmed by you";
    default: return "Still unresolved";
  }
}

// ── Readable durations (addendum §16 / §I) — never "0.00 sec" for a few-ms value ─────────────────
export function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms <= 0) return "0 ms";
  if (ms < 1) return "<1 ms";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  const min = Math.floor(ms / 60000);
  const sec = Math.round((ms % 60000) / 1000);
  return sec ? `${min}m ${sec}s` : `${min} min`;
}

export function shortId(id: string | null | undefined): string {
  if (!id) return "—";
  return id.length > 14 ? `${id.slice(0, 12)}…` : id;
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso.replace(" ", "T")).getTime();
  if (Number.isNaN(then)) return String(iso);
  const s = Math.round((Date.now() - then) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  const d = Math.floor(s / 86400);
  if (d < 30) return `${d} d ago`;
  return new Date(then).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

/** A human display name for a migration card / header from its source filenames or id. */
export function migrationName(filenames: string[] | undefined, id: string): string {
  if (filenames && filenames.length) {
    const base = filenames[0].replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ");
    return filenames.length > 1 ? `${base} +${filenames.length - 1} more` : base;
  }
  return `Migration ${shortId(id)}`;
}
