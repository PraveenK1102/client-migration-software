import { useEffect, useMemo, useRef, useState } from "react";
import type { Job } from "../types";
import type { JobBundle, Section } from "../viewtypes";
import { CopyId, Icon, KPI, cx } from "./ui";
import { FIELD_GROUP, STATION_LABELS, humanStatus, orgLabel, relativeTime, statusTone } from "../labels";

/** How long an "auto-continuing" status may sit unchanged before the UI offers a manual fallback
 * button. Generous enough that a normal auto-continue transition (sub-second to a few seconds of
 * compute) never shows it; long enough that a genuinely stalled/disabled auto-continue deployment
 * does not strand the consultant with no way to proceed. */
const AUTO_CONTINUE_GRACE_MS = 6000;

// Status sets for "at or beyond" a phase.
const AFTER_DELIVER = ["delivering", "migration_complete", "delivery_partial_failure",
  "stale_target_review_required", "rollback_in_progress", "rollback_complete", "rollback_partial_failure"];
const AFTER_MAP = ["mapping_complete", "preparing_records", "awaiting_record_review",
  "preparation_complete", "reconciling_target", "awaiting_target_review", "reconciliation_complete",
  "ready_for_delivery", ...AFTER_DELIVER];
const AFTER_PREP = ["preparation_complete", "reconciling_target", "awaiting_target_review",
  "reconciliation_complete", "ready_for_delivery", ...AFTER_DELIVER];
const AFTER_RECON = ["reconciliation_complete", "ready_for_delivery", ...AFTER_DELIVER];
const IN_MAPPING = ["mapping_queued", "profiling", "analyzing_source", "assessing", "applying_decisions",
  "mapping", "processing"];

type StationState = "completed" | "running" | "action" | "waiting" | "failed" | "skipped";
interface Station { key: string; label: string; state: StationState; detail: string; section: Section }

const has = (arr: string[], s: string) => arr.includes(s);

function buildStations(job: Job, b: JobBundle): Station[] {
  const s = job.status, c = job.counts || {};
  const files = c.files ?? 0, parsed = c.files_parsed ?? 0, failed = c.files_failed ?? 0, profiles = c.profiles ?? 0;
  const mapReviews = b.reviews.length + b.proposals.length;
  const recordReviews = b.recordReviews.length;
  const targetReviews = b.targetReviews.length;
  const dc = b.delivery?.counts ?? {};
  const st = job.stage || "";

  const filesRecv: Station = {
    key: "files_received", label: STATION_LABELS.files_received, section: "files",
    state: files > 0 ? "completed" : "running",
    detail: files > 0 ? `${files} file${files === 1 ? "" : "s"} uploaded` : "Waiting for files",
  };

  const read: Station = {
    key: "read_files", label: STATION_LABELS.read_files, section: "files",
    state: failed > 0 ? "failed" : (files > 0 && parsed >= files) ? "completed"
      : files > 0 ? "running" : "waiting",
    detail: failed > 0 ? `${failed} file${failed === 1 ? "" : "s"} could not be read`
      : files > 0 ? `${parsed}/${files} files read` : "Reading rows and sheets",
  };

  const understand: Station = {
    key: "understand", label: STATION_LABELS.understand, section: "mapping",
    state: (profiles > 0 || has(AFTER_MAP, s)) ? "completed"
      : (files > 0 && parsed >= files) ? "running" : "waiting",
    detail: "Checking columns, values, dates, IDs, and relationships",
  };

  let match: Station;
  if (s === "awaiting_review")
    match = { key: "match", label: STATION_LABELS.match_fields, section: "reviews", state: "action",
      detail: `${mapReviews} column decision${mapReviews === 1 ? "" : "s"}` };
  else if (s === "blocked_provider")
    match = { key: "match", label: STATION_LABELS.match_fields, section: "reviews", state: "action",
      detail: `${c.open_proposals ?? 0} field${(c.open_proposals ?? 0) === 1 ? "" : "s"} to decide` };
  else if (has(AFTER_MAP, s))
    match = { key: "match", label: STATION_LABELS.match_fields, section: "mapping", state: "completed",
      detail: `${c.rule_accepted ?? 0} automatic · ${c.model_accepted ?? 0} AI` };
  else if (has(IN_MAPPING, s))
    match = { key: "match", label: STATION_LABELS.match_fields, section: "mapping", state: "running",
      detail: "Matching source columns to target fields" };
  else if (s === "error" && st.includes("map"))
    match = { key: "match", label: STATION_LABELS.match_fields, section: "mapping", state: "failed", detail: "Matching failed" };
  else
    match = { key: "match", label: STATION_LABELS.match_fields, section: "mapping", state: "waiting", detail: "" };

  let clean: Station;
  if (s === "awaiting_record_review")
    clean = { key: "clean", label: STATION_LABELS.clean_validate, section: "reviews", state: "action",
      detail: `${recordReviews} employee/data decision${recordReviews === 1 ? "" : "s"}` };
  else if (has(AFTER_PREP, s))
    clean = { key: "clean", label: STATION_LABELS.clean_validate, section: "prepared", state: "completed",
      detail: `${c.candidates ?? 0} employee${(c.candidates ?? 0) === 1 ? "" : "s"} ready` };
  else if (s === "preparing_records")
    clean = { key: "clean", label: STATION_LABELS.clean_validate, section: "prepared", state: "running",
      detail: "Cleaning values and checking employee records" };
  else
    clean = { key: "clean", label: STATION_LABELS.clean_validate, section: "prepared",
      state: has(AFTER_MAP, s) ? "waiting" : "waiting", detail: has(AFTER_MAP, s) ? "Ready to clean & validate" : "" };

  let compare: Station;
  if (s === "awaiting_target_review")
    compare = { key: "compare", label: STATION_LABELS.compare_target, section: "reviews", state: "action",
      detail: `${targetReviews} target conflict${targetReviews === 1 ? "" : "s"}` };
  else if (has(AFTER_RECON, s))
    compare = { key: "compare", label: STATION_LABELS.compare_target, section: "reconcile", state: "completed",
      detail: "Compared with target" };
  else if (s === "reconciling_target")
    compare = { key: "compare", label: STATION_LABELS.compare_target, section: "reconcile", state: "running",
      detail: "Checking what will be created, updated, skipped, or reviewed" };
  else
    compare = { key: "compare", label: STATION_LABELS.compare_target, section: "reconcile", state: "waiting", detail: "" };

  let sync: Station;
  if (s === "migration_complete")
    sync = { key: "sync", label: STATION_LABELS.sync, section: "delivery", state: "completed",
      detail: `${dc.SUCCEEDED ?? 0} synced` };
  else if (s === "delivering")
    sync = { key: "sync", label: STATION_LABELS.sync, section: "delivery", state: "running",
      detail: "Sending approved employee changes" };
  else if (s === "delivery_partial_failure")
    sync = { key: "sync", label: STATION_LABELS.sync, section: "delivery", state: "failed",
      detail: `${dc.FAILED ?? 0} failed · ${dc.SUCCEEDED ?? 0} synced` };
  else if (s === "stale_target_review_required")
    sync = { key: "sync", label: STATION_LABELS.sync, section: "delivery", state: "action", detail: "Target changed — review" };
  else if (s === "rollback_in_progress")
    sync = { key: "sync", label: STATION_LABELS.sync, section: "delivery", state: "running", detail: "Undoing sync" };
  else if (s === "rollback_complete")
    sync = { key: "sync", label: STATION_LABELS.sync, section: "delivery", state: "completed", detail: "Sync undone" };
  else if (s === "rollback_partial_failure")
    sync = { key: "sync", label: STATION_LABELS.sync, section: "delivery", state: "failed", detail: "Undo needs attention" };
  else
    sync = { key: "sync", label: STATION_LABELS.sync, section: "delivery",
      state: has(AFTER_RECON, s) ? "waiting" : "waiting", detail: has(AFTER_RECON, s) ? "Ready to sync" : "" };

  const complete: Station = {
    key: "complete", label: STATION_LABELS.complete, section: "delivery",
    state: s === "migration_complete" ? "completed"
      : ["error", "delivery_partial_failure", "rollback_partial_failure"].includes(s) ? "failed" : "waiting",
    detail: s === "migration_complete" ? "Migration completed" : "",
  };

  return [filesRecv, read, understand, match, clean, compare, sync, complete];
}

interface NextAction {
  message: string;
  cta?: { label: string; section: Section };
  tone: "amber" | "green" | "blue" | "neutral";
  /** True for a state where the gate has already passed (order §1) and the pipeline is expected to
   * advance on its own — the UI must not present a required manual approval (order §2). */
  autoContinuing?: boolean;
}

function nextAction(job: Job, b: JobBundle): NextAction {
  const s = job.status, c = job.counts || {};
  const mapReviews = b.reviews.length + b.proposals.length;
  const recordReviews = b.recordReviews.length;
  const targetReviews = b.targetReviews.length;
  const rec = b.reconciliation?.counts ?? {};
  const changes = (rec.ready_create ?? 0) + (rec.ready_update ?? 0);
  const dc = b.delivery?.counts ?? {};
  switch (s) {
    case "created": case "queued": return { message: "Getting started — reading your files.", tone: "blue" };
    case "blocked_provider":
      return { message: `${c.open_proposals ?? 0} field decision${(c.open_proposals ?? 0) === 1 ? "" : "s"} need you before matching can finish.`,
        cta: { label: "Review now", section: "reviews" }, tone: "amber" };
    case "awaiting_review":
      return { message: `${mapReviews} field match${mapReviews === 1 ? "" : "es"} need${mapReviews === 1 ? "s" : ""} your review before employee validation can continue.`,
        cta: { label: "Review now", section: "reviews" }, tone: "amber" };
    case "mapping_queued": case "profiling": case "analyzing_source": case "assessing":
    case "applying_decisions": case "mapping": case "processing":
      return { message: "Matching source columns to target fields.", tone: "blue" };
    case "mapping_complete": case "preparing_records":
      return { message: "All fields are matched. Employee validation is running.", tone: "blue" };
    case "awaiting_record_review":
      return { message: `${recordReviews} decision${recordReviews === 1 ? "" : "s"} need${recordReviews === 1 ? "s" : ""} your review before these employees are ready.`,
        cta: { label: "Review now", section: "reviews" }, tone: "amber" };
    case "preparation_complete":
      // Order §1: with zero open reviews the gate has already passed at this point (a record review
      // would have parked the job at awaiting_record_review instead) — compare-with-target is expected
      // to run on its own, never a required manual click (order §2).
      return { message: `${c.candidates ?? 0} employees are ready. Comparing with the target automatically.`,
        cta: { label: "Compare with target", section: "reconcile" }, tone: "blue", autoContinuing: true };
    case "reconciling_target":
      return { message: "Comparing cleaned employee records with the target system.", tone: "blue" };
    case "awaiting_target_review":
      return { message: `${targetReviews} target conflict${targetReviews === 1 ? "" : "s"} need${targetReviews === 1 ? "s" : ""} your review.`,
        cta: { label: "Review now", section: "reviews" }, tone: "amber" };
    case "reconciliation_complete": case "ready_for_delivery":
      // Reaching this status already means zero open target reviews (the worker would otherwise have
      // parked at awaiting_target_review) — sync is expected to start on its own.
      return { message: `${changes} employee change${changes === 1 ? "" : "s"} ready. Syncing automatically.`,
        cta: { label: "Start sync", section: "delivery" }, tone: "blue", autoContinuing: true };
    case "delivering": return { message: "Syncing employee changes to the target system.", tone: "blue" };
    case "migration_complete": return { message: "Migration completed successfully.", tone: "green" };
    case "delivery_partial_failure":
      return { message: `${dc.FAILED ?? 0} record${(dc.FAILED ?? 0) === 1 ? "" : "s"} failed to sync — review and retry.`,
        cta: { label: "Open Sync", section: "delivery" }, tone: "amber" };
    case "stale_target_review_required":
      return { message: "A target record changed during sync — review before continuing.",
        cta: { label: "Open Sync", section: "delivery" }, tone: "amber" };
    case "rollback_in_progress": return { message: "Undoing the sync.", tone: "blue" };
    case "rollback_complete": return { message: "The sync was undone.", tone: "neutral" };
    case "error": return { message: `This migration needs attention: ${job.error ?? "unknown error"}.`, tone: "amber" };
    default: return { message: humanStatus(s), tone: "neutral" };
  }
}

const RAIL_ICON: Record<StationState, string> = {
  completed: "check", running: "refresh", action: "warn", waiting: "dot", failed: "warn", skipped: "dot",
};

export function Overview({ job, bundle, go }: { job: Job; bundle: JobBundle; go: (s: Section) => void }) {
  const c = job.counts || {};
  const stations = buildStations(job, bundle);
  const na = nextAction(job, bundle);

  // Order §1/§2: an "auto-continuing" status shows no required manual action. If it lingers past a
  // grace period (auto-continue disabled, or a genuine stall), reveal the existing manual trigger as a
  // fallback rather than stranding the consultant — never as the PRIMARY expected action.
  const statusSince = useRef<number>(Date.now());
  const prevStatus = useRef(job.status);
  const [showFallback, setShowFallback] = useState(false);
  useEffect(() => {
    if (prevStatus.current !== job.status) {
      prevStatus.current = job.status;
      statusSince.current = Date.now();
      setShowFallback(false);
    }
  }, [job.status]);
  useEffect(() => {
    if (!na.autoContinuing) { setShowFallback(false); return; }
    const elapsed = Date.now() - statusSince.current;
    if (elapsed >= AUTO_CONTINUE_GRACE_MS) { setShowFallback(true); return; }
    const t = setTimeout(() => setShowFallback(true), AUTO_CONTINUE_GRACE_MS - elapsed);
    return () => clearTimeout(t);
  }, [na.autoContinuing, job.status]);
  const rows = job.row_count ?? bundle.profiles?.tables.reduce((n, t) => n + t.n_rows, 0) ?? 0;
  // The employee count is only real once preparation has grouped source rows into candidates. Before
  // that, source rows ≠ employees (multiple files each describe different things — an employee master
  // plus, say, an addresses file), so we say "Calculating…" rather than showing a misleading row count.
  const employeeCount = c.candidates ?? 0;
  const fieldsMatched = bundle.mappings?.accepted?.length ?? ((c.rule_accepted ?? 0) + (c.model_accepted ?? 0));
  // "Needs review" splits by what the review is ABOUT: a mapping/proposal review is a FILE/COLUMN
  // fact (which column goes where) — folded into the per-column-matching table's own "Needs review"
  // column, per file. A record or target review is about an EMPLOYEE/sync decision, so it stays as
  // its own indicator in the common overview instead.
  const employeeReviews = bundle.recordReviews.length + bundle.targetReviews.length;
  const prepDone = has(AFTER_PREP, job.status);
  const employeesKnown = prepDone || employeeCount > 0;   // count is real only after preparation groups rows
  const rec = bundle.reconciliation?.counts ?? {};
  const reconRun = (bundle.reconciliation?.results?.length ?? 0) > 0;
  const readyToSync = (rec.ready_create ?? 0) + (rec.ready_update ?? 0);
  const dc = bundle.delivery?.counts ?? {};
  const deliveryRun = (bundle.delivery?.operations?.length ?? 0) > 0;
  const synced = deliveryRun ? dc.SUCCEEDED ?? 0 : null;
  const humanMaps = (bundle.mappings?.accepted ?? []).filter((m) => m.actor === "human").length;

  // Column-matching is inherently a PER-FILE fact, not a global one: one migration can hold several
  // files (or several sheets in one file), each with its own column layout — a single "N of M
  // columns matched" number would blur that. Group the already-fetched table profiles by source file
  // and cross-reference accepted mappings by table_id (no new endpoint needed).
  const filesColumnStats = useMemo(() => {
    const tables = bundle.profiles?.tables ?? [];
    if (!tables.length) return [];
    const acceptedByTable = new Map<string, number>();
    for (const m of bundle.mappings?.accepted ?? [])
      acceptedByTable.set(m.table_id, (acceptedByTable.get(m.table_id) ?? 0) + 1);
    // Column/field reviews are keyed by table_id too, so THIS file's own open count (not a global
    // total) is exactly answerable here — the same "everything about this file, in this row" idea
    // as the columns-matched count.
    const reviewsByTable = new Map<string, number>();
    for (const r of [...bundle.reviews, ...bundle.proposals])
      reviewsByTable.set(r.table_id, (reviewsByTable.get(r.table_id) ?? 0) + 1);
    const byFile = new Map<string, { filename: string; totalCols: number; matchedCols: number; rows: number; sheets: number; needsReview: number }>();
    for (const t of tables) {
      const entry = byFile.get(t.file_id) ?? { filename: t.original_filename, totalCols: 0, matchedCols: 0, rows: 0, sheets: 0, needsReview: 0 };
      entry.needsReview += reviewsByTable.get(t.table_id) ?? 0;
      entry.totalCols += t.headers.length;
      entry.matchedCols += acceptedByTable.get(t.table_id) ?? 0;
      entry.rows += t.n_rows;
      entry.sheets += 1;
      byFile.set(t.file_id, entry);
    }
    return [...byFile.values()];
  }, [bundle.profiles, bundle.mappings, bundle.reviews, bundle.proposals]);

  const files = job.source_filenames ?? [];
  const sourceLabel = files.length === 0 ? "No source files"
    : files.length <= 2 ? files.join(", ")
    : `${files.slice(0, 2).join(", ")} +${files.length - 2} more`;

  return (
    <div className="stack lg">
      <div className="page-head">
        <div className="stack" style={{ gap: 3, minWidth: 0 }}>
          <h1>{orgLabel(job.tenant_id)}</h1>
          <div className="muted">Employee migration · <span title={files.join(", ")}>{sourceLabel}</span> · Updated {relativeTime(job.updated_at)}</div>
        </div>
        <div className="spacer" />
        <div className="row" style={{ gap: 8, alignItems: "center" }}>
          <span className={cx("chip", statusTone(job.status))}><span className="dot" />{humanStatus(job.status)}</span>
          <CopyId id={job.id} />
        </div>
      </div>

      {/* Next action — an auto-continuing state never presents its trigger as a required click (§2);
          the same manual control only reappears, secondary, if the state lingers past the grace period. */}
      <div className={cx("next-action", na.tone)}>
        <div className="na-body">
          <div className="na-eyebrow">{na.autoContinuing ? "Continuing automatically" : "Next step"}</div>
          <div className="na-msg">
            {na.autoContinuing && <Icon name="refresh" className="ico-xs ico-spin" />} {na.message}
          </div>
        </div>
        {na.cta && (!na.autoContinuing || showFallback) && (
          <button className={cx("btn", na.autoContinuing ? "ghost sm" : "primary")} onClick={() => go(na.cta!.section)}>
            {na.autoContinuing ? `Taking a while — ${na.cta.label.toLowerCase()} now` : na.cta.label}
          </button>
        )}
      </div>

      {/* Process station rail */}
      <div className="rail" role="list" aria-label="Migration stages">
        {stations.map((st, i) => (
          <button key={st.key} role="listitem" className={cx("rail-station", st.state)} onClick={() => go(st.section)}
            title={st.detail || st.label}>
            {i > 0 && <span className={cx("rail-line", (st.state === "completed" || st.state === "running" || st.state === "action" || st.state === "failed") && "lit")} />}
            <span className="rail-dot"><Icon name={RAIL_ICON[st.state]} className="ico-xs" /></span>
            <span className="rail-label">{st.label}</span>
            {st.detail && <span className="rail-detail">{st.detail}</span>}
          </button>
        ))}
      </div>

      {/* Above-the-fold summary — the handful of GLOBAL facts that stay meaningful regardless of how
          many files/columns are involved. Column-matching (which varies file to file) lives in
          "Columns matched per file" below, not here. */}
      <div className="kpis">
        <KPI label="Files" value={c.files ?? 0} />
        <KPI label="Source rows" value={rows} sub={(c.files ?? 0) > 1 ? "across all files" : undefined} />
        {/* Employee count is real only once preparation groups rows into candidates — until then the
            source-row total is not the employee total (files describe different things), so: Calculating… */}
        <KPI label={prepDone ? "Employees ready" : "Employees found"}
          value={employeesKnown ? employeeCount
            : <span className="muted" style={{ fontSize: 15, fontWeight: 500 }}>Calculating…</span>} />
        {/* Employee-data reviews (duplicate email, a value conflicting with the target, …) are about
            an EMPLOYEE, not a file/column, so they stay here rather than in the per-file panel. */}
        {employeeReviews > 0 && <KPI label="Employee decisions needed" value={employeeReviews} tone="amber" />}
        {/* "Ready to sync" is the PRE-delivery plan; once delivery has actually run, "Synced" is the
            current, more truthful status — never show both (a completed migration must not still
            claim employees are "ready to sync" when they already were). */}
        {reconRun && !deliveryRun && <KPI label="Ready to sync" value={readyToSync} />}
        {synced != null && <KPI label="Synced" value={synced} tone={synced > 0 ? "green" : undefined} />}
      </div>

      {/* Field breakdown (M3G §E1) — human categories, each self-explaining, no core/structured/custom */}
      {(fieldsMatched > 0 || has(AFTER_MAP, job.status)) && (
        <div className="panel field-breakdown">
          <div className="panel-b">
            <div className="fb-head">
              <div className="section-title" style={{ margin: 0 }}>How your columns were matched</div>
              <span className="muted small">Each source column is matched to a target employee field. Uncertain matches wait for your review.</span>
            </div>
            <div className="fb-grid">
              {[
                { ...FIELD_GROUP.standard, n: c.mapped_core ?? 0, always: true },
                { ...FIELD_GROUP.related, n: c.mapped_collection ?? 0, always: true },
                { ...FIELD_GROUP.organization, n: c.mapped_custom ?? 0, always: true },
                { ...FIELD_GROUP.not_mapped, n: c.ignored_columns ?? 0, always: false },
              ].filter((cat) => cat.always || cat.n > 0).map((cat) => (
                <div className="fb-tile" key={cat.label}>
                  <div className="fb-n">{cat.n}</div>
                  <div className="fb-l">{cat.label}</div>
                  <div className="fb-h">{cat.hint}</div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Per-FILE column matching — a migration can hold several files (or several sheets), each
          with its own column layout, so "how many columns matched" only means something per file. */}
      {filesColumnStats.length > 0 && (
        <div className="panel">
          <div className="panel-b">
            <div className="fb-head">
              <div className="section-title" style={{ margin: 0 }}>Columns matched per file</div>
              <span className="muted small">
                {fieldsMatched} field{fieldsMatched === 1 ? "" : "s"} matched across {filesColumnStats.length} file{filesColumnStats.length === 1 ? "" : "s"}
                {" — "}{c.rule_accepted ?? 0} automatically
                {(c.model_accepted ?? 0) > 0 && `, ${c.model_accepted} by AI`}
                {humanMaps > 0 && `, ${humanMaps} confirmed by you`}.
                Each file (and any related-record sheet inside it) has its own columns.
              </span>
            </div>
            <div className="table-wrap" style={{ marginTop: 10 }}>
              <table className="data-table">
                <thead><tr><th>File</th><th className="num">Rows</th><th className="num">Columns matched</th><th style={{ width: 130 }}></th><th className="num">Needs review</th></tr></thead>
                <tbody>
                  {filesColumnStats.map((f) => {
                    const pct = f.totalCols > 0 ? Math.round((f.matchedCols / f.totalCols) * 100) : 0;
                    return (
                      <tr key={f.filename} className="clickable" onClick={() => go(f.needsReview > 0 ? "reviews" : "files")}>
                        <td><span className="mono">{f.filename}</span>{f.sheets > 1 && <span className="muted small"> · {f.sheets} sheets</span>}</td>
                        <td className="num">{f.rows}</td>
                        <td className="num">{f.matchedCols} of {f.totalCols}</td>
                        <td>
                          <div className="row" style={{ gap: 8 }}>
                            <span className="timing-track" style={{ flex: 1 }}>
                              <span className="timing-fill" style={{ width: `${Math.max(3, pct)}%` }} />
                            </span>
                            <span className="muted small" style={{ minWidth: 32, textAlign: "right" }}>{pct}%</span>
                          </div>
                        </td>
                        <td className="num">
                          {f.needsReview > 0
                            ? <span className="chip amber"><span className="dot" />{f.needsReview}</span>
                            : <span className="cell-empty">—</span>}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* Preparation-blocked clarification (fixes "Prepared employees = 0" while review is active) */}
      {job.status === "awaiting_record_review" && (
        <div className="note">
          <Icon name="warn" className="ico-xs" /> {c.candidates ?? 0} employee record{(c.candidates ?? 0) === 1 ? "" : "s"} analyzed.
          Cleaning &amp; validation is paused on {bundle.recordReviews.length} decision{bundle.recordReviews.length === 1 ? "" : "s"} before they are ready to sync.
        </div>
      )}

      {/* Technical details — MIGRATION-LEVEL operational detail only, ZERO-NOISE: a card renders only
          when its value is nonzero or it is one always worth confirming. File-level facts (columns
          matched by rule/AI/you, source rows, per-file parsing) live in "Columns matched per file"
          above — they mean something per file, not for the migration as a whole, so they are not
          repeated here. What remains are whole-migration outcomes: reconciliation and sync. */}
      {(() => {
        type Tile = { label: string; value: React.ReactNode; tone?: "amber" | "green" | "red"; always?: boolean };
        const group = (tiles: Tile[]) => tiles.filter((t) => t.always || Number(t.value) > 0);
        const tiles = [
          ...(reconRun ? group([
            { label: "New employee", value: rec.ready_create ?? 0, always: true },
            { label: "Update to existing employee record", value: rec.ready_update ?? 0, always: true },
            { label: "No changes", value: rec.no_change ?? 0 },
            { label: "Target conflicts", value: rec.review_required ?? 0, tone: "amber" },
            { label: "Excluded from target", value: rec.excluded_target ?? 0 },
          ]) : []),
          ...(deliveryRun ? group([
            { label: "Synced", value: dc.SUCCEEDED ?? 0, tone: "green", always: true },
            { label: "Sync failed", value: dc.FAILED ?? 0, tone: "red" },
            { label: "Waiting to retry", value: dc.RETRYABLE ?? 0, tone: "amber" },
            { label: "Sync undone", value: dc.ROLLED_BACK ?? 0 },
          ]) : []),
        ];
        if (!tiles.length) return null;
        return (
          <details className="tech">
            <summary>Technical details</summary>
            <div className="kpis">{tiles.map((t) => <KPI key={t.label} {...t} />)}</div>
          </details>
        );
      })()}
    </div>
  );
}
