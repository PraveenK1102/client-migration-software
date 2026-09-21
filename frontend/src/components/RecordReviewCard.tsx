import { useMemo, useState } from "react";
import type { Candidate, Provenance, RecordAction, RecordIssue, TargetSchema } from "../types";
import type { SourceRef } from "../viewtypes";
import { ConflictTag, Icon, ProvLine, cx } from "./ui";

export type RecordDecisionExtra = {
  value?: string | null;
  convention?: string | null;
  value_map?: Record<string, string> | null;
  scope?: Record<string, any> | null;
  candidate_id?: string | null;
  reason?: string | null;
  note?: string | null;
};

// Factual conflicts a consultant generally cannot resolve without the client.
const FACTUAL = new Set(["value_conflict", "missing_required", "invalid_value", "collection_conflict"]);
const IDENTITY_FIELDS = ["employee_id", "full_name", "work_email", "hire_date", "department"];

function fieldLabel(schema: TargetSchema | null, path: string | null | undefined): string {
  if (!path) return "Field";
  const f = schema?.fields.find((x) => x.name === path) ?? schema?.custom_fields.find((x) => x.path === path);
  if (f) return f.label;
  const m = path.match(/^([a-z_]+)\[\]$/);
  if (m) return schema?.collections.find((c) => c.key === m[1])?.label ?? m[1];
  return path.replace(/^custom_attributes\./, "").replace(/_/g, " ");
}

function collLabel(schema: TargetSchema | null, key: string): string {
  return schema?.collections.find((c) => c.key === key)?.label ?? key;
}

function itemLabel(schema: TargetSchema | null, key: string): string {
  const l = collLabel(schema, key);
  return l.endsWith("s") ? l.slice(0, -1) : l;
}

const toRef = (p: Provenance | any): SourceRef => ({
  table_id: p.table_id, row_number: p.row_number, header: p.header ?? null, col_index: p.col_index ?? null,
  original_filename: p.original_filename, sheet_name: p.sheet_name,
});

/** Employee identity + details as a flat definition list; ONLY the field under review is marked. */
export function EmployeeDetails({ candidate, schema, conflictField, conflictValue }: {
  candidate: Candidate | null; schema: TargetSchema | null; conflictField?: string | null; conflictValue?: React.ReactNode;
}) {
  const rec = candidate?.record ?? {};
  const fields = useMemo(() => {
    const core = schema?.fields.map((f) => f.name) ?? IDENTITY_FIELDS;
    const withValue = core.filter((f) => IDENTITY_FIELDS.includes(f) || rec[f]?.value != null || f === conflictField);
    return withValue;
  }, [schema, rec, conflictField]);
  if (!candidate) return null;
  return (
    <div className="dl" aria-label="Employee details">
      {fields.map((f) => {
        const isC = f === conflictField;
        const v = rec[f]?.value;
        return (
          <div key={f} style={{ display: "contents" }}>
            <div className={cx("k", isC && "conflict")}>{fieldLabel(schema, f)}</div>
            <div className={cx(isC && "conflict")}>
              {isC ? (
                <span className="dl-inline">{conflictValue}<ConflictTag /></span>
              ) : v == null || v === "" ? <span className="cell-empty">—</span> : Array.isArray(v) ? v.join(", ") : String(v)}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function RecordReviewWorkspace({ issue, candidate, related = [], schema, onDecision, onViewSource }: {
  issue: RecordIssue;
  candidate: Candidate | null;
  related?: Candidate[];
  schema: TargetSchema | null;
  onDecision: (issue: RecordIssue, action: RecordAction, extra?: RecordDecisionExtra) => void | Promise<void>;
  onViewSource: (ref: SourceRef) => void;
}) {
  const t = issue.issue_type;
  const options = issue.options || [];
  const detail: any[] = issue.affected?.options_detail || [];
  const [sel, setSel] = useState<string>(() =>
    t === "ambiguous_date" ? options[0]?.iso ?? "" :
    t === "collection_conflict" ? "" :
    options[0] != null && typeof options[0] !== "object" ? String(options[0]) : "");
  const [text, setText] = useState("");
  const [itemField, setItemField] = useState<string>(issue.affected?.problems?.[0]?.field ?? "");
  const [note, setNote] = useState("");
  const [emailEdits, setEmailEdits] = useState<Record<string, string>>({});
  const [enumMap, setEnumMap] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  const rec = candidate?.record ?? {};
  const val = (f: string) => rec?.[f]?.value ?? null;
  const cval = (c: Candidate, f: string) => c.record?.[f]?.value ?? null;
  const dateProv: Provenance | undefined = issue.affected?.provenance?.[0];
  const view = (p: any) => onViewSource(toRef(p));

  const act = async (action: RecordAction, extra?: RecordDecisionExtra) => {
    setBusy(true);
    try { await onDecision(issue, action, { note: note || null, ...extra }); } finally { setBusy(false); }
  };
  const sourcesFor = (v: string) => (detail.find((d) => String(d.value) === String(v))?.sources ?? []);
  const fl = fieldLabel(schema, issue.field);
  const optWord = (n: number) => ["", "One", "Two", "Three", "Four"][n] ?? String(n);

  const conflictValue = t === "value_conflict"
    ? options.map(String).join(" / ")
    : t === "ambiguous_date" ? String(issue.affected?.raw ?? val(issue.field ?? ""))
    : t === "unknown_enum" || t === "invalid_value" ? String(issue.affected?.raw ?? "")
    : t === "missing_required" ? <span className="cell-empty">missing</span>
    : undefined;
  const conflictField = ["value_conflict", "ambiguous_date", "unknown_enum", "invalid_value", "missing_required"].includes(t)
    ? issue.field : null;

  const noteRow = (
    <div className="note-inline">
      <input type="text" placeholder="Decision note (optional)" value={note} onChange={(e) => setNote(e.target.value)} disabled={busy} aria-label="Decision note" />
    </div>
  );
  const clientConf = FACTUAL.has(t) && (
    <div className="client-conf" style={{ marginTop: 10 }}><Icon name="warn" className="ico-xs" /> Client confirmation may be required for this factual conflict.</div>
  );
  const excludeBtn = t !== "shared_email" && t !== "orphan_child_row" && (
    <button type="button" className="btn ghost" disabled={busy} onClick={() => act("exclude")}>Exclude employee</button>
  );

  return (
    <div>
      <div className="rv-head">
        <div>
          <div className="rv-title">
            {t === "shared_email" ? "Shared work email"
              : t === "orphan_child_row" ? "Unattached rows"
              : t === "unknown_enum" ? `${fl} values`
              : val("full_name") || (issue.candidate_key ? `Employee ${issue.candidate_key}` : "Employee record")}
          </div>
          <div className="rv-sub">
            {t !== "shared_email" && t !== "unknown_enum" && val("work_email") && <span>{val("work_email")}</span>}
            {t !== "shared_email" && t !== "unknown_enum" && issue.candidate_key && <span className="chip sq">{issue.candidate_key}</span>}
            {t !== "shared_email" && t !== "unknown_enum" && candidate && <span className={cx("chip", candidate.eligibility === "eligible" ? "green" : "amber")}><span className="dot" />{candidate.eligibility === "eligible" ? "ready" : candidate.eligibility}</span>}
          </div>
        </div>
      </div>

      {candidate && t !== "orphan_child_row" && t !== "shared_email" && t !== "unknown_enum" && (
        <>
          <div className="h5">Employee details</div>
          <EmployeeDetails candidate={candidate} schema={schema} conflictField={conflictField} conflictValue={conflictValue} />
        </>
      )}

      {t === "value_conflict" && (
        <>
          <div className="h4">{fl} needs review</div>
          <p className="hint">{optWord(options.length)} source records disagree. Choose the value that should be kept.</p>
          <div className="choices" role="radiogroup" aria-label={`${fl} options`}>
            {options.map((o: any, i: number) => (
              <label key={i} className={cx("choice", sel === String(o) && "sel")}>
                <input type="radio" name={issue.id} checked={sel === String(o)} onChange={() => setSel(String(o))} disabled={busy} />
                <div className="cv">{String(o)}</div>
                <div className="cs">{sourcesFor(o).map((s: any, k: number) => <ProvLine key={k} p={s} onView={view} />)}</div>
              </label>
            ))}
          </div>
          <div className="actions">
            <button type="button" className="btn primary" disabled={busy || !sel} onClick={() => act("select", { value: sel })}>Confirm selection</button>
            {excludeBtn}
          </div>
          {noteRow}{clientConf}
        </>
      )}

      {t === "ambiguous_date" && (() => {
        // This is a COLUMN-scoped review (one decision covers the whole column, order §M3C), so
        // `issue.affected.raw`/a single value never applies here — show the actual evidence instead:
        // the reason already states how many distinct values/rows are affected, and the column's own
        // sample values give the human enough real context to judge the convention (order: "show the
        // possible format for this file").
        const dateSamples: { value: string; count: number }[] = issue.affected?.distinct_values ?? [];
        return (
          <>
            <div className="h4">{fl} needs review</div>
            <p className="hint">{issue.reason || "This column's dates are valid under two conventions and the source does not establish one."}</p>
            {dateSamples.length > 0 && (
              <>
                <div className="k" style={{ marginBottom: 4 }}>Values in this column</div>
                <div className="samples" style={{ marginBottom: 12 }}>
                  {dateSamples.slice(0, 12).map((d, i) => <span key={i} className="chip sq mono">{d.value}</span>)}
                  {dateSamples.length > 12 && <span className="muted small">+{dateSamples.length - 12} more</span>}
                </div>
              </>
            )}
            <div className="k" style={{ marginBottom: 4 }}>Possible formats</div>
            <div className="choices" role="radiogroup" aria-label={`${fl} interpretations`}>
              {options.map((o: any, i: number) => (
                <label key={i} className={cx("choice", sel === o.iso && "sel")}>
                  <input type="radio" name={issue.id} checked={sel === o.iso} onChange={() => setSel(o.iso)} disabled={busy} />
                  <div className="cv">{o.iso} <span className="sub">({o.meaning === "DMY" ? "day / month / year" : "month / day / year"})</span></div>
                  {i === 0 && dateProv && <div className="cs"><ProvLine p={dateProv} onView={view} /></div>}
                </label>
              ))}
            </div>
            <div className="actions">
              <button type="button" className="btn primary" disabled={busy || !sel} onClick={() => act("correct", { value: sel })}>Confirm date</button>
              {excludeBtn}
            </div>
            {noteRow}
          </>
        );
      })()}

      {t === "unknown_enum" && (() => {
        const distinct: { value: string; count: number }[] =
          (issue.affected?.distinct_values ?? (issue.scope?.unmapped_values ?? []).map((v: any) => ({ value: v, count: 0 })))
            .map((d: any) => ({ value: String(d.value), count: d.count ?? 0 }));
        const allowed = options.map(String);
        const optional = !!issue.field && !schema?.fields.find((f) => f.name === issue.field)?.required;
        const allChosen = distinct.length > 0 && distinct.every((d) => enumMap[d.value]);
        return (
          <>
            <div className="h4">Some source values need a target value</div>
            <p className="hint">
              The source uses <b>{fl.toLowerCase()}</b> values that do not exactly match the target system.
              Choose the target value for each one.
            </p>
            {allowed.length > 0 && (
              <>
                <div className="k" style={{ marginBottom: 4 }}>Allowed values for {fl}</div>
                <div className="samples" style={{ marginBottom: 12 }}>
                  {allowed.map((o) => <span key={o} className="chip blue">{o}</span>)}
                </div>
              </>
            )}
            <table className="value-map">
              <thead><tr><th>Source value</th><th>Rows</th><th>Target value</th></tr></thead>
              <tbody>
                {distinct.map((d) => (
                  <tr key={d.value}>
                    <td className="mono">{d.value}</td>
                    <td className="muted">{d.count || "—"}</td>
                    <td>
                      <select value={enumMap[d.value] ?? ""} disabled={busy}
                        onChange={(e) => setEnumMap({ ...enumMap, [d.value]: e.target.value })} aria-label={`Target value for ${d.value}`}>
                        <option value="">Choose…</option>
                        {allowed.map((o) => <option key={o} value={o}>{o}</option>)}
                      </select>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="hint muted">This decision applies to every employee with the same source value.</p>
            <div className="actions">
              <button type="button" className="btn primary" disabled={busy || !allChosen}
                onClick={() => act("map_values", { value_map: enumMap })}>Apply these values</button>
              {optional && <button type="button" className="btn" disabled={busy} onClick={() => act("null")}>Do not migrate this column value</button>}
            </div>
            {noteRow}
          </>
        );
      })()}

      {(t === "missing_required" || t === "invalid_value") && (
        <>
          <div className="h4">{fl} needs review</div>
          <p className="hint">{issue.reason}</p>
          {dateProv && <div style={{ marginBottom: 8 }}><ProvLine p={dateProv} onView={view} /></div>}
          <div className="actions">
            <input type="text" placeholder={`corrected ${fl.toLowerCase()}`} value={text} aria-label={`Corrected ${fl}`}
              onChange={(e) => setText(e.target.value)} disabled={busy} style={{ minWidth: 260 }} />
            <button type="button" className="btn primary" disabled={busy || !text} onClick={() => act("correct", { value: text })}>Apply correction</button>
            {excludeBtn}
          </div>
          {noteRow}{clientConf}
        </>
      )}

      {t === "shared_email" && (() => {
        const people = related;
        const dupEmail = people[0]?.record?.work_email?.value
          ?? (issue.reason?.match(/[\w.+-]+@[\w.-]+\.[a-z]{2,}/i)?.[0]) ?? "this work email";
        const ids = (issue.affected?.candidates ?? []).map((c: any) => c.business_key).filter(Boolean);
        return (
          <>
            <div className="h4">{people.length === 2 ? "Two employees share the same work email" : "Employees share the same work email"}</div>
            <p className="hint">Work email must be unique before these records can be synced.</p>
            <div className="dup-value" aria-label="Duplicated work email">{String(dupEmail)}</div>
            <p className="hint">
              Decide which employee should receive a corrected email, or exclude an incorrect employee.
              If you correct or exclude one record, the other employee keeps this email.
            </p>
            {people.map((c) => {
              const name = String(cval(c, "full_name") ?? `Employee ${c.business_key}`);
              const email = cval(c, "work_email");
              const dept = cval(c, "department");
              const desig = cval(c, "designation");
              const ref = c.source_refs?.[0];
              const draft = emailEdits[c.id];
              const editing = draft !== undefined;
              const valid = editing && /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test((draft || "").trim());
              return (
                <div key={c.id} className="person-card">
                  <div className="person-head">
                    <div className="stack" style={{ gap: 2, minWidth: 0 }}>
                      <div className="person-name">{name}</div>
                      <div className="person-meta">
                        <span className="chip sq">{c.business_key}</span>
                        {email && <span>{String(email)}</span>}
                        {dept && <span>{String(dept)}</span>}
                        {desig && <span>{String(desig)}</span>}
                      </div>
                    </div>
                    {ref && <button type="button" className="link-btn" onClick={() => view(ref)}><Icon name="link" className="ico-xs" /> View source row</button>}
                  </div>
                  {!editing ? (
                    <div className="actions">
                      <button type="button" className="btn" disabled={busy} onClick={() => setEmailEdits({ ...emailEdits, [c.id]: "" })}>Change this employee's email</button>
                      <button type="button" className="btn ghost" disabled={busy}
                        onClick={() => { if (window.confirm(`Exclude ${name} (${c.business_key}) from this migration? Their record will not be synced.`)) act("exclude", { candidate_id: c.id }); }}>
                        Exclude employee
                      </button>
                    </div>
                  ) : (
                    <div className="actions">
                      <input type="email" placeholder="new work email" value={draft} disabled={busy} style={{ minWidth: 240 }}
                        aria-label={`New work email for ${name}`} onChange={(e) => setEmailEdits({ ...emailEdits, [c.id]: e.target.value })} />
                      <button type="button" className="btn primary" disabled={busy || !valid} onClick={() => act("correct", { candidate_id: c.id, value: (draft || "").trim() })}>Save correction</button>
                      <button type="button" className="btn ghost" disabled={busy} onClick={() => { const cp = { ...emailEdits }; delete cp[c.id]; setEmailEdits(cp); }}>Cancel</button>
                      {editing && (draft || "").length > 0 && !valid && <span className="field-err">Enter a valid email address</span>}
                    </div>
                  )}
                </div>
              );
            })}
            {people.length === 0 && <p className="hint">Affected employees: {ids.join(", ") || "unavailable"}.</p>}
            {noteRow}
          </>
        );
      })()}

      {t === "collection_conflict" && (() => {
        const coll = issue.affected?.collection as string;
        const cf: string[] = issue.affected?.conflict_fields ?? [];
        const ident = issue.affected?.identity_key;
        return (
          <>
            <div className="h4">{itemLabel(schema, coll)} needs review</div>
            <p className="hint">
              The same {itemLabel(schema, coll).toLowerCase()} <span className="mono">{ident}</span> appears in {options.length} source rows with a different {cf.join(", ")}. Choose the variant to keep.
            </p>
            <div className="choices" role="radiogroup" aria-label="Variants">
              {options.map((o: any) => {
                const od = detail.find((d) => String(d.value) === String(o.variant_key));
                const values = Object.entries(o.values || {}).map(([k, v]) => `${k}: ${v ?? "—"}`).join(" · ");
                return (
                  <label key={o.variant_key} className={cx("choice", sel === o.variant_key && "sel")}>
                    <input type="radio" name={issue.id} checked={sel === o.variant_key} onChange={() => setSel(o.variant_key)} disabled={busy} />
                    <div className="cv">{values}</div>
                    <div className="cs">{(od?.sources ?? o.sources ?? []).map((s: any, k: number) => <ProvLine key={k} p={s} onView={view} />)}</div>
                  </label>
                );
              })}
            </div>
            <div className="actions">
              <button type="button" className="btn primary" disabled={busy || !sel} onClick={() => act("select", { value: sel })}>Confirm selection</button>
              <button type="button" className="btn" disabled={busy} onClick={() => act("drop_item")}>Drop this item</button>
              {excludeBtn}
            </div>
            {noteRow}{clientConf}
          </>
        );
      })()}

      {t === "collection_item_invalid" && (() => {
        const coll = issue.affected?.collection as string;
        const item = issue.affected?.item ?? {};
        const problems: any[] = issue.affected?.problems ?? [];
        return (
          <>
            <div className="h4">{itemLabel(schema, coll)} item needs review</div>
            <p className="hint">{problems.map((p) => `${p.field}: ${p.reason}`).join("; ")}</p>
            <div className="dl" style={{ marginBottom: 8 }}>
              {Object.entries(item).map(([k, v]) => {
                const bad = problems.some((p) => p.field === k);
                return (<div key={k} style={{ display: "contents" }}>
                  <div className={cx("k", bad && "conflict")}>{k}</div>
                  <div className={cx(bad && "conflict")}>{v == null || v === "" ? <span className="cell-empty">—</span> : String(v)}{bad && <> <ConflictTag label="Invalid" /></>}</div>
                </div>);
              })}
            </div>
            {(issue.affected?.provenance ?? []).slice(0, 4).map((p: any, k: number) => <div key={k}><ProvLine p={p} onView={view} /></div>)}
            <div className="actions">
              <select value={itemField} onChange={(e) => setItemField(e.target.value)} disabled={busy} aria-label="Item field to correct">
                {problems.map((p) => <option key={p.field} value={p.field}>{p.field}</option>)}
              </select>
              <input type="text" placeholder="corrected value" value={text} onChange={(e) => setText(e.target.value)} disabled={busy} style={{ minWidth: 200 }} aria-label="Corrected item value" />
              <button type="button" className="btn primary" disabled={busy || !text || !itemField}
                onClick={() => act("correct", { value: text, scope: { collection: coll, identity_key: issue.affected?.identity_key, item_field: itemField } })}>Apply correction</button>
              <button type="button" className="btn" disabled={busy} onClick={() => act("drop_item")}>Drop this item</button>
              {excludeBtn}
            </div>
            {noteRow}
          </>
        );
      })()}

      {t === "orphan_child_row" && (() => {
        const rows: any[] = issue.affected?.rows ?? [];
        return (
          <>
            <div className="h4">Child rows reference an unknown employee</div>
            <p className="hint">{issue.reason}</p>
            <table className="items-table">
              <thead><tr><th>Row</th><th>Employee key in file</th><th>Collection</th><th>Values</th><th></th></tr></thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.row_number}>
                    <td className="mono">{r.row_number}</td>
                    <td className="mono">{r.employee_id_raw ?? <span className="cell-empty">—</span>}</td>
                    <td>{collLabel(schema, r.collection)}</td>
                    <td className="small">{Object.entries(r.values || {}).map(([k, v]) => `${k}: ${v ?? "—"}`).join(" · ")}</td>
                    <td><button type="button" className="link-btn" onClick={() => onViewSource({ table_id: issue.affected?.table_id, row_number: r.row_number, header: null, original_filename: issue.affected?.original_filename, sheet_name: issue.affected?.sheet_name })}><Icon name="link" className="ico-xs" /> View source</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="actions">
              <button type="button" className="btn primary" disabled={busy} onClick={() => act("exclude")}>Exclude these rows</button>
            </div>
            {noteRow}
          </>
        );
      })()}
    </div>
  );
}
