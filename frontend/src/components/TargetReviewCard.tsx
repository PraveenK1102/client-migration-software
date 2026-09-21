import { useState } from "react";
import type { Candidate, TargetAction, TargetReviewIssue, TargetSchema } from "../types";
import type { SourceRef } from "../viewtypes";
import { EmployeeDetails } from "./RecordReviewCard";
import { ConflictTag, Icon, ProvLine, cx } from "./ui";

const FIELDS = ["employee_id", "full_name", "work_email", "department", "hire_date", "contract_start_date"];

function label(schema: TargetSchema | null, path: string | null): string {
  if (!path) return "Field";
  const f = schema?.fields.find((x) => x.name === path) ?? schema?.custom_fields.find((x) => x.path === path);
  if (f) return f.label;
  const m = path.match(/^([a-z_]+)\[\]$/);
  if (m) return schema?.collections.find((c) => c.key === m[1])?.label ?? m[1];
  return path;
}

/** Compact multi-party comparison (Incoming / Target by ID / Target by email); differing cells are marked
 *  with a background AND an explicit "differs" label. */
function Comparison({ cols, fields, schema }: { cols: { title: string; rec: any }[]; fields: string[]; schema: TargetSchema | null }) {
  return (
    <table className="compare">
      <thead><tr><th>Field</th>{cols.map((c) => <th key={c.title}>{c.title}{c.rec?.revision != null && <span className="muted"> · rev {c.rec.revision}</span>}</th>)}</tr></thead>
      <tbody>
        {fields.map((f) => {
          const vals = cols.map((c) => c.rec?.[f] ?? null);
          const present = vals.filter((v) => v != null && v !== "");
          const differs = new Set(present.map((v) => String(v).toLowerCase())).size > 1;
          return (
            <tr key={f}>
              <td className="field">{label(schema, f)}</td>
              {vals.map((v, i) => (
                <td key={i} className={cx(differs && "differs")}>
                  {v == null || v === "" ? <span className="cell-empty">—</span> : String(v)}
                  {differs && i === 0 && <span className="lbl-differs">differs</span>}
                </td>
              ))}
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export function TargetReviewWorkspace({ issue, candidate, schema, onDecision, onViewSource }: {
  issue: TargetReviewIssue;
  candidate: Candidate | null;
  schema: TargetSchema | null;
  onDecision: (issue: TargetReviewIssue, action: TargetAction, note: string | null) => void | Promise<void>;
  onViewSource: (ref: SourceRef) => void;
}) {
  const [note, setNote] = useState("");
  const [choice, setChoice] = useState<TargetAction | "">("");
  const [busy, setBusy] = useState(false);
  const a = issue.affected || {};
  const incoming = a.incoming || {};
  const target = a.target || a.target_by_id || null;
  const act = async (action: TargetAction) => {
    setBusy(true);
    try { await onDecision(issue, action, note || null); } finally { setBusy(false); }
  };
  const can = (o: TargetAction) => issue.options.includes(o);
  const view = (p: any) => onViewSource({ table_id: p.table_id, row_number: p.row_number, header: p.header, col_index: p.col_index, original_filename: p.original_filename, sheet_name: p.sheet_name });
  const fl = label(schema, issue.field);
  const incomingProv: any[] = issue.field ? (candidate?.record?.[issue.field]?.provenance ?? []) : [];
  const t = issue.issue_type;

  const noteRow = (
    <div className="note-inline">
      <input type="text" placeholder="Decision note (optional)" value={note} onChange={(e) => setNote(e.target.value)} disabled={busy} aria-label="Decision note" />
    </div>
  );

  return (
    <div>
      <div className="rv-head">
        <div>
          <div className="rv-title">{incoming.full_name || `Employee ${issue.business_key ?? issue.candidate_id}`}</div>
          <div className="rv-sub">
            {incoming.work_email && <span>{incoming.work_email}</span>}
            <span className="chip sq">EMP-{issue.business_key ?? issue.candidate_id}</span>
            {target?.employee_id && <span className="muted">confirmed target {target.employee_id} · revision {target.revision}</span>}
            <span className="muted">Incoming vs existing target</span>
          </div>
        </div>
      </div>

      {(t === "value_conflict") && (
        <>
          <div className="h5">Employee details</div>
          <EmployeeDetails
            candidate={candidate ?? { id: issue.candidate_id, business_key: issue.business_key, eligibility: "eligible", exclude_reason: null, issue_ids: [], source_refs: [],
              record: Object.fromEntries(Object.entries(incoming).filter(([k]) => k !== "collections" && k !== "custom_attributes").map(([k, v]) => [k, { value: v, status: "resolved", rule: null, reason: null, provenance: [] }])),
              collections: {}, custom_attributes: [] }}
            schema={schema} conflictField={issue.field}
            conflictValue={<span>Target: <b>{issue.target_value}</b> · Incoming: <b>{issue.incoming_value}</b></span>} />
          <div className="h4">{fl} needs review</div>
          <p className="hint">The confirmed target already holds a different value. Decide which value wins; nothing is written to the target here.</p>
          <div className="choices" role="radiogroup" aria-label={`${fl} resolution`}>
            {can("keep_existing") && (
              <label className={cx("choice", choice === "keep_existing" && "sel")}>
                <input type="radio" name={issue.id} checked={choice === "keep_existing"} onChange={() => setChoice("keep_existing")} disabled={busy} />
                <div className="cv">Keep the existing target value <span className="sub">— {issue.target_value ?? "(blank)"}</span></div>
                <div className="cs"><span>confirmed target employee {target?.employee_id} · revision {target?.revision}</span></div>
              </label>
            )}
            {can("use_incoming") && (
              <label className={cx("choice", choice === "use_incoming" && "sel")}>
                <input type="radio" name={issue.id} checked={choice === "use_incoming"} onChange={() => setChoice("use_incoming")} disabled={busy} />
                <div className="cv">Use incoming value <span className="sub">— {issue.incoming_value ?? "(blank)"}</span></div>
                <div className="cs">{incomingProv.length ? incomingProv.map((p, k) => <ProvLine key={k} p={p} onView={view} />) : <span>from the prepared incoming record</span>}</div>
              </label>
            )}
          </div>
          <div className="actions">
            <button type="button" className="btn primary" disabled={busy || !choice} onClick={() => choice && act(choice)}>Confirm</button>
            {can("exclude") && <button type="button" className="btn ghost" disabled={busy} onClick={() => act("exclude")}>Exclude employee</button>}
          </div>
          {noteRow}
          <div className="client-conf" style={{ marginTop: 10 }}><Icon name="warn" className="ico-xs" /> Client confirmation may be required for this factual conflict.</div>
        </>
      )}

      {t === "collection_item_conflict" && (() => {
        const coll = a.collection as string;
        const cl = schema?.collections.find((c) => c.key === coll);
        const fields = cl?.fields.map((f) => f.name) ?? Object.keys(a.incoming_item || {});
        const changed: string[] = a.changed_fields ?? [];
        return (
          <>
            <div className="h4">{cl?.label ?? coll} item needs review</div>
            <p className="hint">The item <span className="mono">{a.identity_key}</span> already exists in the confirmed target with a different {changed.join(", ")}.</p>
            <table className="compare">
              <thead><tr><th>Field</th><th>Existing in target</th><th>Incoming</th></tr></thead>
              <tbody>
                {fields.map((f) => (
                  <tr key={f}>
                    <td className="field">{f}</td>
                    <td className={cx(changed.includes(f) && "differs")}>{a.target_item?.[f] ?? <span className="cell-empty">—</span>}{changed.includes(f) && <span className="lbl-differs">differs</span>}</td>
                    <td className={cx(changed.includes(f) && "differs")}>{a.incoming_item?.[f] ?? <span className="cell-empty">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="choices" role="radiogroup" aria-label="Item resolution">
              {can("keep_existing") && (
                <label className={cx("choice", choice === "keep_existing" && "sel")}>
                  <input type="radio" name={issue.id} checked={choice === "keep_existing"} onChange={() => setChoice("keep_existing")} disabled={busy} />
                  <div className="cv">Keep current target item</div>
                  <div className="cs"><span>confirmed target employee {target?.employee_id} · revision {target?.revision}</span></div>
                </label>
              )}
              {can("use_incoming") && (
                <label className={cx("choice", choice === "use_incoming" && "sel")}>
                  <input type="radio" name={issue.id} checked={choice === "use_incoming"} onChange={() => setChoice("use_incoming")} disabled={busy} />
                  <div className="cv">Use incoming item values</div>
                </label>
              )}
            </div>
            <div className="actions">
              <button type="button" className="btn primary" disabled={busy || !choice} onClick={() => choice && act(choice)}>Confirm</button>
              {can("exclude") && <button type="button" className="btn ghost" disabled={busy} onClick={() => act("exclude")}>Exclude employee</button>}
            </div>
            {noteRow}
          </>
        );
      })()}

      {t === "id_email_mismatch" && (
        <>
          <div className="h4">Identity conflict <ConflictTag label="Identity" /></div>
          <p className="hint">The employee ID matches target {a.target_by_id?.employee_id} but the work email belongs to target {a.target_by_email?.employee_id}. No safe automatic choice exists; the conservative action is to exclude this employee and confirm with the client.</p>
          <Comparison schema={schema} fields={FIELDS} cols={[
            { title: "Incoming", rec: incoming },
            { title: "Target by employee ID", rec: a.target_by_id },
            { title: "Target by work email", rec: a.target_by_email },
          ]} />
          <div className="actions">
            {can("exclude") && <button type="button" className="btn primary" disabled={busy} onClick={() => act("exclude")}>Exclude employee</button>}
          </div>
          {noteRow}
          <div className="client-conf" style={{ marginTop: 10 }}><Icon name="warn" className="ico-xs" /> Client confirmation may be required.</div>
        </>
      )}

      {t === "email_owned_by_other" && (
        <>
          <div className="h4">Work email already owned <ConflictTag /></div>
          <p className="hint">This new employee's work email belongs to confirmed target employee {a.target_by_email?.employee_id}. Only exclusion is offered.</p>
          <Comparison schema={schema} fields={FIELDS} cols={[
            { title: "Incoming", rec: incoming },
            { title: "Target owner of this email", rec: a.target_by_email },
          ]} />
          <div className="actions">
            {can("exclude") && <button type="button" className="btn primary" disabled={busy} onClick={() => act("exclude")}>Exclude employee</button>}
          </div>
          {noteRow}
        </>
      )}
    </div>
  );
}
