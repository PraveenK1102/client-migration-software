import { useMemo, useState } from "react";
import type { ColumnProfile, MappingRow, TargetSchema } from "../types";
import type { JobBundle, Section } from "../viewtypes";
import { DestinationTag, Drawer, Icon, MethodTag, cx } from "./ui";
import { destinationLabel, mappingStatusLabel, methodLabel } from "../labels";

const FILTERS: { key: string; label: string; test: (r: MappingRow) => boolean }[] = [
  { key: "all", label: "All", test: () => true },
  { key: "core", label: "Standard", test: (r) => r.destination_kind === "CORE_FIELD" },
  { key: "structured", label: "Related records", test: (r) => r.destination_kind === "COLLECTION_FIELD" },
  { key: "custom", label: "Organization fields", test: (r) => r.destination_kind === "CUSTOM_FIELD" },
  { key: "review", label: "Needs review", test: (r) => r.destination_kind === "NEEDS_REVIEW" },
  { key: "proposal", label: "New organization fields", test: (r) => r.destination_kind === "PROPOSAL" },
  { key: "ignored", label: "Not migrated", test: (r) => r.destination_kind === "IGNORED" },
  { key: "unmapped", label: "Not mapped", test: (r) => r.destination_kind === "UNMAPPED" },
];

function pathMeta(m: Record<string, any> | null): string {
  if (!m) return "";
  if (m.index != null) return `item #${m.index}`;
  if (m.multi_value) return `delimited (${(m.delimiters || []).join(" ")})`;
  if (m.single_item) return "single item";
  return "";
}

/** A privacy-safe one-line description of a column's data (M3G §G): shape, not raw PII samples. */
function shapeSummary(p: ColumnProfile, header: string): string {
  const total = p.non_empty_count + p.missing_count;
  const pct = total > 0 ? Math.round((p.non_empty_count / total) * 100) : 0;
  const types = Object.keys(p.observed_types || {});
  const typeWord = types.includes("date") ? "Dates"
    : types.length === 1 && types[0] === "number" ? "Numbers"
    : types.includes("number") && types.includes("text") ? "Mixed values" : "Text values";
  const parts = [typeWord, `${pct}% filled`];
  if (p.non_empty_count > 0) {
    const ratio = p.distinct_count / p.non_empty_count;
    if (p.distinct_count === p.non_empty_count) parts.push("all unique");
    else if (ratio >= 0.9) parts.push("mostly unique");
    else parts.push(`${p.distinct_count} distinct values`);
  }
  // Header-driven shape hint (predictable; avoids false positives from format-indicator key names).
  const h = header.toLowerCase();
  if (h.includes("email") || h.includes("mail") || h.includes("mailbox")) parts.push("email-like");
  else if (h.includes("date") || h.includes("joining") || h.includes("hire") || h.includes("commencement") || h.includes("service")) parts.push("date-like");
  else if (h.includes("name")) parts.push("name-like");
  else if (h.endsWith("_id") || h.includes(" id") || h.includes("reference") || h.includes("employee_id")) parts.push("identifier-like");
  return parts.join(" · ");
}

/** Low-cardinality categorical values are safe & useful to show inline; high-cardinality PII is not. */
const LOW_CARD = 12;

export function MappingView({ bundle, schema, go }: { bundle: JobBundle; schema: TargetSchema | null; go: (s: Section) => void }) {
  const [sel, setSel] = useState<MappingRow | null>(null);
  const [filter, setFilter] = useState("all");
  const [showSamples, setShowSamples] = useState(false);
  const tableLabel = useMemo(() => {
    const m: Record<string, string> = {};
    for (const t of bundle.profiles?.tables ?? [])
      m[t.table_id] = t.sheet_name ? `${t.original_filename} · ${t.sheet_name}` : t.original_filename;
    return m;
  }, [bundle.profiles]);
  const tableRole = useMemo(() => {
    const m: Record<string, string> = {};
    for (const t of bundle.profiles?.tables ?? []) m[t.table_id] = t.table_role;
    return m;
  }, [bundle.profiles]);
  const profileById = useMemo(() => {
    const m: Record<string, ColumnProfile> = {};
    for (const t of bundle.profiles?.tables ?? []) for (const p of t.profiles) m[p.profile_id] = p;
    return m;
  }, [bundle.profiles]);

  const rows: MappingRow[] = [
    ...(bundle.mappings?.accepted ?? []), ...(bundle.mappings?.unresolved ?? []),
    ...(bundle.mappings?.proposals ?? []), ...(bundle.mappings?.ignored ?? []),
  ];
  const counts = bundle.mappings?.counts ?? {};
  const shown = rows.filter(FILTERS.find((f) => f.key === filter)!.test);
  const decideable = (r: MappingRow) => r.destination_kind === "PROPOSAL" || r.destination_kind === "NEEDS_REVIEW";
  const unresolvedNoTarget = (r: MappingRow) => !r.target_field && !decideable(r) && r.destination_kind !== "IGNORED";

  const selProfile = sel ? profileById[sel.profile_id] : null;
  const selDesc = sel && schema && sel.target_field
    ? (schema.fields.find((f) => f.path === sel.target_field) ?? schema.custom_fields.find((f) => f.path === sel.target_field)
        ?? schema.collections.flatMap((c) => c.fields).find((f) => f.path === sel.target_field))?.description
    : undefined;
  const lowCard = selProfile && selProfile.distinct_count > 0 && selProfile.distinct_count <= LOW_CARD;

  const statusTone = (r: MappingRow) =>
    ["needs_review", "proposal"].includes(r.status) ? "amber"
    : ["auto_accepted", "approved", "corrected"].includes(r.status) ? "green" : "";

  return (
    <div>
      <div className="section-title">Field mapping</div>
      <div className="section-desc">We match each column from the source files to a target employee field. Clear matches are handled automatically. Only uncertain matches need your review — nothing is silently dropped.</div>

      <div className="toolbar">
        <div className="filters" role="tablist" aria-label="Filter mappings">
          {FILTERS.map((f) => {
            const n = rows.filter(f.test).length;
            if (f.key !== "all" && n === 0) return null;
            return <button key={f.key} type="button" role="tab" aria-selected={filter === f.key} className={cx("filter", filter === f.key && "active")} onClick={() => setFilter(f.key)}>{f.label}{f.key !== "all" ? ` (${n})` : ` (${rows.length})`}</button>;
          })}
        </div>
        <div className="spacer" />
        <span className="muted small">{counts.core ?? 0} standard · {counts.collection ?? 0} related · {counts.custom ?? 0} organization · {counts.ignored ?? 0} not mapped</span>
      </div>

      <div className="panel">
        <div className="table-wrap">
          <table className="data-table">
            <thead><tr><th>Source column</th><th>Source file</th><th>Target field</th><th>Field type</th><th>Match</th><th></th></tr></thead>
            <tbody>
              {shown.map((r, i) => (
                <tr key={r.profile_id + i} className="clickable" onClick={() => { setSel(r); setShowSamples(false); }}>
                  <td><span className="mono">{r.source_header}</span></td>
                  <td className="small muted">{tableLabel[r.table_id] ?? "—"}{tableRole[r.table_id]?.startsWith("child:") && <span className="tag structured" style={{ marginLeft: 6 }}>related table</span>}</td>
                  <td>{r.target_field ? <span className="mono">{r.target_field}{pathMeta(r.path_meta) && <span className="muted small"> · {pathMeta(r.path_meta)}</span>}</span> : <span className="cell-empty">—</span>}</td>
                  <td><DestinationTag kind={r.destination_kind} /></td>
                  <td><span className={cx("chip", statusTone(r))}><span className="dot" />{mappingStatusLabel(r.status, r.method)}</span></td>
                  <td className="right">{decideable(r) && <button type="button" className="btn sm ghost" onClick={(e) => { e.stopPropagation(); go("reviews"); }}>Decide <Icon name="arrow" /></button>}</td>
                </tr>
              ))}
              {shown.length === 0 && <tr><td colSpan={6} className="empty">No columns in this category.</td></tr>}
            </tbody>
          </table>
        </div>
      </div>

      {sel && (
        <Drawer title={sel.source_header} subtitle={tableLabel[sel.table_id]} onClose={() => setSel(null)} wide>
          {/* 1. Match summary */}
          <div className="drawer-section">
            <div className="dl">
              <div className="k">Source column</div><div className="mono">{sel.source_header}</div>
              <div className="k">Target field</div>
              <div className="dl-inline">
                {sel.target_field ? <span className="mono">{sel.target_field}</span> : <span className="muted">No target field yet</span>}
                <DestinationTag kind={sel.destination_kind} />
              </div>
              <div className="k">Match</div>
              <div><span className={cx("chip", statusTone(sel))}><span className="dot" />{mappingStatusLabel(sel.status, sel.method)}</span></div>
            </div>
            {sel.reason && <div className="why" style={{ marginTop: 10 }}>{sel.reason}</div>}
            {selDesc && <div className="muted small" style={{ marginTop: 8 }}><b>{destinationLabel(sel.destination_kind)}:</b> {selDesc}</div>}
          </div>

          {/* 2. Why this match makes sense — privacy-safe evidence */}
          {selProfile && (
            <div className="drawer-section">
              <div className="h5">Why this match makes sense</div>
              <div className="ev-line">{shapeSummary(selProfile, sel.source_header)}</div>
              <div className="ev-cards">
                <div className="ev-card"><div className="ev-n">{Math.round((selProfile.non_empty_count / Math.max(1, selProfile.non_empty_count + selProfile.missing_count)) * 100)}%</div><div className="ev-l">filled</div></div>
                <div className="ev-card"><div className="ev-n">{selProfile.distinct_count}</div><div className="ev-l">distinct values</div></div>
                <div className="ev-card"><div className="ev-n">{selProfile.non_empty_count}</div><div className="ev-l">values present</div></div>
              </div>
              {lowCard ? (
                <div style={{ marginTop: 10 }}>
                  <div className="muted small" style={{ marginBottom: 4 }}>Values in this column</div>
                  <div className="samples">{selProfile.samples.map((s, i) => <span key={i} className="chip sq">{s}</span>)}</div>
                </div>
              ) : selProfile.samples.length > 0 ? (
                <details className="tech" style={{ marginTop: 10 }} open={showSamples}>
                  <summary>View sample values</summary>
                  <div className="samples" style={{ marginTop: 8 }}>{selProfile.samples.map((s, i) => <span key={i} className="chip sq">{s}</span>)}</div>
                  <div className="muted small" style={{ marginTop: 6 }}>Samples stay in your browser and are never sent to the AI model beyond the privacy-safe projection.</div>
                </details>
              ) : null}
            </div>
          )}

          {/* 3. Decision */}
          <div className="drawer-section">
            <div className="h5">Decision</div>
            {sel.target_field ? (
              <div className="dl">
                <div className="k">Decided by</div><div><MethodTag method={sel.method} /> <span className="muted small">{methodLabel(sel.method)}</span></div>
                <div className="k">Result</div><div>Mapped to <span className="mono">{sel.target_field}</span></div>
                {sel.note && (<><div className="k">Note</div><div>{sel.note}</div></>)}
              </div>
            ) : decideable(sel) ? (
              <>
                <div className="muted small" style={{ marginBottom: 8 }}>This column needs a decision before the migration can continue.</div>
                <button type="button" className="btn primary" onClick={() => { setSel(null); go("reviews"); }}>Open in Needs review</button>
              </>
            ) : unresolvedNoTarget(sel) ? (
              <>
                <div className="muted small" style={{ marginBottom: 8 }}>No target field was found for this column. Choose what to do with it.</div>
                <button type="button" className="btn primary" onClick={() => { setSel(null); go("reviews"); }}>Choose in Needs review</button>
              </>
            ) : (
              <div className="muted small">This column is not migrated.</div>
            )}
          </div>

          {/* 4. Technical details */}
          <details className="tech">
            <summary>Technical details</summary>
            <div className="dl" style={{ marginTop: 10 }}>
              <div className="k">Destination kind</div><div className="mono small">{sel.destination_kind}</div>
              {sel.target_field && (<><div className="k">Destination path</div><div className="mono small">{sel.target_field}{pathMeta(sel.path_meta) && ` · ${pathMeta(sel.path_meta)}`}</div></>)}
              {sel.custom_definition_id && (<><div className="k">Field definition ID</div><div className="mono small">{sel.custom_definition_id}</div></>)}
              <div className="k">Raw status</div><div className="mono small">{sel.status}</div>
              <div className="k">Decision method</div><div className="mono small">{sel.method || "—"}</div>
              <div className="k">Source column ID</div><div className="mono small">{sel.profile_id}</div>
            </div>
          </details>
        </Drawer>
      )}
    </div>
  );
}
