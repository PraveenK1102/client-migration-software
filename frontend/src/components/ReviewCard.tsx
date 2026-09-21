import { useState } from "react";
import type { DecisionAction, ReviewIssue, TargetSchema } from "../types";
import { cx, Icon } from "./ui";

/** Grouped target-field selector: core fields / structured records / organization fields. */
export function TargetPathSelect({ schema, value, onChange, disabled, id, includeCustom = true }: {
  schema: TargetSchema | null; value: string; onChange: (v: string) => void; disabled?: boolean; id?: string; includeCustom?: boolean;
}) {
  return (
    <select id={id} value={value} onChange={(e) => onChange(e.target.value)} disabled={disabled} aria-label="Target field">
      <option value="">— choose a target field —</option>
      <optgroup label="Employee fields">
        {schema?.fields.map((f) => <option key={f.path} value={f.path}>{f.label}</option>)}
      </optgroup>
      {schema?.collections.map((c) => (
        <optgroup key={c.key} label={`Related records · ${c.label}`}>
          {c.fields.map((f) => <option key={f.path} value={f.path}>{f.label}</option>)}
        </optgroup>
      ))}
      {includeCustom && !!schema?.custom_fields.length && (
        <optgroup label="Organization fields">
          {schema?.custom_fields.map((f) => <option key={f.path} value={f.path}>{f.label}</option>)}
        </optgroup>
      )}
    </select>
  );
}

/** Human label for a target path (e.g. "work_email" -> "Work email"). */
function targetLabel(schema: TargetSchema | null, path: string | null | undefined): string {
  if (!path) return "";
  const f = schema?.fields.find((x) => x.path === path)
    ?? schema?.custom_fields.find((x) => x.path === path)
    ?? schema?.collections.flatMap((c) => c.fields).find((x) => x.path === path);
  return f?.label ?? path;
}

export function MappingReviewWorkspace({ issue, schema, tableLabel, onDecision }: {
  issue: ReviewIssue;
  schema: TargetSchema | null;
  tableLabel: string;
  onDecision: (issue: ReviewIssue, action: DecisionAction, correctedTarget: string | null, reason: string) => void;
}) {
  const candidates = issue.candidate_target_fields.length ? issue.candidate_target_fields : (schema?.target_paths ?? []);
  const [target, setTarget] = useState<string>(issue.proposed_target_field ?? candidates[0] ?? "");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const es = issue.evidence_summary || {};
  const samples: string[] = (es.sample_values as string[]) ?? [];
  // A within-table conflict is a DIFFERENT shape of ambiguity from "this column could mean several
  // things": here several SOURCE COLUMNS all independently look right for the SAME target, so the
  // system holds all of them for review rather than silently letting one auto-accept and win. The
  // backend already names every column in the conflict (evidence_summary.within_table_conflict,
  // including this one) — name the OTHERS explicitly instead of a generic "not certain enough".
  const conflictHeaders: string[] = Array.isArray(es.within_table_conflict) ? es.within_table_conflict : [];
  const otherConflicting = conflictHeaders.filter((h) => h !== issue.source_header);

  const act = async (action: DecisionAction, correctedTarget: string | null) => {
    setBusy(true);
    try { await onDecision(issue, action, correctedTarget, reason); } finally { setBusy(false); }
  };

  const proposedLabel = targetLabel(schema, issue.proposed_target_field);
  const alts = candidates.filter((c) => c !== issue.proposed_target_field);

  return (
    <div>
      <div className="rv-head">
        <div>
          <div className="rv-title">
            {otherConflicting.length > 0
              ? `${conflictHeaders.length} columns in this file could all be ${proposedLabel}`
              : "This column could match more than one target field"}
          </div>
          <div className="rv-sub"><span className="chip sq">{issue.source_header}</span><span className="muted">{tableLabel} · {issue.affected_non_empty_rows} non-empty rows</span></div>
        </div>
      </div>

      {otherConflicting.length > 0 && (
        <div className="note" style={{ marginBottom: 14 }}>
          <Icon name="warn" className="ico-xs" />
          <span>
            <b>{otherConflicting.join(", ")}</b> in this same file also look{otherConflicting.length === 1 ? "s" : ""} like <b>{proposedLabel}</b>.
            The system will not guess which one is correct and silently overwrite the other{otherConflicting.length === 1 ? "" : "s"} —
            confirm this column, or choose a different field for it below.
          </span>
        </div>
      )}

      <div className="h5">What the system found</div>
      <div className="dl">
        <div className="k">Source column</div><div className="mono">{issue.source_header}</div>
        <div className="k">Best AI match</div><div>{issue.proposed_target_field ? <span className="dl-inline"><b>{proposedLabel}</b></span> : <span className="cell-empty">none</span>}</div>
        {otherConflicting.length > 0 && (
          <><div className="k">Also matches this field</div>
            <div className="dl-inline">{otherConflicting.map((h) => <span key={h} className="chip sq">{h}</span>)}</div></>
        )}
        {alts.length > 0 && <><div className="k">Other possible fields</div><div className="dl-inline">{alts.slice(0, 8).map((c) => <span key={c} className="chip blue">{targetLabel(schema, c)}</span>)}{alts.length > 8 && <span className="muted small">+{alts.length - 8} more</span>}</div></>}
        {samples.length > 0 && (<><div className="k">Sample values</div><div className="samples">{samples.map((s, i) => <span key={i} className="chip sq">{s}</span>)}</div></>)}
        <div className="k">Why it needs you</div>
        <div className="small">
          {otherConflicting.length > 0
            ? `${otherConflicting.join(" and ")} in this file also look${otherConflicting.length === 1 ? "s" : ""} like ${proposedLabel}, so the system cannot safely auto-pick one.`
            : es.model_ambiguity_reason || "The match is not certain enough to apply automatically."}
        </div>
      </div>

      <div className="h4">Choose the target field</div>
      <p className="hint">Only real target fields can be chosen — the system never invents a field.</p>
      <div className="actions">
        {issue.proposed_target_field && (
          <button type="button" className="btn primary" disabled={busy} onClick={() => act("approve", null)}>
            Use {proposedLabel}
          </button>
        )}
        <TargetPathSelect schema={schema} value={target} onChange={setTarget} disabled={busy} id={`tp-${issue.id}`} />
        <button type="button" className={cx("btn", !issue.proposed_target_field && "primary")} disabled={busy || !target} onClick={() => act("correct", target)}>Choose {targetLabel(schema, target) || "another field"}</button>
        <button type="button" className="btn ghost" disabled={busy} onClick={() => act("ignore", null)}>Do not migrate this column</button>
      </div>
      <div className="note-inline">
        <input type="text" placeholder="Decision note (optional)" value={reason} aria-label="Decision note"
          onChange={(e) => setReason(e.target.value)} disabled={busy} />
      </div>
    </div>
  );
}
