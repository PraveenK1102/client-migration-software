import { useEffect, useMemo, useRef, useState } from "react";
import type {
  Candidate, CustomFieldProposal, DecisionAction, ProposalDecisionBody, RecordAction, RecordIssue, ReviewIssue,
  TargetAction, TargetReviewIssue, TargetSchema,
} from "../types";
import type { JobBundle, SourceRef } from "../viewtypes";
import { RecordReviewWorkspace, type RecordDecisionExtra } from "./RecordReviewCard";
import { TargetReviewWorkspace } from "./TargetReviewCard";
import { MappingReviewWorkspace } from "./ReviewCard";
import { ProposalReviewWorkspace } from "./ProposalReviewWorkspace";
import { SourceRowDrawer } from "./SourceRowDrawer";
import { Icon, cx } from "./ui";

type Item =
  | { kind: "record"; id: string; issue: RecordIssue; candidate: Candidate | null; related: Candidate[] }
  | { kind: "target"; id: string; issue: TargetReviewIssue; candidate: Candidate | null }
  | { kind: "mapping"; id: string; issue: ReviewIssue }
  | { kind: "proposal"; id: string; proposal: CustomFieldProposal };

/** Plain-language review-type label + stable group key + workflow order (no "enum"/"candidate"). */
function reviewType(it: Item): { key: string; label: string; order: number } {
  if (it.kind === "mapping") return { key: "mapping", label: "Field match to confirm", order: 1 };
  if (it.kind === "proposal") return { key: "proposal", label: "No target field found", order: 2 };
  if (it.kind === "target") return { key: "target", label: "Target conflict", order: 8 };
  switch (it.issue.issue_type) {
    case "shared_email": return { key: "shared_email", label: "Shared work email", order: 3 };
    case "unknown_enum": return { key: "unknown_enum", label: "Value needs a target match", order: 4 };
    case "ambiguous_date": return { key: "ambiguous_date", label: "Date needs confirming", order: 5 };
    case "value_conflict": return { key: "value_conflict", label: "Conflicting values", order: 6 };
    case "missing_required": case "invalid_value": return { key: "employee_data", label: "Employee data issue", order: 7 };
    case "collection_conflict": case "collection_item_invalid": return { key: "related_record", label: "Related record issue", order: 7 };
    case "orphan_child_row": return { key: "orphan", label: "Unattached rows", order: 7 };
    default: return { key: it.issue.issue_type, label: "Employee data issue", order: 7 };
  }
}

function fieldLabel(schema: TargetSchema | null, path: string | null | undefined): string {
  if (!path) return "";
  const f = schema?.fields.find((x) => x.name === path) ?? schema?.custom_fields.find((x) => x.path === path);
  if (f) return f.label;
  const m = path.match(/^([a-z_]+)\[\]$/);
  if (m) return schema?.collections.find((c) => c.key === m[1])?.label ?? m[1];
  return path;
}

export function ReviewsView({ jobId, bundle, schema, onDecision, onRecordDecision, onTargetDecision, onProposalDecision }: {
  jobId: string;
  bundle: JobBundle;
  schema: TargetSchema | null;
  onDecision: (i: ReviewIssue, a: DecisionAction, t: string | null, r: string) => void;
  onRecordDecision: (i: RecordIssue, a: RecordAction, e?: RecordDecisionExtra) => void;
  onTargetDecision: (i: TargetReviewIssue, a: TargetAction, note: string | null) => void;
  onProposalDecision: (p: CustomFieldProposal, body: Omit<ProposalDecisionBody, "version">) => void | Promise<void>;
}) {
  const byId = useMemo(() => {
    const bi: Record<string, Candidate> = {}, bk: Record<string, Candidate> = {};
    for (const c of bundle.candidates) { bi[c.id] = c; if (c.business_key) bk[c.business_key] = c; }
    return { bi, bk };
  }, [bundle.candidates]);
  const tableLabel = useMemo(() => {
    const m: Record<string, string> = {};
    for (const t of bundle.profiles?.tables ?? []) m[t.table_id] = t.sheet_name ? `${t.original_filename} · ${t.sheet_name}` : t.original_filename;
    return m;
  }, [bundle.profiles]);

  const items: Item[] = useMemo(() => {
    const out: Item[] = [];
    for (const i of bundle.recordReviews) {
      const affectedIds: string[] = Array.isArray(i.affected?.candidates) ? i.affected.candidates.map((a: any) => a.candidate_id) : [];
      const related = affectedIds.map((id) => byId.bi[id]).filter(Boolean) as Candidate[];
      const candidate = (i.candidate_key ? byId.bk[i.candidate_key] : null) ?? byId.bi[i.affected?.candidate_id] ?? related[0] ?? null;
      out.push({ kind: "record", id: i.id, issue: i, candidate, related });
    }
    for (const i of bundle.targetReviews) out.push({ kind: "target", id: i.id, issue: i, candidate: byId.bi[i.candidate_id] ?? null });
    for (const i of bundle.reviews) out.push({ kind: "mapping", id: i.id, issue: i });
    for (const p of bundle.proposals) out.push({ kind: "proposal", id: p.id, proposal: p });
    out.sort((a, b) => reviewType(a).order - reviewType(b).order);
    return out;
  }, [bundle, byId]);

  const [selId, setSelId] = useState<string | null>(null);
  const [src, setSrc] = useState<SourceRef | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const advance = useRef<number | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const selIdx = items.findIndex((x) => x.id === selId);
  const selected = (selIdx >= 0 ? items[selIdx] : items[0]) ?? null;
  useEffect(() => { if (selected && selected.id !== selId) setSelId(selected.id); }, [selected, selId]);

  // After a decision resolves an issue and the bundle refreshes, move to the NEXT open issue (never
  // dump the reviewer back to the top or an empty view) and flash a non-blocking success message.
  useEffect(() => {
    if (advance.current != null && items.length) {
      setSelId(items[Math.min(advance.current, items.length - 1)].id);
      advance.current = null;
    }
  }, [items]);
  useEffect(() => { if (!toast) return; const t = setTimeout(() => setToast(null), 3500); return () => clearTimeout(t); }, [toast]);

  // The exact confirmation text depends on whether ANY review remains after this one (across every
  // kind — mapping/record/target/proposal), matching the auto-sync gate (order §1/§2): resolving the
  // LAST blocking item is the moment the pipeline resumes hands-free, so ONLY then do we claim that.
  const wrap = <F extends (...a: any[]) => any>(fn: F) => (async (...a: Parameters<F>) => {
    const wasLastOpenItem = items.length <= 1;
    advance.current = Math.max(0, items.findIndex((x) => x.id === selId));
    await fn(...a);
    setToast(wasLastOpenItem ? "Decision saved. Migration resumed automatically." : "Decision saved.");
  }) as F;

  const onKey = (e: React.KeyboardEvent) => {
    if (!items.length) return;
    const idx = Math.max(0, items.findIndex((x) => x.id === selected?.id));
    let next = idx;
    if (e.key === "ArrowDown") next = Math.min(items.length - 1, idx + 1);
    else if (e.key === "ArrowUp") next = Math.max(0, idx - 1);
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = items.length - 1;
    else return;
    e.preventDefault();
    setSelId(items[next].id);
  };

  // Queue row: plain-language title, human key, plain reason. Never "Unknown employee".
  const rowFor = (it: Item): { t: string; k: string; s: string } => {
    if (it.kind === "record") {
      const it2 = it as Extract<Item, { kind: "record" }>;
      if (it2.issue.issue_type === "shared_email") {
        const n = it2.related.length || (it2.issue.affected?.candidates?.length ?? 2);
        const email = it2.related[0]?.record?.work_email?.value ?? "shared email";
        const ids = (it2.issue.affected?.candidates ?? []).map((c: any) => c.business_key).filter(Boolean).join(" · ");
        return { t: `${n} employees share this work email`, k: String(email), s: ids || "Work email must be unique" };
      }
      if (it2.issue.issue_type === "unknown_enum") {
        return { t: `${fieldLabel(schema, it2.issue.field) || "A field"} needs value matches`, k: "values", s: "Some source values have no target value" };
      }
      if (it2.issue.issue_type === "orphan_child_row") {
        return { t: "Unattached rows", k: "rows", s: "Rows reference an unknown employee" };
      }
      const name = it2.candidate?.record?.full_name?.value ?? (it2.issue.candidate_key ? `Employee ${it2.issue.candidate_key}` : "An employee record");
      return { t: String(name), k: it2.issue.candidate_key ? String(it2.issue.candidate_key) : "", s: `${fieldLabel(schema, it2.issue.field) || "Record"} needs a decision` };
    }
    if (it.kind === "target") {
      const name = it.issue.affected?.incoming?.full_name ?? it.candidate?.record?.full_name?.value ?? "An employee";
      return { t: String(name), k: it.issue.business_key ?? "", s: `${fieldLabel(schema, it.issue.field) || "Identity"} differs from the target` };
    }
    if (it.kind === "mapping") {
      // A within-table conflict is the OPPOSITE ambiguity direction from "this column could mean
      // several things": several columns all look right for the SAME target, not one column being
      // unsure between several targets — the queue row should say which, matching the workspace.
      const conflict: string[] = Array.isArray(it.issue.evidence_summary?.within_table_conflict)
        ? it.issue.evidence_summary.within_table_conflict : [];
      const summary = conflict.length > 1
        ? `${tableLabel[it.issue.table_id] ?? ""} · also matches ${conflict.filter((h) => h !== it.issue.source_header).join(", ")}`
        : `${tableLabel[it.issue.table_id] ?? ""} · could match more than one field`;
      return { t: it.issue.source_header, k: "column", s: summary };
    }
    return { t: it.proposal.source_header, k: "column", s: `${tableLabel[it.proposal.table_id] ?? ""} · no matching target field` };
  };

  // Grouped summary + paused sentence.
  const groups = useMemo(() => {
    const g: Record<string, { label: string; order: number; items: Item[] }> = {};
    for (const it of items) {
      const rt = reviewType(it);
      (g[rt.key] ??= { label: rt.label, order: rt.order, items: [] }).items.push(it);
    }
    return Object.values(g).sort((a, b) => a.order - b.order);
  }, [items]);

  const hasRecord = bundle.recordReviews.length > 0;
  const hasMapping = bundle.reviews.length > 0 || bundle.proposals.length > 0;
  const hasTarget = bundle.targetReviews.length > 0;
  // The job pauses at the LATEST stage reached, so a record/target review outranks leftover mapping items.
  const pausedAt = hasTarget ? "Compare with target" : hasRecord ? "Clean & validate" : hasMapping ? "Match fields" : "";

  if (items.length === 0) {
    return (
      <div className="stack lg">
        <div className="page-head"><h1>Needs review</h1></div>
        <div className="empty">
          <Icon name="check" />
          <div className="stack" style={{ gap: 4, alignItems: "center" }}>
            <strong>All decisions resolved</strong>
            <div className="muted">Nothing needs your attention. The migration continues automatically.</div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="stack lg">
      <div className="page-head">
        <div className="stack" style={{ gap: 4 }}>
          <h1>{items.length} decision{items.length === 1 ? "" : "s"} need{items.length === 1 ? "s" : ""} attention</h1>
          {pausedAt && <div className="muted">The migration is paused at <b>{pausedAt}</b>. Resolve these decisions and it will resume automatically.</div>}
          <div className="review-chips">
            {groups.map((g) => <span key={g.label} className="chip"><span className="dot" />{g.label} · {g.items.length}</span>)}
          </div>
        </div>
      </div>

      {toast && <div className="toast"><Icon name="check" className="ico-xs" /> {toast}</div>}

      <div className="rv-layout">
        <div className="rv-queue" role="listbox" aria-label="Review queue" tabIndex={0} ref={listRef} onKeyDown={onKey}>
          {groups.map((g) => (
            <div key={g.label}>
              <div className="rv-qgroup">{g.label} · {g.items.length}</div>
              {g.items.map((it) => {
                const r = rowFor(it);
                const isSel = selected?.id === it.id;
                return (
                  <div key={it.id} role="option" aria-selected={isSel} className={cx("rv-qitem", isSel && "sel")} onClick={() => setSelId(it.id)}>
                    <div className="t">{r.t}</div>
                    {r.k && <div className="k">{r.k}</div>}
                    <div className="s">{r.s}</div>
                  </div>
                );
              })}
            </div>
          ))}
        </div>

        <div className="rv-ws" key={selected?.id}>
          {selected?.kind === "record" && (
            <RecordReviewWorkspace issue={selected.issue} candidate={selected.candidate} related={selected.related}
              schema={schema} onDecision={wrap(onRecordDecision)} onViewSource={setSrc} />
          )}
          {selected?.kind === "target" && (
            <TargetReviewWorkspace issue={selected.issue} candidate={selected.candidate} schema={schema}
              onDecision={wrap(onTargetDecision)} onViewSource={setSrc} />
          )}
          {selected?.kind === "mapping" && (
            <MappingReviewWorkspace issue={selected.issue} schema={schema} tableLabel={tableLabel[selected.issue.table_id] ?? ""}
              onDecision={wrap(onDecision)} />
          )}
          {selected?.kind === "proposal" && (
            <ProposalReviewWorkspace proposal={selected.proposal} schema={schema} tenantFields={bundle.tenantFields}
              tableLabel={tableLabel[selected.proposal.table_id] ?? ""} onDecision={wrap(onProposalDecision)} />
          )}
        </div>
      </div>

      {src && <SourceRowDrawer jobId={jobId} source={src} onClose={() => setSrc(null)} />}
    </div>
  );
}
