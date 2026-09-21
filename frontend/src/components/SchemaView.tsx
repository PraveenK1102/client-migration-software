import { useMemo, useState } from "react";
import type { SchemaField, TargetSchema, Tenant } from "../types";
import { DestinationTag, cx } from "./ui";
import { orgLabel } from "../labels";

type Tab = "core" | "collections" | "custom";

/** Compact Configuration → Target schema browser: core fields / structured collections / tenant custom fields,
 *  searchable. The contract is representative and defined by this prototype (not Darwinbox's proprietary schema). */
export function SchemaView({ schema, tenants, tenantId, onTenantChange }: {
  schema: TargetSchema | null; tenants: Tenant[]; tenantId: string | null; onTenantChange?: (t: string) => void;
}) {
  const [tab, setTab] = useState<Tab>("core");
  const [q, setQ] = useState("");
  const match = (f: SchemaField) => !q || [f.label, f.path, f.description, f.value_type, ...(f.allowed_values ?? [])].join(" ").toLowerCase().includes(q.toLowerCase());
  const groups = useMemo(() => schema?.groups?.length ? schema.groups : [{ key: "all", label: "Fields" }], [schema]);
  if (!schema) return <div className="empty">Schema unavailable.</div>;
  const coreShown = schema.fields.filter(match);
  const customShown = schema.custom_fields.filter(match);
  const collShown = schema.collections.map((c) => ({ ...c, fields: c.fields.filter(match) })).filter((c) => c.fields.length || (!q) || c.label.toLowerCase().includes(q.toLowerCase()));

  return (
    <div>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <div className="section-title">Target fields</div>
          <div className="section-desc">The employee fields this migration can write to: {schema.fields.length} standard fields, {schema.collections.length} related-record types, and {schema.custom_fields.length} organization field{schema.custom_fields.length === 1 ? "" : "s"}.</div>
        </div>
        {onTenantChange && (
          <label className="row" style={{ gap: 6 }}>
            <span className="fld">Organization</span>
            <select value={tenantId ?? ""} onChange={(e) => onTenantChange(e.target.value)} aria-label="Organization">
              {tenants.map((t) => <option key={t.id} value={t.id}>{orgLabel(t.id)}{t.custom_field_count ? ` (${t.custom_field_count} org field${t.custom_field_count === 1 ? "" : "s"})` : ""}</option>)}
              {!tenants.some((t) => t.id === tenantId) && tenantId && <option value={tenantId}>{orgLabel(tenantId)}</option>}
            </select>
          </label>
        )}
      </div>
      <div className="schema-note">
        Representative employee migration contract defined by this prototype under the assignment's permission — not a reproduction of Darwinbox's proprietary production schema, and these keys are not official Darwinbox IDs.
        Boundary: <b>Standard</b> {schema.boundary?.core} <b>Related records</b> {schema.boundary?.collections} <b>Organization</b> {schema.boundary?.custom} <b>Not mapped</b> {schema.boundary?.unmapped}
      </div>

      <div className="toolbar">
        <div className="tabs" style={{ margin: 0, borderBottom: "none" }} role="tablist">
          <button type="button" role="tab" aria-selected={tab === "core"} className={cx("tab", tab === "core" && "active")} onClick={() => setTab("core")}>Standard fields ({schema.fields.length})</button>
          <button type="button" role="tab" aria-selected={tab === "collections"} className={cx("tab", tab === "collections" && "active")} onClick={() => setTab("collections")}>Related records ({schema.collections.length})</button>
          <button type="button" role="tab" aria-selected={tab === "custom"} className={cx("tab", tab === "custom" && "active")} onClick={() => setTab("custom")}>Organization fields ({schema.custom_fields.length})</button>
        </div>
        <div className="spacer" />
        <input className="search" type="text" placeholder="Search fields, paths, values…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search schema" />
      </div>

      {tab === "core" && (
        <div className="panel">
          <div className="table-wrap">
            <table className="data-table">
              <thead><tr><th>Field</th><th>Path</th><th>Type</th><th>Required</th><th>Allowed values</th><th>Description</th></tr></thead>
              <tbody>
                {groups.map((g) => {
                  const fs = coreShown.filter((f) => (f.group ?? "all") === g.key);
                  if (!fs.length) return null;
                  return [
                    <tr key={`g-${g.key}`}><td colSpan={6} className="h5" style={{ margin: 0, padding: "8px 12px 4px" }}>{g.label}</td></tr>,
                    ...fs.map((f) => (
                      <tr key={f.path}>
                        <td>{f.label}</td>
                        <td className="mono small">{f.path}</td>
                        <td><span className="tag">{f.value_type}</span></td>
                        <td>{f.required ? <span className="chip green"><span className="dot" />required</span> : <span className="muted small">optional</span>}</td>
                        <td className="small">{f.allowed_values?.length ? f.allowed_values.join(", ") : <span className="muted">—</span>}</td>
                        <td className="small muted">{f.description}</td>
                      </tr>
                    )),
                  ];
                })}
                {coreShown.length === 0 && <tr><td colSpan={6} className="empty">No standard field matches.</td></tr>}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {tab === "collections" && collShown.map((c) => (
        <div key={c.key} className="panel">
          <div className="panel-h">
            <h2>{c.label} <span className="mono muted small">{c.key}[]</span></h2>
            <span className="sub">identity: {c.item_identity.join(" + ")} · table aliases: {c.table_aliases.join(", ")}</span>
            <div className="spacer" /><DestinationTag kind="COLLECTION_FIELD" />
          </div>
          <div className="table-wrap">
            <table className="data-table">
              <thead><tr><th>Item field</th><th>Path</th><th>Type</th><th>Required</th><th>Allowed values</th><th>Header aliases</th></tr></thead>
              <tbody>
                {c.fields.map((f) => (
                  <tr key={f.path}>
                    <td>{f.label}</td>
                    <td className="mono small">{f.path}</td>
                    <td><span className="tag">{f.value_type}</span></td>
                    <td>{f.required ? <span className="chip green"><span className="dot" />required</span> : <span className="muted small">optional</span>}</td>
                    <td className="small">{f.allowed_values?.length ? f.allowed_values.join(", ") : <span className="muted">—</span>}</td>
                    <td className="small muted">{f.description}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {(c.indexed_column || c.multi_value_column) && (
            <div className="panel-b small muted">
              Legacy flattened shapes accepted by rule (schema-declared only):
              {c.indexed_column && <> indexed columns “{c.indexed_column.aliases[0]} 1”, “{c.indexed_column.aliases[0]} 2” → {c.key}[].{c.indexed_column.field};</>}
              {c.multi_value_column && <> delimited “{c.multi_value_column.aliases[0]}” ({c.multi_value_column.delimiters.join(" ")}) → {c.key}[].{c.multi_value_column.field}.</>}
            </div>
          )}
        </div>
      ))}

      {tab === "custom" && (
        <div className="panel">
          <div className="table-wrap">
            <table className="data-table">
              <thead><tr><th>Label</th><th>Path</th><th>Type</th><th>Required</th><th>Options</th><th>Definition</th></tr></thead>
              <tbody>
                {customShown.map((f) => (
                  <tr key={f.path}>
                    <td>{f.label}</td>
                    <td className="mono small">{f.path}</td>
                    <td><span className="tag">{f.value_type}{f.multi_value ? " · multi" : ""}</span></td>
                    <td>{f.required ? <span className="chip green"><span className="dot" />required</span> : <span className="muted small">optional</span>}</td>
                    <td className="small">{f.allowed_values?.length ? f.allowed_values.join(", ") : <span className="muted">—</span>}</td>
                    <td className="mono small muted">{f.custom_definition_id}</td>
                  </tr>
                ))}
                {customShown.length === 0 && <tr><td colSpan={6} className="empty">No organization fields yet. Columns with no standard target field become fields you can create under Needs review.</td></tr>}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {schema.date_role_group?.length > 0 && tab === "core" && (
        <div className="banner info">Date-role group: {schema.date_role_group.join(", ")} — a bare date column is escalated rather than assigned a role from its values.</div>
      )}
    </div>
  );
}
