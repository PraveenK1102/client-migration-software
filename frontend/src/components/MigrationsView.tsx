import { Link } from "react-router-dom";
import type { Job } from "../types";
import { Icon, cx } from "./ui";
import { MigrationOverflow } from "./MigrationActions";
import { humanStatus, statusTone, migrationName, relativeTime, shortId, orgLabel } from "../labels";

/** Which group a migration belongs to on the landing page. */
function groupOf(j: Job): "attention" | "progress" | "done" {
  const s = j.status;
  if ((j.open_reviews ?? 0) > 0 ||
      ["blocked_provider", "stale_target_review_required", "error",
       "delivery_partial_failure", "rollback_partial_failure"].includes(s)) return "attention";
  if (["migration_complete", "rollback_complete"].includes(s)) return "done";
  return "progress";
}

function MigrationCard({ job, onChanged }: { job: Job; onChanged: () => void }) {
  const tone = statusTone(job.status);
  const employees = job.row_count ?? job.counts?.candidates ?? null;
  const files = job.source_filenames?.length ?? job.counts?.files ?? 0;
  const open = job.open_reviews ?? 0;
  return (
    <Link className={cx("mig-card", tone)} to={`/jobs/${job.id}`}>
      <div className="mig-card-top">
        <div className="stack" style={{ gap: 2, minWidth: 0 }}>
          <div className="mig-org">{orgLabel(job.tenant_id)}</div>
          <div className="mig-name">{migrationName(job.source_filenames, job.id)}</div>
        </div>
        <span className={cx("chip", tone === "neutral" ? "" : tone)}><span className="dot" />{humanStatus(job.status)}</span>
        <MigrationOverflow job={job} onDeleted={onChanged} size="sm" stop />
      </div>
      <div className="mig-meta">
        {employees != null && <span>{employees} employee{employees === 1 ? "" : "s"}</span>}
        <span>{files} file{files === 1 ? "" : "s"}</span>
        {open > 0 && <span className="mig-attn"><Icon name="warn" className="ico-xs" /> {open} need{open === 1 ? "s" : ""} review</span>}
      </div>
      <div className="mig-foot">
        <span className="mono">{shortId(job.id)}</span>
        <span className="spacer" />
        <span>Updated {relativeTime(job.updated_at)}</span>
      </div>
    </Link>
  );
}

const GROUPS: { key: "attention" | "progress" | "done"; label: string; hint: string }[] = [
  { key: "attention", label: "Needs attention", hint: "Waiting on a decision from you" },
  { key: "progress", label: "In progress", hint: "Running automatically" },
  { key: "done", label: "Completed", hint: "Finished migrations" },
];

export function MigrationsView({ jobs, loading, error, onRefresh }:
  { jobs: Job[]; loading: boolean; error: string | null; onRefresh: () => void }) {
  const sorted = [...jobs].sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
  const byGroup = { attention: [] as Job[], progress: [] as Job[], done: [] as Job[] };
  for (const j of sorted) byGroup[groupOf(j)].push(j);

  return (
    <div className="stack lg">
      <div className="page-head">
        <div className="stack" style={{ gap: 2 }}>
          <h1>Migrations</h1>
          <div className="muted">Every migration you have started, newest first. Open one to see its progress and decisions.</div>
        </div>
        <div className="spacer" />
        <button className="icon-btn" onClick={onRefresh} title="Refresh" aria-label="Refresh"><Icon name="refresh" /></button>
        <Link className="btn primary" to="/migrations/new"><Icon name="plus" className="ico" /> New migration</Link>
      </div>

      {error && <div className="banner error">Could not load migrations: {error}</div>}

      {!loading && jobs.length === 0 && !error && (
        <div className="empty">
          <Icon name="collection" />
          <div className="stack" style={{ gap: 4, alignItems: "center" }}>
            <strong>No migrations yet</strong>
            <div className="muted">Start your first migration by uploading a source HR export.</div>
            <Link className="btn primary" to="/migrations/new" style={{ marginTop: 6 }}>New migration</Link>
          </div>
        </div>
      )}

      {loading && jobs.length === 0 && (
        <div className="mig-grid">{[0, 1, 2].map((i) => <div key={i} className="mig-card skeleton" style={{ height: 118 }} />)}</div>
      )}

      {GROUPS.map((g) => byGroup[g.key].length > 0 && (
        <section key={g.key} className="stack">
          <div className="group-head">
            <span className={cx("group-title", g.key === "attention" && "attn")}>{g.label}</span>
            <span className="count-pill">{byGroup[g.key].length}</span>
            <span className="muted small">{g.hint}</span>
          </div>
          <div className="mig-grid">
            {byGroup[g.key].map((j) => <MigrationCard key={j.id} job={j} onChanged={onRefresh} />)}
          </div>
        </section>
      ))}
    </div>
  );
}
