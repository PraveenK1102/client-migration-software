import { useState } from "react";
import type { CustomFieldDefinition, CustomFieldProposal, ProposalDecisionBody, TargetSchema } from "../types";
import { TargetPathSelect } from "./ReviewCard";
import { Icon, cx } from "./ui";

const TYPES = ["string", "number", "boolean", "date", "enum", "multiselect"];
// Human type names (§I1/§R) — the stored value stays the same; only the label is friendly.
const TYPE_LABEL: Record<string, string> = {
  string: "Text", number: "Number", boolean: "Yes / No", date: "Date",
  enum: "Choice list", multiselect: "Multiple choice",
};

type Mode = "choose" | "target" | "create" | "existing" | "ignore";
const CHOICES: { key: Mode; title: string; hint: string }[] = [
  { key: "target", title: "Choose an existing target field", hint: "Map this column onto a standard employee field." },
  { key: "create", title: "Create an organization field", hint: "Add a new field configured only for this organization." },
  { key: "existing", title: "Use an existing organization field", hint: "Reuse a field already configured for this organization." },
  { key: "ignore", title: "Do not migrate this column", hint: "Keep it out of the target. Nothing is written." },
];

/** An unmapped source field: the consultant creates a TENANT custom field from the proposal (editable),
 *  maps it to an existing tenant custom field, maps it to a core/collection path, or ignores it.
 *  Nothing is created or discarded without this explicit decision. */
export function ProposalReviewWorkspace({ proposal, schema, tenantFields, tableLabel, onDecision }: {
  proposal: CustomFieldProposal;
  schema: TargetSchema | null;
  tenantFields: CustomFieldDefinition[];
  tableLabel: string;
  onDecision: (proposal: CustomFieldProposal, body: Omit<ProposalDecisionBody, "version">) => void | Promise<void>;
}) {
  const s = proposal.suggestion;
  const [label, setLabel] = useState(s.label);
  const [key, setKey] = useState(s.key);
  const [type, setType] = useState(s.type);
  const [options, setOptions] = useState((s.options ?? []).join(", "));
  const [required, setRequired] = useState(!!s.required);
  const [existing, setExisting] = useState<string>(tenantFields[0]?.id ?? "");
  const [targetPath, setTargetPath] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [mode, setMode] = useState<Mode>("choose");                 // §I1: decide the action FIRST
  const needsOptions = type === "enum" || type === "multiselect";

  const act = async (body: Omit<ProposalDecisionBody, "version">) => {
    setBusy(true); setErr(null);
    try { await onDecision(proposal, { ...body, note: note || null }); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  const approve = () => act({
    action: "approve", key: key.trim(), label: label.trim(), type,
    options: needsOptions ? options.split(",").map((o) => o.trim()).filter(Boolean) : null,
    required, multi_value: type === "multiselect",
  });

  return (
    <div>
      <div className="rv-head">
        <div>
          <div className="rv-title">We could not find a matching target field for "{proposal.source_header}"</div>
          <div className="rv-sub">
            <span>{tableLabel}</span>
            <span className="chip amber"><span className="dot" />No target field found</span>
            <span className="muted">{proposal.non_empty_count} non-empty value{proposal.non_empty_count === 1 ? "" : "s"}</span>
          </div>
        </div>
      </div>

      <p className="hint">This source column does not match a standard target field.</p>

      <div className="h5">Values in this column</div>
      <div className="samples">{proposal.observed_values.map((v, i) => <span key={i} className="chip sq">{v}</span>)}</div>

      {/* §I1: decide the ACTION first (cards) — the schema form is only revealed for "Create". */}
      <div className="h4">What would you like to do?</div>
      <div className="choice-cards">
        {CHOICES.map((ch) => (
          <button key={ch.key} type="button" className={cx("choice-card", mode === ch.key && "active")}
            disabled={busy} onClick={() => setMode(ch.key)} aria-pressed={mode === ch.key}>
            <span className="cc-radio">{mode === ch.key ? <Icon name="check" className="ico-xs" /> : null}</span>
            <span className="stack" style={{ gap: 2, textAlign: "left" }}>
              <span className="cc-title">{ch.title}</span>
              <span className="cc-hint">{ch.hint}</span>
            </span>
          </button>
        ))}
      </div>

      {mode === "target" && (
        <div className="choice-body">
          <div className="actions">
            <TargetPathSelect schema={schema} value={targetPath} onChange={setTargetPath} disabled={busy} includeCustom={false} />
            <button type="button" className="btn primary" disabled={busy || !targetPath} onClick={() => act({ action: "map_target", target_path: targetPath })}>Map to this field</button>
          </div>
        </div>
      )}

      {mode === "create" && (
        <div className="choice-body">
          <p className="hint">This field is configured only for this organization. Other organizations will not see it. Nothing is created until you confirm.</p>
          <div className="form-grid">
            <label htmlFor={`lbl-${proposal.id}`}>Label</label>
            <input id={`lbl-${proposal.id}`} type="text" value={label} onChange={(e) => setLabel(e.target.value)} disabled={busy} />
            <label htmlFor={`type-${proposal.id}`}>Type</label>
            <select id={`type-${proposal.id}`} value={type} onChange={(e) => setType(e.target.value)} disabled={busy}>
              {TYPES.map((t) => <option key={t} value={t}>{TYPE_LABEL[t] ?? t}</option>)}
            </select>
            {needsOptions && (<>
              <label htmlFor={`opt-${proposal.id}`}>Allowed values</label>
              <textarea id={`opt-${proposal.id}`} rows={2} value={options} onChange={(e) => setOptions(e.target.value)} disabled={busy} placeholder="comma-separated" />
            </>)}
            <label htmlFor={`req-${proposal.id}`}>Required?</label>
            <div><input id={`req-${proposal.id}`} type="checkbox" checked={required} onChange={(e) => setRequired(e.target.checked)} disabled={busy} /> <span className="muted small">every employee must have a value for this field</span></div>
          </div>
          <details className="tech" style={{ marginTop: 8 }}>
            <summary>Advanced</summary>
            <div className="form-grid" style={{ marginTop: 8 }}>
              <label htmlFor={`key-${proposal.id}`}>Field key</label>
              <input id={`key-${proposal.id}`} type="text" className="mono" value={key} onChange={(e) => setKey(e.target.value)} disabled={busy} />
            </div>
          </details>
          <div className="actions">
            <button type="button" className="btn primary" disabled={busy || !key.trim() || !label.trim() || (needsOptions && !options.trim())} onClick={approve}>
              <Icon name="plus" /> Create organization field
            </button>
          </div>
        </div>
      )}

      {mode === "existing" && (
        <div className="choice-body">
          <div className="actions">
            <select value={existing} onChange={(e) => setExisting(e.target.value)} disabled={busy || !tenantFields.length} aria-label="Existing organization field">
              {tenantFields.length === 0 && <option value="">no existing organization fields yet</option>}
              {tenantFields.map((d) => <option key={d.id} value={d.id}>{d.label}</option>)}
            </select>
            <button type="button" className="btn primary" disabled={busy || !existing} onClick={() => act({ action: "map_existing", definition_id: existing })}>Use this organization field</button>
          </div>
        </div>
      )}

      {mode === "ignore" && (
        <div className="choice-body">
          <div className="actions">
            <button type="button" className="btn primary" disabled={busy} onClick={() => act({ action: "ignore", reason: "Not needed in the target" })}>Confirm — do not migrate this column</button>
            <span className="muted small">This decision is recorded in the activity log.</span>
          </div>
        </div>
      )}

      {mode !== "choose" && (
        <div className="note-inline">
          <input type="text" placeholder="Decision note (optional)" value={note} onChange={(e) => setNote(e.target.value)} disabled={busy} aria-label="Decision note" />
        </div>
      )}
      {err && <div className="banner error" style={{ marginTop: 10 }}>{err}</div>}
    </div>
  );
}
