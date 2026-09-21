import { Fragment, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { Candidate, EmployeeVersion, TargetSchema, VersionCompare } from "../types";
import type { SourceRef } from "../viewtypes";
import { SourceRowDrawer } from "./SourceRowDrawer";
import { ConfirmDialog, Drawer, Icon, cx } from "./ui";

const ELIG: Record<string, string> = { eligible: "green", excluded: "red", blocked: "amber" };
const ORIGIN_LABEL: Record<string, string> = {
  existing_target: "Existing target baseline", migration: "Migration state", human: "Human decision", rollback: "Sync undone",
};

/** One target field's use within a single source record: human label, the source header it came from,
 *  and the raw→final change ONLY when the value was actually transformed. */
type FieldUse = { label: string; header: string | null; final: any; raw: string | null; changed: boolean };
type SrcRec = {
  key: string; table_id: string; row_number: number; original_filename: string; sheet_name: string | null;
  fields: FieldUse[]; relatedLabels: string[]; relatedCount: number; view: SourceRef;
};

/** Aggregate all provenance on a candidate into UNIQUE source records keyed by (table_id, row_number).
 *  A file/sheet/row is listed once regardless of how many fields came from it (addendum §B). */
function buildSourceRecords(candidate: Candidate, schema: TargetSchema | null): SrcRec[] {
  const labelFor = (name: string) => schema?.fields.find((x) => x.name === name)?.label ?? name;
  const collLabel = (key: string) => schema?.collections.find((c) => c.key === key)?.label ?? key;
  const rec = candidate.record ?? {};
  const recs = new Map<string, SrcRec>();
  const ensure = (p: { table_id: string; row_number: number; original_filename: string; sheet_name: string | null }): SrcRec => {
    const key = `${p.table_id}#${p.row_number}`;
    let r = recs.get(key);
    if (!r) {
      r = { key, table_id: p.table_id, row_number: p.row_number, original_filename: p.original_filename,
        sheet_name: p.sheet_name ?? null, fields: [], relatedLabels: [], relatedCount: 0,
        view: { table_id: p.table_id, row_number: p.row_number, header: null, col_index: null,
          original_filename: p.original_filename, sheet_name: p.sheet_name ?? null } };
      recs.set(key, r);
    }
    return r;
  };
  const addField = (label: string, fv: any) => {
    for (const p of fv?.provenance ?? []) {
      if (!p?.table_id || p.row_number == null) continue;
      const r = ensure(p);
      const finalStr = fv.value == null ? "" : String(fv.value);
      const raw = (p.raw != null && String(p.raw) !== "" && String(p.raw) !== finalStr) ? String(p.raw) : null;
      const fu: FieldUse = { label, header: p.header ?? null, final: fv.value, raw, changed: raw != null };
      const idx = r.fields.findIndex((x) => x.label === label);
      if (idx >= 0) { if (fu.changed && !r.fields[idx].changed) r.fields[idx] = fu; }
      else r.fields.push(fu);
      if (r.view.header == null && p.header) { r.view.header = p.header; r.view.col_index = p.col_index ?? null; }
    }
  };
  for (const name of (schema?.fields.map((f) => f.name) ?? Object.keys(rec))) addField(labelFor(name), rec[name]);
  for (const ca of candidate.custom_attributes ?? []) addField(ca.label, ca);   // human label, never the key
  for (const [ck, items] of Object.entries(candidate.collections ?? {})) {
    for (const it of items ?? []) {
      for (const s of it.sources ?? []) {
        if (!s?.table_id || s.row_number == null) continue;
        const r = ensure(s);
        r.relatedCount += 1;
        if (!r.relatedLabels.includes(collLabel(ck))) r.relatedLabels.push(collLabel(ck));
      }
    }
  }
  return [...recs.values()].sort(
    (a, b) => a.original_filename.localeCompare(b.original_filename) || a.row_number - b.row_number);
}

/** The raw source value that was cleaned into the final value, if (and only if) it changed (§H2). */
function cleanedFrom(fv: any): string | null {
  if (!fv) return null;
  const finalStr = fv.value == null ? "" : String(fv.value);
  for (const p of fv.provenance ?? []) {
    if (p?.raw != null && String(p.raw) !== "" && String(p.raw) !== finalStr) return String(p.raw);
  }
  return null;
}
export function fmtTime(ts: string): string {
  try { return new Date(ts).toLocaleString(undefined, { hour12: false }); } catch { return ts; }
}
const show = (v: any) => v == null || v === "" ? <span className="cell-empty">—</span> : Array.isArray(v) ? v.join(", ") : String(v);

function Sec({ title, count, open, children }: { title: string; count?: number | string; open?: boolean; children: React.ReactNode }) {
  return (
    <details className="sec" open={open}>
      <summary><span className="caret">▸</span>{title}{count != null && <span className="muted" style={{ textTransform: "none", letterSpacing: 0 }}>· {count}</span>}</summary>
      <div className="sec-b">{children}</div>
    </details>
  );
}

/** One employee: final record (grouped), structured collections, custom attributes, source lineage and the
 *  immutable version history with deterministic comparison (scalars, collection items, custom attributes). */
export function EmployeeDrawer({ jobId, candidate, schema, onClose, initialCompare, jobStatus, onChanged }: {
  jobId: string; candidate: Candidate; schema: TargetSchema | null; onClose: () => void;
  initialCompare?: { a: number; b: number } | null; jobStatus?: string; onChanged?: () => void;
}) {
  const [versions, setVersions] = useState<EmployeeVersion[] | null>(null);
  const [viewing, setViewing] = useState<EmployeeVersion | null>(null);
  const [compare, setCompare] = useState<VersionCompare | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [src, setSrc] = useState<SourceRef | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [delOpen, setDelOpen] = useState(false);
  // Locally applied admin edits so the drawer reflects a saved change immediately, before the parent
  // list refetches. Keyed by field name (core) or custom key.
  const [saved, setSaved] = useState<Record<string, string | null>>({});
  const rec = candidate.record ?? {};
  const val = (f: string) => (f in saved ? saved[f] : (rec?.[f]?.value ?? null));
  // An employee can be deleted from the target only once the migration has actually synced.
  const synced = !!jobStatus && ["migration_complete", "delivery_partial_failure",
    "stale_target_review_required", "rollback_in_progress", "rollback_complete",
    "rollback_partial_failure"].includes(jobStatus);
  const customVal = (ca: any) => (ca.key in saved ? saved[ca.key] : ca.value);

  const beginEdit = () => {
    const d: Record<string, string> = {};
    for (const f of schema?.fields ?? []) { const v = val(f.name); d[f.name] = v == null ? "" : String(v); }
    for (const ca of candidate.custom_attributes ?? []) { const v = customVal(ca); d[ca.key] = v == null ? "" : String(v); }
    setDraft(d); setMsg(null); setEditing(true);
  };

  const saveEdit = async () => {
    const fields: Record<string, string> = {};
    for (const f of schema?.fields ?? []) {
      const cur = val(f.name); const next = draft[f.name] ?? "";
      if (String(cur == null ? "" : cur) !== next) fields[f.name] = next;
    }
    for (const ca of candidate.custom_attributes ?? []) {
      const cur = customVal(ca); const next = draft[ca.key] ?? "";
      if (String(cur == null ? "" : cur) !== next) fields[ca.key] = next;
    }
    if (Object.keys(fields).length === 0) { setEditing(false); return; }
    setSaving(true); setMsg(null);
    try {
      const r = await api.editEmployee(jobId, candidate.id, { fields });
      setSaved((s) => ({ ...s, ...fields }));
      setEditing(false);
      setMsg({ tone: "ok", text: r.pushed_to_target
        ? `Saved as version ${r.version_no}. Update is being synced to the target.`
        : `Saved as version ${r.version_no}. It will sync with the next Sync.` });
      api.getEmployeeVersions(jobId, candidate.id).then(setVersions).catch(() => undefined);
      onChanged?.();
    } catch (e) {
      setMsg({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally { setSaving(false); }
  };

  const doDelete = async (note: string) => {
    await api.deleteEmployee(jobId, candidate.id, note || undefined);
    setDelOpen(false);
    setMsg({ tone: "ok", text: "Delete is being sent to the target. This employee will be removed there." });
    api.getEmployeeVersions(jobId, candidate.id).then(setVersions).catch(() => undefined);
    onChanged?.();
  };
  const groups = schema?.groups?.length ? schema.groups : [{ key: "all", label: "Record" }];
  const fieldsByGroup = useMemo(() => {
    const m: Record<string, string[]> = {};
    for (const f of schema?.fields ?? []) (m[f.group ?? "all"] ??= []).push(f.name);
    return m;
  }, [schema]);
  const view = (p: any) => setSrc({ table_id: p.table_id, row_number: p.row_number, header: p.header ?? null, col_index: p.col_index ?? null, original_filename: p.original_filename, sheet_name: p.sheet_name });

  useEffect(() => {
    let ok = true;
    setVersions(null); setViewing(null); setCompare(null);
    api.getEmployeeVersions(jobId, candidate.id).then((v) => { if (ok) setVersions(v); }).catch(() => { if (ok) setVersions([]); });
    if (initialCompare) api.compareVersions(jobId, candidate.id, initialCompare.a, initialCompare.b).then((c) => { if (ok) setCompare(c); }).catch(() => undefined);
    return () => { ok = false; };
  }, [jobId, candidate.id, initialCompare?.a, initialCompare?.b]);

  const doCompare = async (a: number, b: number) => {
    setViewing(null);
    try { setCompare(await api.compareVersions(jobId, candidate.id, a, b)); } catch { /* ignore */ }
  };
  const collEntries = Object.entries(candidate.collections ?? {}).filter(([, items]) => items?.length);
  const nItems = collEntries.reduce((n, [, items]) => n + items.length, 0);

  // Provenance aggregated into UNIQUE SOURCE RECORDS (table_id + row_number), not one row per field —
  // the addendum's Source-records model. Human labels, filename/sheet/row once, changed raw→final only.
  const sourceRecords = useMemo(
    () => buildSourceRecords(candidate, schema),
    [candidate, schema]);

  return (
    <Drawer wide title={`${val("full_name") ?? "Employee"} · EMP-${candidate.business_key ?? candidate.id}`}
      subtitle={<span className="dl-inline"><span className={cx("chip", ELIG[candidate.eligibility])}><span className="dot" />{candidate.eligibility === "eligible" ? "Ready" : candidate.eligibility === "excluded" ? "Excluded" : candidate.eligibility}{candidate.exclude_reason ? ` · ${candidate.exclude_reason}` : ""}</span>{val("work_email") && <span>{val("work_email")}</span>}</span>}
      onClose={onClose}>

      {msg && <div className={cx("banner", msg.tone === "ok" ? "ok" : "error")} style={{ marginBottom: 12 }}>{msg.text}</div>}

      <Sec title="Final record" open>
        <div className="row" style={{ justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
          <div className="row" style={{ gap: 8 }}>
            {!editing ? (
              <button type="button" className="btn sm" onClick={beginEdit}><Icon name="edit" className="ico-xs" /> Edit details</button>
            ) : (
              <>
                <button type="button" className="btn primary sm" disabled={saving} onClick={saveEdit}>{saving ? <span className="spin" /> : <Icon name="check" className="ico-xs" />} Save changes</button>
                <button type="button" className="btn ghost sm" disabled={saving} onClick={() => setEditing(false)}>Cancel</button>
              </>
            )}
          </div>
          {!editing && <button type="button" className="link-btn" onClick={() => setShowAll((s) => !s)}>{showAll ? "Hide empty fields" : "Show empty fields"}</button>}
        </div>
        {groups.map((g) => {
          const fs = (fieldsByGroup[g.key] ?? (g.key === "all" ? Object.keys(rec) : [])).filter((f) => editing || showAll || val(f) != null || schema?.fields.find((x) => x.name === f)?.required);
          if (!fs.length) return null;
          return (
            <div key={g.key} style={{ marginBottom: 16 }}>
              <div className="h5" style={{ margin: "0 0 7px" }}>{g.label}</div>
              <div className="dl">
                {fs.map((f) => {
                  const fv = rec[f];
                  const label = schema?.fields.find((x) => x.name === f)?.label ?? f;
                  const raw = cleanedFrom(fv);                       // only present when the value changed
                  const provs = (fv?.provenance ?? []).filter((p: any) => p?.table_id && p?.row_number != null);
                  const prov = provs[0];
                  return (
                    <Fragment key={f}>
                      <div className="k">{label}</div>
                      {editing && f !== "employee_id" ? (
                        <div className="dl-inline">
                          <input type="text" className="edit-inp" value={draft[f] ?? ""} placeholder="—"
                            onChange={(e) => setDraft((d) => ({ ...d, [f]: e.target.value }))} />
                        </div>
                      ) : editing && f === "employee_id" ? (
                        <div className="dl-inline">{show(val(f))} <span className="muted small">· identity, not editable</span></div>
                      ) : (
                        <div className="dl-inline">
                          {show(val(f))}
                          {raw != null && (
                            <span className="tag" title={`Source value  ${raw}\nFinal value  ${val(f)}\nChange  value normalized`}>Cleaned</span>
                          )}
                          {prov && (
                            <button type="button" className="link-btn" title={`View source for ${label}`}
                              onClick={() => view(prov)} aria-label={`View source for ${label}`}>
                              <Icon name="link" className="ico-xs" />
                              {provs.length > 1 && <span className="small" style={{ marginLeft: 2 }}>{provs.length} sources</span>}
                            </button>
                          )}
                        </div>
                      )}
                    </Fragment>
                  );
                })}
              </div>
            </div>
          );
        })}
      </Sec>

      <Sec title="Related records" count={nItems ? `${nItems} items` : "none"} open={nItems > 0}>
        {collEntries.length === 0 && <div className="muted small">No related records attached to this employee.</div>}
        {collEntries.map(([key, items]) => {
          const coll = schema?.collections.find((c) => c.key === key);
          const cols = coll?.fields.map((f) => f.name) ?? Object.keys(items[0]?.fields ?? {});
          return (
            <div key={key} style={{ marginBottom: 12 }}>
              <div className="h5" style={{ margin: "8px 0 4px" }}>{coll?.label ?? key} · {items.length}</div>
              <table className="items-table">
                <thead><tr>{cols.map((c) => <th key={c}>{coll?.fields.find((f) => f.name === c)?.label ?? c}</th>)}<th>Source</th></tr></thead>
                <tbody>
                  {items.map((it) => (
                    <tr key={it.identity_key} className={cx(`status-${it.status}`)}>
                      {cols.map((c) => <td key={c}>{show(it.fields?.[c]?.value)}</td>)}
                      <td>
                        {it.sources.map((s, i) => (
                          <div key={i} className="prov"><span>{s.original_filename}{s.sheet_name ? ` · ${s.sheet_name}` : ""} · row {s.row_number}</span>
                            <button type="button" className="link-btn" onClick={() => setSrc({ table_id: s.table_id, row_number: s.row_number, header: null, original_filename: s.original_filename, sheet_name: s.sheet_name })}><Icon name="link" className="ico-xs" /> View source</button></div>
                        ))}
                        {it.duplicates_collapsed > 0 && <div className="muted small">{it.duplicates_collapsed} exact duplicate row{it.duplicates_collapsed > 1 ? "s" : ""} collapsed</div>}
                        {it.status !== "resolved" && <div className="small" style={{ color: "var(--amber)" }}>{it.status}{it.reason ? ` · ${it.reason}` : ""}</div>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        })}
      </Sec>

      <Sec title="Organization fields" count={candidate.custom_attributes?.length || "none"} open={!!candidate.custom_attributes?.length}>
        {!candidate.custom_attributes?.length ? <div className="muted small">No organization fields for this employee.</div> : (
          <>
            <div className="dl">
              {candidate.custom_attributes.map((ca) => (
                <Fragment key={ca.key}>
                  <div className="k">{ca.label}</div>
                  {editing ? (
                    <div className="dl-inline">
                      <input type="text" className="edit-inp" value={draft[ca.key] ?? ""} placeholder="—"
                        onChange={(e) => setDraft((d) => ({ ...d, [ca.key]: e.target.value }))} />
                    </div>
                  ) : <div className="dl-inline">{show(customVal(ca))}</div>}
                </Fragment>
              ))}
            </div>
            {/* Internal field keys / definition IDs live under Technical details (§H4), never in the record. */}
            <details className="tech" style={{ marginTop: 10 }}>
              <summary>Technical details</summary>
              <div className="dl">
                {candidate.custom_attributes.map((ca) => (
                  <Fragment key={ca.key}>
                    <div className="k">{ca.label}</div>
                    <div className="mono small">{ca.key} · {ca.type}{ca.definition_id ? ` · ${ca.definition_id}` : ""}</div>
                  </Fragment>
                ))}
              </div>
            </details>
          </>
        )}
      </Sec>

      <Sec title="Source records" count={sourceRecords.length ? `${sourceRecords.length} record${sourceRecords.length === 1 ? "" : "s"}` : "none"}>
        <div className="section-desc" style={{ marginBottom: 8 }}>See which uploaded rows contributed to this employee.</div>
        {sourceRecords.length === 0 ? (
          <div className="muted small">No source rows recorded for this employee.</div>
        ) : (
          <>
            <div className="src-recs">
              {sourceRecords.map((r) => <SourceRecordCard key={r.key} rec={r} onView={() => setSrc(r.view)} />)}
            </div>
            <details className="tech" style={{ marginTop: 10 }}>
              <summary>Technical details</summary>
              <div className="dl">
                {sourceRecords.map((r) => (
                  <Fragment key={r.key}>
                    <div className="k">{r.original_filename}{r.sheet_name ? ` · ${r.sheet_name}` : ""} · row {r.row_number}</div>
                    <div className="mono small">{r.table_id} · row {r.row_number}</div>
                  </Fragment>
                ))}
              </div>
            </details>
          </>
        )}
      </Sec>

      <Sec title="Version history" count={versions ? versions.length : "…"} open>
        {versions === null ? <div className="muted small"><span className="spin" /> loading…</div>
          : versions.length === 0 ? <div className="muted small">No versions yet — run Compare with target to establish the baseline version.</div>
          : (
            <div className="ver-list">
              {versions.map((v) => (
                <div key={v.id} className={cx("ver", v.is_current && "current")}>
                  <div className="vh">
                    <span className="vno">v{v.version_no}</span>
                    {v.is_current && <span className="chip green"><span className="dot" />current</span>}
                    <span className="tag">{ORIGIN_LABEL[v.origin] ?? v.origin}</span>
                    <div className="spacer" />
                    <button type="button" className="btn ghost sm" onClick={() => { setViewing(viewing?.id === v.id ? null : v); setCompare(null); }}>View</button>
                    {v.version_no > 1 && <button type="button" className="btn sm" onClick={() => doCompare(v.version_no - 1, v.version_no)}>Compare with v{v.version_no - 1}</button>}
                  </div>
                  <div className="vmeta">
                    {v.change_reason}
                    {v.field_changes?.length ? ` · ${v.field_changes.map((c: any) => c.kind ? `${c.field} ${c.kind}` : `${c.field}: ${c.from ?? "∅"} → ${c.to ?? "∅"}`).join(", ")}` : ""}
                  </div>
                  <div className="vmeta">{fmtTime(v.created_at)} · {v.created_by}{v.target_revision != null ? ` · target rev ${v.target_revision}` : ""}{v.decision_note ? ` · note: ${v.decision_note}` : ""}</div>
                  {viewing?.id === v.id && <VersionView v={v} schema={schema} />}
                </div>
              ))}
            </div>
          )}

        {compare && <CompareView compare={compare} schema={schema} onClose={() => setCompare(null)} />}
      </Sec>

      {synced && (
        <div className="danger-zone">
          <div className="stack" style={{ gap: 2 }}>
            <strong>Delete this employee</strong>
            <span className="muted small">Removes the employee from the target system. Recorded as a version and reversible via Sync history.</span>
          </div>
          <button type="button" className="btn danger sm" onClick={() => setDelOpen(true)}><Icon name="trash" className="ico-xs" /> Delete employee</button>
        </div>
      )}

      {delOpen && (
        <ConfirmDialog title="Delete this employee from the target?" confirmLabel="Delete employee"
          busyLabel="Deleting…" onConfirm={() => doDelete("")} onClose={() => setDelOpen(false)}>
          <p>This sends a delete for <strong>{val("full_name") ?? `EMP-${candidate.business_key ?? candidate.id}`}</strong> to
            the target system. It is recorded as a new version in this employee's history.</p>
        </ConfirmDialog>
      )}

      {src && <SourceRowDrawer jobId={jobId} source={src} onClose={() => setSrc(null)} />}
    </Drawer>
  );
}

/** One unique source record: file · sheet · row, a contribution summary, a direct View-source-row
 *  action, and an optional human-labelled "Fields used" disclosure (raw→final only when changed). */
function SourceRecordCard({ rec, onView }: { rec: SrcRec; onView: () => void }) {
  const empN = rec.fields.length;
  const parts = [
    empN ? `Contributed ${empN} employee field${empN === 1 ? "" : "s"}` : null,
    rec.relatedCount
      ? `${rec.relatedCount} related record${rec.relatedCount === 1 ? "" : "s"}`
        + (rec.relatedLabels.length ? ` (${rec.relatedLabels.join(", ")})` : "")
      : null,
  ].filter(Boolean);
  return (
    <div className="src-rec">
      <div className="src-rec-h">
        <span className="src-rec-file">{rec.original_filename}</span>
        <span className="src-rec-loc">{rec.sheet_name ? `${rec.sheet_name} · ` : ""}Row {rec.row_number}</span>
        <span className="src-rec-actions">
          <button type="button" className="btn ghost sm" onClick={onView}>
            <Icon name="link" className="ico-xs" /> View source row
          </button>
        </span>
      </div>
      <div className="src-rec-sum">{parts.length ? parts.join(" · ") : "Contributed to this employee"}</div>
      {empN > 0 && (
        <details>
          <summary>Fields used ({empN})</summary>
          <div className="field-uses">
            {rec.fields.map((f, i) => (
              <div key={i} className="field-use">
                <div className="field-use-top">
                  <span className="field-use-label">{f.label}</span>
                  {f.header && <span className="field-use-from">← {f.header}</span>}
                </div>
                {f.changed && (
                  <div className="field-use-change">
                    <span className="old">{String(f.raw)}</span>
                    <span className="arrow">→</span>
                    <span>{show(f.final)}</span>
                    <span className="tag">Cleaned</span>
                  </div>
                )}
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

function VersionView({ v, schema }: { v: EmployeeVersion; schema: TargetSchema | null }) {
  const [tech, setTech] = useState(false);
  const fields = schema?.fields.map((f) => f.name) ?? Object.keys(v.snapshot).filter((k) => k !== "collections" && k !== "custom_attributes");
  const colls = Object.entries((v.snapshot?.collections ?? {}) as Record<string, any[]>).filter(([, items]) => items?.length);
  const custom: any[] = v.snapshot?.custom_attributes ?? [];
  return (
    <div style={{ marginTop: 10, borderTop: "1px solid var(--border)", paddingTop: 10 }}>
      <div className="dl">
        {fields.filter((f) => v.snapshot?.[f] != null).map((f) => (
          <Fragment key={f}><div className="k">{schema?.fields.find((x) => x.name === f)?.label ?? f}</div><div>{show(v.snapshot?.[f])}</div></Fragment>
        ))}
        {custom.map((c) => (<Fragment key={c.key}><div className="k">{c.key} <span className="tag custom">org field</span></div><div>{show(c.value)}</div></Fragment>))}
        {colls.map(([k, items]) => (
          <Fragment key={k}>
            <div className="k">{schema?.collections.find((c) => c.key === k)?.label ?? k}</div>
            <div>{items.map((it, i) => <div key={i} className="small">{Object.entries(it).map(([kk, vv]) => `${kk}: ${vv ?? "—"}`).join(" · ")}</div>)}</div>
          </Fragment>
        ))}
        <div className="k">parent</div><div>{v.parent_version_id ? "v" + (v.version_no - 1) : "—"}</div>
        <div className="k">record hash</div><div className="mono small">{v.record_hash.slice(0, 16)}…</div>
      </div>
      <button type="button" className="btn ghost sm" style={{ marginTop: 8 }} onClick={() => setTech((t) => !t)}>{tech ? "▾" : "▸"} Technical snapshot</button>
      {tech && <pre className="tech-json">{JSON.stringify(v.snapshot, null, 2)}</pre>}
    </div>
  );
}

export function CompareView({ compare, schema, onClose }: { compare: VersionCompare; schema: TargetSchema | null; onClose?: () => void }) {
  const [all, setAll] = useState(false);
  const rows = compare.fields.filter((f) => all || f.changed);
  const collChanged = compare.collections.filter((c) => c.kind !== "unchanged");
  const collByKey = collChanged.reduce<Record<string, typeof collChanged>>((m, c) => { (m[c.collection] ??= []).push(c); return m; }, {});
  const custom = compare.custom_attributes.filter((f) => all || f.changed);
  return (
    <div style={{ marginTop: 14 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <label className="fld">Comparing v{compare.a_version} → v{compare.b_version} · {compare.changed_fields.length} field{compare.changed_fields.length === 1 ? "" : "s"}, {collChanged.length} item{collChanged.length === 1 ? "" : "s"}, {compare.changed_custom_attributes.length} custom</label>
        <button type="button" className="link-btn" onClick={() => setAll((a) => !a)}>{all ? "Only changes" : "Show unchanged"}</button>
      </div>
      <table className="compare" style={{ marginTop: 6 }}>
        <thead><tr><th>Field</th><th>v{compare.a_version}</th><th>v{compare.b_version}</th><th>Change</th></tr></thead>
        <tbody>
          {rows.map((f) => (
            <tr key={f.field}>
              <td className="field">{schema?.fields.find((x) => x.name === f.field)?.label ?? f.field}</td>
              <td className={f.changed ? "cmp-old" : ""}>{show(f.a)}</td>
              <td className={f.changed ? "cmp-new" : ""}>{show(f.b)}</td>
              <td className="small">{f.changed ? f.kind : <span className="muted">unchanged</span>}</td>
            </tr>
          ))}
          {custom.map((f) => (
            <tr key={`c-${f.field}`}>
              <td className="field">custom_attributes.{f.field}</td>
              <td className={f.changed ? "cmp-old" : ""}>{show(f.a)}</td>
              <td className={f.changed ? "cmp-new" : ""}>{show(f.b)}</td>
              <td className="small">{f.changed ? f.kind : <span className="muted">unchanged</span>}</td>
            </tr>
          ))}
          {rows.length === 0 && custom.length === 0 && <tr><td colSpan={4} className="muted small">No scalar or custom-attribute changes.</td></tr>}
        </tbody>
      </table>
      {Object.entries(collByKey).map(([k, items]) => {
        const coll = schema?.collections.find((c) => c.key === k);
        return (
          <Sec key={k} title={`${coll?.label ?? k} items`} count={items.length} open>
            <table className="items-table">
              <thead><tr><th>Item</th><th>Change</th><th>v{compare.a_version}</th><th>v{compare.b_version}</th></tr></thead>
              <tbody>
                {items.map((c) => (
                  <tr key={c.identity_key}>
                    <td className="mono">{c.identity_key}</td>
                    <td><span className={cx("tag", c.kind === "added" ? "human" : c.kind === "removed" ? "unresolved" : "rule")}>{c.kind}</span>{c.changed_fields.length ? <div className="muted small">{c.changed_fields.join(", ")}</div> : null}</td>
                    <td className={c.kind === "removed" ? "cmp-removed" : c.kind === "changed" ? "cmp-old" : ""}>{c.a ? Object.entries(c.a).map(([kk, vv]) => `${kk}: ${vv ?? "—"}`).join(" · ") : <span className="cell-empty">—</span>}</td>
                    <td className={c.kind === "added" ? "cmp-added" : c.kind === "changed" ? "cmp-new" : ""}>{c.b ? Object.entries(c.b).map(([kk, vv]) => `${kk}: ${vv ?? "—"}`).join(" · ") : <span className="cell-empty">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Sec>
        );
      })}
      {onClose && <button type="button" className="btn ghost sm" style={{ marginTop: 8 }} onClick={onClose}>Close comparison</button>}
    </div>
  );
}
