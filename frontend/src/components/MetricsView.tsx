import { useEffect, useState } from "react";
import { api } from "../api";
import type { JobMetrics } from "../types";
import { KPI } from "./ui";
import { formatDuration } from "../labels";

/** Format a USD estimate for the metrics UI. Always 2+ decimals; switches to 4 decimals under a cent
 * so a real-but-tiny cost never rounds away to a misleading "$0.00". */
function fmtUsd(v: number): string {
  if (v > 0 && v < 0.01) return `$${v.toFixed(4)}`;
  return `$${v.toFixed(2)}`;
}

/** Product + engineering metrics for one migration — answers "how much was automatic, what used AI,
 * what needed a human, what was transformed, what reached the target". All read from persisted state. */
export function MetricsView({ jobId }: { jobId: string }) {
  const [m, setM] = useState<JobMetrics | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api.getJobMetrics(jobId).then((x) => live && setM(x)).catch((e) => live && setErr(String(e)));
    return () => { live = false; };
  }, [jobId]);

  if (err) return <div className="banner error">Could not load metrics: {err}</div>;
  if (!m) return (
    <div>
      <div className="section-title">Metrics</div>
      <div className="section-desc">Loading metrics…</div>
      <div className="kpis" style={{ marginTop: 16 }} aria-hidden>
        {Array.from({ length: 4 }).map((_, i) => (
          <div className="kpi" key={i}><div className="skl" style={{ width: "60%" }} />
            <div className="skl" style={{ width: "40%", height: 20, marginTop: 8 }} /></div>
        ))}
      </div>
    </div>
  );

  const map = m.mapping || {};
  const prep = m.preparation || {};
  const del = m.delivery || { operations: 0, by_status: {}, by_type: {}, total_attempts: 0 };
  const model = m.model;
  const autoTotal = (map.rule || 0) + (map.model || 0);
  const needsHuman = (map.human || 0) + (map.unresolved_issues || 0) + (map.open_custom_field_proposals || 0);
  const resolved = autoTotal + (map.human || 0);
  const autoPct = resolved > 0 ? Math.round((autoTotal / resolved) * 100) : 0;

  const H = ({ children }: { children: React.ReactNode }) => (
    <h3 style={{ margin: "20px 0 8px", fontSize: 12, color: "var(--muted)", textTransform: "uppercase", letterSpacing: ".05em" }}>{children}</h3>
  );

  const timings = m.timings;
  const timingRows = timings ? ([
    { label: "Read files", ms: timings.stages.parse_stage, cls: "" },
    { label: "Match fields", ms: timings.stages.mapping, cls: "" },
    { label: "AI mapping", ms: timings.llm_mapping_ms, cls: "ai" },
    { label: "Clean & validate", ms: timings.stages.preparation, cls: "" },
    { label: "AI value mapping", ms: timings.llm_transforms_ms, cls: "ai" },
    { label: "Compare with target", ms: timings.stages.reconcile, cls: "" },
    { label: "Sync", ms: timings.stages.delivery, cls: "" },
  ].filter((r) => (r.ms ?? 0) > 0)) : [];
  const timingMax = Math.max(1, ...timingRows.map((r) => r.ms || 0));

  return (
    <div>
      <div className="section-title">Metrics</div>
      <div className="section-desc">Where the migration spent its time, where AI was used, how much was automatic,
        and what reached the target — every figure read from persisted state.</div>

      <H>At a glance</H>
      <div className="kpis">
        <KPI label="Auto-resolved" value={`${autoPct}%`} sub={`${autoTotal} of ${resolved} columns`}
          tone={autoPct >= 50 ? "green" : undefined} />
        <KPI label="Needing a human" value={needsHuman} tone={needsHuman > 0 ? "amber" : undefined} />
        <KPI label="AI calls" value={model?.calls ?? (map.model_proposal_requests || 0)}
          sub={model ? `${model.total_attempts} attempts` : undefined} />
        <KPI label="Synced" value={del.by_status?.SUCCEEDED || 0}
          tone={(del.by_status?.SUCCEEDED || 0) > 0 ? "green" : undefined} />
      </div>

      {timings && (timingRows.length > 0 || (timings.total_compute_ms ?? 0) > 0) && (
        <>
          <H>Migration timing</H>
          <div className="panel" style={{ padding: "12px 16px" }}>
            {timingRows.map((r) => (
              <div className="timing-row" key={r.label}>
                <span className="timing-label">{r.label}</span>
                <span className="timing-track"><span className={`timing-fill ${r.cls}`} style={{ width: `${Math.max(3, ((r.ms || 0) / timingMax) * 100)}%` }} /></span>
                <span className="timing-val">{formatDuration(r.ms)}</span>
              </div>
            ))}
            <div className="timing-row" style={{ borderTop: "1px solid var(--border)", marginTop: 6, paddingTop: 8 }}>
              <span className="timing-label"><b>Total compute time</b></span>
              <span />
              <span className="timing-val"><b>{formatDuration(timings.total_compute_ms)}</b></span>
            </div>
            <div className="timing-row">
              <span className="timing-label">Human review wait</span>
              <span className="timing-track"><span className="timing-fill wait" style={{ width: timings.human_review_wait_ms ? "100%" : "0%" }} /></span>
              <span className="timing-val">{timings.human_review_wait_measured ? formatDuration(timings.human_review_wait_ms) : "—"}</span>
            </div>
          </div>
          <div className="muted small" style={{ marginTop: 6 }}>Compute time is measured around each stage; human review wait is shown separately and is never counted as system time.</div>
        </>
      )}

      <H>AI usage</H>
      {(!model || model.calls === 0) ? (
        <>
          <div className="banner ok" style={{ marginBottom: 12 }}>
            No AI calls were required for this migration.</div>
          <div className="kpis">
            <KPI label="AI calls" value={0} />
            <KPI label="Input tokens" value={0} />
            <KPI label="Output tokens" value={0} />
            <KPI label="Estimated AI cost" value="$0.00" />
          </div>
        </>
      ) : (
        <>
          <div className="kpis">
            <KPI label="Model" value={(model.model_ids && model.model_ids.length
              ? model.model_ids.join(", ") : (model.adapter_kinds.join(", ") || "—"))} />
            <KPI label="AI calls" value={model.calls} sub={`${model.ok} ok · ${model.errors} error`}
              tone={model.errors > 0 ? "amber" : undefined} />
            <KPI label="Input tokens" value={model.prompt_tokens || 0} />
            <KPI label="Output tokens" value={model.completion_tokens || 0} />
            <KPI label="Total tokens" value={model.total_tokens || 0} />
            <KPI label="Total AI latency" value={formatDuration(model.latency_ms_total)} />
            <KPI label="Average response time" value={model.latency_ms_avg ? formatDuration(model.latency_ms_avg) : "—"} />
            <KPI label="Estimated AI cost" value={fmtUsd(model.estimated_cost_usd ?? 0)} />
          </div>
          {model.calls_detail && model.calls_detail.length > 0 && (
            <details className="tech" style={{ marginTop: 12 }}>
              <summary>Technical details — per-call model metrics ({model.adapter_kinds.join(", ") || "—"})</summary>
              <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Kind</th><th>Model</th><th>Status</th><th className="num">Attempts</th>
                    <th className="num">Latency</th><th className="num">Input tok</th>
                    <th className="num">Output tok</th><th className="num">Total tok</th>
                    <th className="num">Cols/values</th><th>When</th>
                  </tr>
                </thead>
                <tbody>
                  {model.calls_detail.map((c, i) => (
                    <tr key={i}>
                      <td>{c.kind === "mapping_proposal" ? "mapping" : c.kind === "transform_proposal" ? "transform" : c.kind}</td>
                      <td style={{ fontFamily: "var(--mono, monospace)", fontSize: 12 }}>{c.model_id}</td>
                      <td style={{ color: c.status === "ok" ? "var(--green, inherit)" : "var(--red, inherit)" }}>
                        {c.status}{c.error_category ? ` (${c.error_category})` : ""}</td>
                      <td className="num">{c.attempts}</td>
                      <td className="num">{c.latency_ms ? formatDuration(c.latency_ms) : "—"}</td>
                      <td className="num">{c.prompt_tokens ?? "—"}</td>
                      <td className="num">{c.completion_tokens ?? "—"}</td>
                      <td className="num">{c.total_tokens ?? "—"}</td>
                      <td className="num">{c.kind === "transform_proposal" ? (c.n_proposals ?? "—") : (c.n_columns ?? "—")}</td>
                      <td style={{ color: "var(--muted)", fontSize: 12 }}>
                        {c.created_at ? new Date(c.created_at).toLocaleTimeString() : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </div>
            </details>
          )}
        </>
      )}

      <H>Outcome — records</H>
      <div className="kpis">
        <KPI label="Employees found" value={prep.candidate_employees ?? "—"} />
        <KPI label="Ready" value={prep.eligible ?? "—"} tone={(prep.eligible ?? 0) > 0 ? "green" : undefined} />
        <KPI label="Blocked" value={prep.blocked ?? "—"} tone={(prep.blocked ?? 0) > 0 ? "amber" : undefined} />
        <KPI label="Excluded" value={prep.excluded ?? "—"} />
        <KPI label="Synced" value={del.by_status?.SUCCEEDED || 0} tone={(del.by_status?.SUCCEEDED || 0) > 0 ? "green" : undefined} />
        <KPI label="Human decisions" value={m.human_decisions || 0} />
      </div>

    </div>
  );
}
