import { Fragment, useMemo, useState } from "react";
import type { AuditEvent, Candidate, TargetSchema } from "../types";
import type { JobBundle, SourceRef } from "../viewtypes";
import { EmployeeDrawer } from "./EmployeeDrawer";
import { SourceRowDrawer } from "./SourceRowDrawer";
import { Drawer, Icon, ProvLine, cx } from "./ui";
import { orgLabel } from "../labels";

/** Human actor label (§M): System / AI + guardrails / You — never raw "system"/"model"/"human". */
function actorLabel(actor: string | null | undefined): string {
  const a = (actor || "").toLowerCase();
  if (a === "system") return "System";
  if (a === "model" || a === "ai") return "AI + guardrails";
  if (a === "human" || a === "user" || a === "consultant") return "You";
  return actor || "System";
}

const FILTERS: { key: string; label: string }[] = [
  { key: "all", label: "All" },
  { key: "ingestion", label: "Ingestion" },
  { key: "mapping", label: "Mapping" },
  { key: "preparation", label: "Preparation" },
  { key: "human", label: "Human decisions" },
  { key: "target", label: "Compare with target" },
  { key: "error", label: "Errors" },
];
const CAT_CLASS: Record<string, string> = {
  ingestion: "blue", mapping: "", preparation: "", human: "green", target: "blue", error: "red",
};

function fmtTime(ts: string): string {
  try { return new Date(ts).toLocaleString(undefined, { hour12: false }); } catch { return ts; }
}
function safeParse(v: any): any {
  if (typeof v !== "string") return v;
  const t = v.trim();
  if ((t.startsWith("{") && t.endsWith("}")) || (t.startsWith("[") && t.endsWith("]"))) {
    try { return JSON.parse(t); } catch { return v; }
  }
  return v;
}
function summary(a: AuditEvent): string {
  const src = a.source_ref || {};
  const parts: string[] = [];
  if (src.header) parts.push(src.header);
  if (src.business_key) parts.push(`EMP-${src.business_key}`);
  else if (src.candidate_key) parts.push(`EMP-${src.candidate_key}`);
  const af: any = a.after && typeof a.after === "object" ? a.after : {};
  if (af.target) parts.push(`→ ${af.target}`);
  if (af.key && (a.event_type.startsWith("custom_field"))) parts.push(`custom ${af.key}${af.type ? ` (${af.type})` : ""}`);
  if (af.action) parts.push(af.action.replace(/_/g, " "));
  if (af.value != null && typeof af.value !== "object") parts.push(`= ${af.value}`);
  if (af.winning_value != null) parts.push(`keeps ${af.winning_value}`);
  if (af.to_version) parts.push(af.from_version ? `v${af.from_version}→v${af.to_version}` : `v${af.to_version} (${(af.origin || "").replace(/_/g, " ")})`);
  if (af.tables != null) parts.push(`${af.tables} tables · ${af.rows ?? af.files ?? ""}`);
  if (af.ready_create != null) parts.push(`create ${af.ready_create} · update ${af.ready_update} · no-change ${af.no_change}`);
  if (af.suggested?.key) parts.push(`suggested ${af.suggested.key} (${af.suggested.type})`);
  return parts.filter(Boolean).join(" · ");
}

/** Plain-English label for an audit event_type (§17). Raw event ids stay under Technical details. */
const EVENT_LABEL: Record<string, string> = {
  ingested: "Files read", file_parsed: "File read", profiled: "Data understood",
  proposed: "AI suggested a field match", mapping_rule_accepted: "Field matched automatically",
  mapping_model_accepted: "Field matched by AI", issue_created: "Field match sent for review",
  issue_resolved: "Field mapping confirmed by consultant", mapping_review_resolved: "Field mapping confirmed by consultant",
  mapping_complete: "All fields matched", record_review_created: "Employee data issue sent for review",
  record_review_resolved: "Employee data decision made", preparation_complete: "Employees cleaned & validated",
  target_reconciled: "Compared with target", reconciliation_complete: "Compared with target",
  target_review_resolved: "Target conflict decision made", delivery_retry_scheduled: "Retry scheduled for a failed sync",
  migration_complete: "Migration completed",
};
function eventLabel(t: string): string {
  if (EVENT_LABEL[t]) return EVENT_LABEL[t];
  if (t.includes("rollback")) return "Sync undone";
  if (t.includes("retry")) return "Retry scheduled for a failed sync";
  if (t.startsWith("delivery") || t.includes("deliver")) return "Sync activity";
  if (t.includes("reconcil") || t.includes("target")) return "Compared with target";
  if (t.includes("record_review")) return t.includes("resolved") ? "Employee data decision made" : "Employee data issue sent for review";
  if (t.includes("issue") || t.includes("mapping_review")) return t.includes("resolved") ? "Field mapping confirmed by consultant" : "Field match sent for review";
  if (t.includes("custom_field")) return "Organization field decision";
  if (t.includes("prepar")) return "Employees cleaned & validated";
  if (t.includes("propos")) return "AI suggested a field match";
  if (t.includes("ingest") || t.includes("parsed")) return "Files read";
  if (t.includes("profil")) return "Data understood";
  if (t.includes("map")) return "Field matched";
  return t.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function KV({ k, v }: { k: string; v: any }) {
  if (v == null || v === "") return null;
  return (<><div className="k">{k}</div><div>{typeof v === "object" ? <span className="mono small">{JSON.stringify(v)}</span> : String(v)}</div></>);
}

export function AuditView({ jobId, bundle, schema, jobStatus, onChanged }: { jobId: string; bundle: JobBundle; schema: TargetSchema | null; jobStatus?: string; onChanged?: () => void }) {
  const [filter, setFilter] = useState("all");
  const [open, setOpen] = useState<AuditEvent | null>(null);
  const [tech, setTech] = useState(false);
  const [emp, setEmp] = useState<{ cand: Candidate; compare: { a: number; b: number } | null } | null>(null);
  const [src, setSrc] = useState<SourceRef | null>(null);
  const events = useMemo(() => [...bundle.audit].reverse(), [bundle.audit]);
  const shown = filter === "all" ? events : events.filter((e) => e.category === filter);
  const counts = useMemo(() => {
    const m: Record<string, number> = {};
    for (const e of events) m[e.category || "mapping"] = (m[e.category || "mapping"] || 0) + 1;
    return m;
  }, [events]);
  const byKey = useMemo(() => {
    const bk: Record<string, Candidate> = {}, bi: Record<string, Candidate> = {};
    for (const c of bundle.candidates) { if (c.business_key) bk[c.business_key] = c; bi[c.id] = c; }
    return { bk, bi };
  }, [bundle.candidates]);

  const empFor = (a: AuditEvent): Candidate | null => {
    const s = a.source_ref || {};
    return (s.candidate_id && byKey.bi[s.candidate_id]) || (s.business_key && byKey.bk[s.business_key]) ||
      (s.candidate_key && byKey.bk[s.candidate_key]) || null;
  };
  const view = (p: any) => setSrc({ table_id: p.table_id, row_number: p.row_number, header: p.header ?? null, col_index: p.col_index ?? null, original_filename: p.original_filename, sheet_name: p.sheet_name });

  return (
    <div>
      <div className="section-title">Activity log</div>
      <div className="section-desc">A plain-English history of what happened in this migration: files read, fields matched, decisions you made, and changes synced to the target. Technical event ids are available on each row.</div>

      <div className="toolbar">
        <div className="filters">
          {FILTERS.map((f) => (
            <button key={f.key} type="button" className={cx("filter", filter === f.key && "active")} onClick={() => setFilter(f.key)}>
              {f.label}{f.key !== "all" && counts[f.key] ? ` (${counts[f.key]})` : ""}
            </button>
          ))}
        </div>
      </div>

      <div className="panel">
        <div className="table-wrap">
          <table className="data-table">
            <thead><tr><th style={{ width: 150 }}>Time</th><th>Event</th><th>Actor</th><th>Details</th></tr></thead>
            <tbody>
              {shown.map((a) => (
                <tr key={a.id} className="clickable" onClick={() => { setOpen(a); setTech(false); }}>
                  <td className="mono small muted">{fmtTime(a.ts)}</td>
                  <td><span className={cx("chip", CAT_CLASS[a.category || ""])}><span className="dot" />{eventLabel(a.event_type)}</span></td>
                  <td className="small">{actorLabel(a.actor)}</td>
                  <td className="small muted">{summary(a) || a.reason || "—"}</td>
                </tr>
              ))}
              {shown.length === 0 && <tr><td colSpan={4} className="empty">No events in this category.</td></tr>}
            </tbody>
          </table>
        </div>
      </div>

      {open && (() => {
        const cand = empFor(open);
        const s = open.source_ref || {};
        const before = safeParse(open.before);
        const after = safeParse(open.after);
        const ao = after && typeof after === "object" ? after : null;
        const bo = before && typeof before === "object" ? before : null;
        const note = (ao && (ao.decision_note || ao.note)) || null;
        const action = ao ? ao.action : null;
        const prov: any[] = Array.isArray(s.provenance) ? s.provenance : [];
        const isVersion = !!(ao && (ao.from_version || ao.to_version));
        const isRollback = isVersion && ao.origin === "rollback";
        // A real "before" is a non-empty object with actual prior field values — NOT a bare version
        // marker ({version_no}) and not simply absent. Only THIS case is a genuine update worth a
        // Before/After diff (§3). When there is no real before, the event created/recorded something —
        // it never had a "before" to show, so no Before column and no diff-yellow decoration.
        const hasRealBefore = !!bo && !("version_no" in bo && Object.keys(bo).length === 1);
        return (
          <Drawer title={eventLabel(open.event_type)}
            subtitle={<><span className={cx("chip", CAT_CLASS[open.category || ""])}><span className="dot" />{FILTERS.find((f) => f.key === open.category)?.label ?? open.category}</span> <span className="muted small">{fmtTime(open.ts)}</span></>}
            onClose={() => setOpen(null)}>
            <div className="dl-label">Event details</div>
            <div className="dl" style={{ marginBottom: 12 }}>
              <KV k="Actor" v={actorLabel(open.actor)} />
              {cand && (<><div className="k">Employee</div><div className="dl-inline">
                <span>{cand.record?.full_name?.value ?? "—"} <span className="muted">· EMP-{cand.business_key ?? "?"} · {cand.record?.work_email?.value ?? ""}</span></span>
                <button type="button" className="link-btn" onClick={() => setEmp({ cand, compare: null })}><Icon name="link" className="ico-xs" /> Open employee</button>
              </div></>)}
              {!cand && (s.business_key || s.candidate_key) && <KV k="Employee" v={`EMP-${s.business_key ?? s.candidate_key}`} />}
              <KV k="Field" v={s.field} />
              <KV k="Source column" v={s.header} />
              {s.tenant_id && <KV k="Organization" v={orgLabel(s.tenant_id)} />}
              <KV k="Action" v={action ? String(action).replace(/_/g, " ") : null} />
              {ao?.winning_side && <KV k="Winning value" v={`${ao.winning_side === "target" ? "existing target" : "incoming"}: ${ao.winning_value ?? "(blank)"}`} />}
              {ao?.key && open.event_type.startsWith("custom_field") && <KV k="Organization field" v={`${ao.label ?? ao.key}${ao.type ? ` · ${ao.type}` : ""}${ao.options ? ` [${ao.options.join(", ")}]` : ""}`} />}
              <KV k="Decision note" v={note} />
              <KV k="Reason" v={open.reason && open.reason !== note ? open.reason : null} />
              {prov.length > 0 && (<><div className="k">Source evidence</div><div>{prov.map((p, i) => <div key={i}><ProvLine p={p} onView={view} /></div>)}</div></>)}
              {!prov.length && (s.original_filename || s.sheet_name) && (
                <><div className="k">Source</div><div className="small">{s.original_filename}{s.sheet_name ? ` · ${s.sheet_name}` : ""}{s.row_number != null ? ` · row ${s.row_number}` : ""}</div></>
              )}
              <KV k="Work item" v={open.work_item_id} />
              <KV k="Issue / review id" v={open.issue_id} />
            </div>

            {isVersion && (() => {
              // CREATE (v1, no prior version): "Employee created" — no Before column, no diff yellow.
              // UPDATE (v2+) / ROLLBACK: a real prior version exists — show it as a genuine transition,
              // with yellow reserved for the fields that actually changed (§3).
              const isCreate = !ao.from_version;
              const headline = isRollback
                ? (ao.deleted_from_target
                    ? "Restored — removed from target (undoes the create)"
                    : `Restored Version ${ao.restores_version_no ?? "?"}`)
                : isCreate ? "Employee created (Version 1)" : `Updated to Version ${ao.to_version}`;
              const changed: string[] = ao.changed_fields || [];
              return (
                <>
                  {/* Yellow is reserved for an actual attention-worthy change (§3): a rollback undoes
                      something and deserves emphasis; a routine create/update does not. */}
                  <div className={cx("banner", isRollback ? "warn" : "info")} style={{ marginBottom: 12 }}>
                    <div className="dl-inline"><b>{headline}</b>
                      {cand && ao.from_version && (
                        <button type="button" className="link-btn"
                          onClick={() => setEmp({ cand, compare: { a: ao.from_version, b: ao.to_version } })}>
                          <Icon name="link" className="ico-xs" /> Open comparison
                        </button>
                      )}
                    </div>
                    {!isCreate && changed.length > 0 && (
                      <div className="muted small" style={{ marginTop: 4 }}>
                        {changed.length} field{changed.length === 1 ? "" : "s"} changed: {changed.join(", ")}
                      </div>
                    )}
                  </div>
                  {cand && isCreate && (<>
                    <div className="dl-label">Record created</div>
                    <div className="dl" style={{ marginBottom: 12 }}>
                      <div className="k">Employee ID</div><div>{cand.business_key ?? "—"}</div>
                      <div className="k">Full name</div><div>{cand.record?.full_name?.value ?? "—"}</div>
                      <div className="k">Work email</div><div>{cand.record?.work_email?.value ?? "—"}</div>
                      <div className="k">Hire date</div><div>{cand.record?.hire_date?.value ?? "—"}</div>
                    </div>
                  </>)}
                </>
              );
            })()}

            {!isVersion && (bo || ao) && (() => {
              const skip = new Set(["note", "decision_note", "action", "actor", "provenance", "suggested", "observed_values", "options"]);
              const allKeys = Array.from(new Set([...(bo ? Object.keys(bo) : []), ...(ao ? Object.keys(ao) : [])]))
                .filter((k) => !skip.has(k) && ((bo && bo[k] != null) || (ao && ao[k] != null)));
              if (!allKeys.length) return null;
              if (!hasRealBefore) {
                // CREATE-shaped: nothing to compare against. Show the recorded fields plainly — no
                // "Before" column, no yellow (there is no change to emphasize, only a new fact).
                return (<>
                  <div className="dl-label">Recorded values</div>
                  <div className="dl" style={{ marginBottom: 12 }}>
                    {allKeys.map((k) => {
                      const av = ao ? ao[k] : undefined;
                      return (<Fragment key={k}>
                        <div className="k">{k}</div>
                        <div>{av == null ? <span className="cell-empty">—</span> : typeof av === "object" ? <span className="mono small">{JSON.stringify(av)}</span> : String(av)}</div>
                      </Fragment>);
                    })}
                  </div>
                </>);
              }
              // Real update: show ONLY the fields that actually changed, old → new (§3).
              const changedKeys = allKeys.filter((k) => JSON.stringify(bo?.[k]) !== JSON.stringify(ao?.[k]));
              if (!changedKeys.length) return null;
              return (<>
                <div className="dl-label">What changed</div>
                <table className="compare">
                  <thead><tr><th>Field</th><th>Old value</th><th>New value</th></tr></thead>
                  <tbody>
                    {changedKeys.map((k) => {
                      const bv = bo ? bo[k] : undefined, av = ao ? ao[k] : undefined;
                      return (
                        <tr key={k} className="diff">
                          <td className="field">{k}</td>
                          <td>{bv == null ? <span className="cell-empty">—</span> : typeof bv === "object" ? <span className="mono small">{JSON.stringify(bv)}</span> : String(bv)}</td>
                          <td>{av == null ? <span className="cell-empty">—</span> : typeof av === "object" ? <span className="mono small">{JSON.stringify(av)}</span> : String(av)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </>);
            })()}

            <div style={{ marginTop: 14 }}>
              <button type="button" className="btn ghost sm" onClick={() => setTech((t) => !t)}>{tech ? "▾" : "▸"} Technical details</button>
              {tech && (
                <pre className="tech-json">{JSON.stringify({
                  id: open.id, event_type: open.event_type, category: open.category, actor: open.actor,
                  ts: open.ts, issue_id: open.issue_id, work_item_id: open.work_item_id,
                  source_ref: safeParse(open.source_ref), before, after,
                  schema_version: open.schema_version, policy_version: open.policy_version,
                }, null, 2)}</pre>
              )}
            </div>
          </Drawer>
        );
      })()}

      {emp && <EmployeeDrawer jobId={jobId} candidate={emp.cand} schema={schema} onClose={() => setEmp(null)} initialCompare={emp.compare} jobStatus={jobStatus} onChanged={onChanged} />}
      {src && <SourceRowDrawer jobId={jobId} source={src} onClose={() => setSrc(null)} />}
    </div>
  );
}
