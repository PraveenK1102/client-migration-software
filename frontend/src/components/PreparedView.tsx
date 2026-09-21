import { useState } from "react";
import type { Candidate, TargetSchema } from "../types";
import type { JobBundle } from "../viewtypes";
import { EmployeeDrawer } from "./EmployeeDrawer";
import { cx } from "./ui";

const ELIG: Record<string, string> = { eligible: "green", excluded: "red", blocked: "amber" };

export function PreparedView({ jobId, bundle, schema, jobStatus, onChanged }:
  { jobId: string; bundle: JobBundle; schema: TargetSchema | null; jobStatus?: string; onChanged?: () => void }) {
  const [sel, setSel] = useState<Candidate | null>(null);
  const cands = bundle.candidates;
  const val = (c: Candidate, f: string) => c.record?.[f]?.value ?? null;
  const collSummary = (c: Candidate) => Object.entries(c.collections ?? {}).filter(([, v]) => v?.length)
    .map(([k, v]) => `${schema?.collections.find((x) => x.key === k)?.label ?? k} ${v.length}`);

  return (
    <div>
      <div className="section-title">Employees</div>
      <div className="section-desc">Cleaned and validated employee records, with their related records and organization fields. Open a row for the full record, its source lineage, and version history.</div>

      <div className="panel" style={{ marginTop: 16 }}>
        <div className="table-wrap">
          <table className="data-table">
            <thead><tr><th>Employee ID</th><th>Name</th><th>Email</th><th>Department</th><th>Hire date</th><th>Related</th><th className="num">Org fields</th><th>Status</th><th className="num">Sources</th></tr></thead>
            <tbody>
              {cands.map((c) => {
                const cs = collSummary(c);
                return (
                  <tr key={c.id} className="clickable" onClick={() => setSel(c)}>
                    <td><span className="mono">{c.business_key ?? "—"}</span></td>
                    <td>{val(c, "full_name") ?? <span className="cell-empty">—</span>}</td>
                    <td className="small">{val(c, "work_email") ?? <span className="cell-empty">—</span>}</td>
                    <td>{val(c, "department") ?? <span className="cell-empty">—</span>}</td>
                    <td className="mono small">{val(c, "hire_date") ?? <span className="cell-empty">—</span>}</td>
                    <td className="small">{cs.length ? cs.map((s) => <span key={s} className="chip" style={{ marginRight: 4 }}>{s}</span>) : <span className="muted">—</span>}</td>
                    <td className="num">{c.custom_attributes?.length || <span className="muted">—</span>}</td>
                    <td><span className={cx("chip", ELIG[c.eligibility])}><span className="dot" />{c.eligibility === "eligible" ? "Ready" : c.eligibility === "excluded" ? "Excluded" : c.eligibility}</span></td>
                    <td className="num">{c.source_refs?.length ?? 0}</td>
                  </tr>
                );
              })}
              {cands.length === 0 && <tr><td colSpan={9} className="empty">No prepared employees yet.</td></tr>}
            </tbody>
          </table>
        </div>
      </div>

      {sel && <EmployeeDrawer jobId={jobId} candidate={sel} schema={schema} jobStatus={jobStatus} onChanged={onChanged} onClose={() => setSel(null)} />}
    </div>
  );
}
