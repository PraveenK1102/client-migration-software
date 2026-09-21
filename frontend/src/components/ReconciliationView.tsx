import { useState } from "react";
import type { Job, ReconciliationRow, TargetSchema } from "../types";
import type { JobBundle, Section } from "../viewtypes";
import { Drawer, Icon, KPI, OutcomeChip, cx } from "./ui";
import { OUTCOME_LABEL } from "../labels";

const AT_OR_AFTER_MAP = ["mapping_complete", "preparing_records", "awaiting_record_review",
  "preparation_complete", "reconciling_target", "awaiting_target_review", "reconciliation_complete",
  "ready_for_delivery", "delivering", "migration_complete", "delivery_partial_failure",
  "stale_target_review_required", "rollback_in_progress", "rollback_complete", "rollback_partial_failure"];
const AT_OR_AFTER_PREP = ["preparation_complete", "reconciling_target", "awaiting_target_review",
  "reconciliation_complete", "ready_for_delivery", "delivering", "migration_complete",
  "delivery_partial_failure", "stale_target_review_required", "rollback_in_progress",
  "rollback_complete", "rollback_partial_failure"];

function why(r: ReconciliationRow): string {
  if (r.outcome === "NO_CHANGE") return "Identical to target";
  if (r.outcome === "READY_CREATE") return "Not found in target";
  if (r.outcome === "EXCLUDED") return "Excluded by reviewer";
  if (r.outcome === "READY_UPDATE") {
    const f = Object.entries(r.diff || {}).filter(([, v]: any) => v.status === "update").map(([k]) => k);
    return f.length ? `Update ${f.join(", ")}` : "Safe update";
  }
  return "Needs review";
}
const show = (v: any) => v == null || v === "" ? <span className="cell-empty">—</span> : Array.isArray(v) ? v.join(", ") : typeof v === "object" ? Object.entries(v).map(([k, x]) => `${k}: ${x ?? "—"}`).join(" · ") : String(v);

export function ReconciliationView({ job, bundle, schema, onReconcile, busy, go }: {
  job: Job; bundle: JobBundle; schema: TargetSchema | null;
  onReconcile: () => void;
  busy: boolean;
  go: (s: Section) => void;
}) {
  const [sel, setSel] = useState<ReconciliationRow | null>(null);
  const rec = bundle.reconciliation;
  const results = rec?.results ?? [];
  const c = rec?.counts ?? {};
  const canRun = ["preparation_complete", "reconciling_target", "awaiting_target_review", "reconciliation_complete"].includes(job.status);
  const complete = rec?.summary?.result === "reconciliation_complete";
  const nameOf = (cid: string) => bundle.candidates.find((x) => x.id === cid)?.record?.full_name?.value;

  return (
    <div>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <div className="section-title">Compare with target</div>
          <div className="section-desc">We compare the cleaned employee records with the target system before syncing changes. This is a read-only comparison — nothing is written yet.</div>
        </div>
        {canRun && <button type="button" className="btn primary" disabled={busy} onClick={onReconcile}>
          {busy ? <span className="spin" /> : <Icon name="reconcile" />} {results.length ? "Compare again" : "Compare with target"}</button>}
      </div>

      {/* Meaningful blocked/empty state (M3G §J) — never a blank screen; always say why + what next. */}
      {results.length === 0 && !complete && (() => {
        const fieldsMatched = AT_OR_AFTER_MAP.includes(job.status);
        const employeesReady = AT_OR_AFTER_PREP.includes(job.status);
        const openDecisions = bundle.reviews.length + bundle.proposals.length + bundle.recordReviews.length;
        return (
          <div className="empty-state" style={{ marginTop: 16 }}>
            <Icon name="reconcile" />
            <div className="stack" style={{ gap: 6, alignItems: "center", textAlign: "center" }}>
              <strong>{employeesReady ? "Ready to compare with the target" : "Comparison hasn't started yet"}</strong>
              {employeesReady ? (
                <div className="muted">Run the comparison to see what will be created, updated, left unchanged, reviewed, or excluded. Nothing is written to the target during comparison.</div>
              ) : openDecisions > 0 ? (
                <div className="muted">Comparison will start after {openDecisions} review decision{openDecisions === 1 ? "" : "s"} {openDecisions === 1 ? "is" : "are"} resolved.</div>
              ) : (
                <div className="muted">Comparison starts automatically once fields are matched and employee records are validated.</div>
              )}
              <div className="prereqs">
                <span className={cx("prereq", fieldsMatched && "done")}><Icon name={fieldsMatched ? "check" : "dot"} className="ico-xs" /> Fields matched</span>
                <span className={cx("prereq", employeesReady && "done")}><Icon name={employeesReady ? "check" : "dot"} className="ico-xs" /> Employees validated</span>
              </div>
              <div className="row" style={{ gap: 8, marginTop: 4, justifyContent: "center" }}>
                {employeesReady && <button type="button" className="btn primary" disabled={busy} onClick={onReconcile}>{busy ? <span className="spin" /> : <Icon name="reconcile" />} Compare with target</button>}
                {openDecisions > 0 && <button type="button" className="btn" onClick={() => go("reviews")}>Go to Needs review <Icon name="arrow" /></button>}
              </div>
            </div>
          </div>
        );
      })()}
      {complete && <div className="banner info" style={{ marginTop: 14 }}>
        Comparison complete. Every employee is New / Update to existing employee record / No changes and no target conflicts remain. Nothing has been written to the target yet — that happens in Sync.</div>}
      {bundle.targetReviews.length > 0 && (
        <div className="banner warn dl-inline" style={{ marginTop: 14 }}>
          <Icon name="warn" className="ico-xs" /> {bundle.targetReviews.length} target conflict{bundle.targetReviews.length === 1 ? " needs" : "s need"} a decision (keep current target value / use incoming / exclude).
          <button type="button" className="btn sm" onClick={() => go("reviews")}>Open in Needs review <Icon name="arrow" /></button>
        </div>
      )}

      {results.length > 0 && (
        <div className="kpis" style={{ marginTop: 14 }}>
          <KPI label={OUTCOME_LABEL.READY_CREATE} value={c.ready_create ?? 0} />
          <KPI label={OUTCOME_LABEL.READY_UPDATE} value={c.ready_update ?? 0} />
          <KPI label={OUTCOME_LABEL.NO_CHANGE} value={c.no_change ?? 0} />
          <KPI label={OUTCOME_LABEL.REVIEW_REQUIRED} value={c.review_required ?? 0} tone={c.review_required ? "amber" : undefined} />
          <KPI label={OUTCOME_LABEL.EXCLUDED} value={c.excluded_target ?? 0} />
        </div>
      )}

      {results.length > 0 && (
        <div className="panel" style={{ marginTop: 16 }}>
          <div className="table-wrap">
            <table className="data-table">
              <thead><tr><th>Employee</th><th>Name</th><th>Outcome</th><th>Matched on</th><th>Target id</th><th className="num">Rev</th><th>Why</th></tr></thead>
              <tbody>
                {results.map((r) => (
                  <tr key={r.candidate_id} className="clickable" onClick={() => setSel(r)}>
                    <td><span className="mono">{r.business_key ?? r.candidate_id}</span></td>
                    <td>{nameOf(r.candidate_id) ?? <span className="cell-empty">—</span>}</td>
                    <td><OutcomeChip outcome={r.outcome} /></td>
                    <td className="small muted">{r.match_basis}</td>
                    <td className="mono small">{r.target_record_id ?? "—"}</td>
                    <td className="num">{r.target_revision ?? "—"}</td>
                    <td className="small muted">{why(r)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {sel && (
        <Drawer wide title={`${nameOf(sel.candidate_id) ?? "Employee"} · EMP-${sel.business_key ?? sel.candidate_id}`}
          subtitle={<OutcomeChip outcome={sel.outcome} />} onClose={() => setSel(null)}>
          <div className="dl" style={{ marginBottom: 14 }}>
            <div className="k">Match basis</div><div>{sel.match_basis}</div>
            <div className="k">Target record</div><div className="mono">{sel.target_record_id ?? "—"}</div>
            <div className="k">Target revision</div><div>{sel.target_revision ?? "—"}</div>
          </div>
          {sel.diff && Object.keys(sel.diff).length > 0 ? (
            <table className="compare">
              <thead><tr><th>Field</th><th>Incoming</th><th>Existing target</th><th>Result</th></tr></thead>
              <tbody>
                {Object.entries(sel.diff).map(([f, d]: any) => {
                  if (Array.isArray(d.items)) {
                    const label = schema?.collections.find((c) => `${c.key}[]` === f)?.label ?? f;
                    return d.items.map((it: any) => (
                      <tr key={`${f}-${it.identity_key}`} className={it.status === "conflict" || it.status === "update" ? "diff" : ""}>
                        <td className="field">{label} · {it.identity_key}</td>
                        <td>{show(it.incoming)}</td>
                        <td>{show(it.target)}</td>
                        <td><span className="small">{it.decision ? `${it.status} (${it.decision.replace(/_/g, " ")})` : it.status}</span></td>
                      </tr>
                    ));
                  }
                  return (
                    <tr key={f} className={d.status === "conflict" || d.status === "update" ? "diff" : ""}>
                      <td className="field">{schema?.fields.find((x) => x.name === f)?.label ?? f}</td>
                      <td>{show(d.incoming)}</td>
                      <td>{d.target == null || d.target === "" ? <span className="cell-empty">(blank)</span> : show(d.target)}</td>
                      <td><span className="small">{d.decision ? `${d.status} (${d.decision.replace(/_/g, " ")})` : d.status}</span></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : (
            <div className="banner info">
              {sel.outcome === "READY_CREATE"
                ? "This is a new employee — no matching record exists in the target yet, so there is nothing to compare. The full record will be created when you sync."
                : sel.outcome === "EXCLUDED"
                ? "This employee is excluded, so it is not compared with the target and nothing will be written."
                : sel.outcome === "NO_CHANGE"
                ? "This record already matches the target exactly — nothing will change when you sync."
                : "No field-by-field differences to show for this employee."}
            </div>
          )}
        </Drawer>
      )}
    </div>
  );
}
