import { useRef, useState } from "react";
import type { Tenant } from "../types";
import { Icon } from "./ui";
import { orgLabel, slugifyOrg } from "../labels";

function fmtSize(n: number): string {
  return n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function UploadPanel({ onUpload, error, tenants, defaultTenant }: {
  onUpload: (files: File[], tenantId: string) => void; error: string | null; tenants: Tenant[]; defaultTenant: string;
}) {
  const [drag, setDrag] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  // Start empty — do NOT pre-fill the internal "default" organization (M3G §C1). The user names the
  // client organization; a blank entry falls back to the internal default only as a last resort.
  const [org, setOrg] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const add = (list: FileList | null) => { if (list) setFiles((p) => [...p, ...Array.from(list)]); };

  const entered = org.trim();
  // The org name the consultant types is free text ("Acme Pvt Ltd"), but the backend tenant_id only
  // allows [A-Za-z0-9-_.]. Normalize to a valid id here so a natural name never gets rejected.
  const slug = slugifyOrg(entered);
  const effectiveTenant = slug || defaultTenant;
  const normalized = slug !== "" && slug !== entered; // the typed name had to be adjusted (e.g. spaces)
  const known = tenants.find((t) => t.id === slug);
  // Only surface an organization-field count when it is actually useful (non-zero); never "0 fields".
  const orgHint = !entered ? "Name the client organization this migration is for"
    : slug === "default" ? "Default organization (internal)"
    : known && known.custom_field_count > 0
      ? `${known.custom_field_count} organization field${known.custom_field_count === 1 ? "" : "s"} configured`
    : known ? "Existing organization"
    : normalized ? `New organization — saved as “${slug}”`
    : "New organization";

  return (
    <div className="upload-hero">
      <div className="section-title">New migration</div>
      <div className="section-desc" style={{ marginBottom: 16 }}>
        Upload the client's raw HR exports. CSV and XLSX are supported (an employee master plus optional child tables / sheets such as Vehicles or Education); PDF and other formats are rejected.
        Files are ingested durably in the background — you can watch each one process.
      </div>
      <div className="panel">
        <div className="panel-b">
          <div className="form-grid" style={{ marginBottom: 14 }}>
            <label htmlFor="org">Organization</label>
            <div className="stack" style={{ gap: 6 }}>
              <div className="row" style={{ gap: 8, alignItems: "center" }}>
                <input id="org" type="text" value={org} placeholder="e.g. Acme Pvt Ltd"
                  onChange={(e) => setOrg(e.target.value)} style={{ minWidth: 260 }} />
                <span className="muted small">{orgHint}</span>
              </div>
            </div>
          </div>
          <div className={`dropzone ${drag ? "drag" : ""}`} onClick={() => inputRef.current?.click()}
            onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
            onDragLeave={() => setDrag(false)}
            onDrop={(e) => { e.preventDefault(); setDrag(false); add(e.dataTransfer.files); }}>
            <input ref={inputRef} type="file" multiple accept=".csv,.xlsx" style={{ display: "none" }}
              onChange={(e) => add(e.target.files)} />
            <Icon name="upload" /> <span style={{ marginLeft: 6 }}>Drop files here, or click to choose (.csv, .xlsx)</span>
          </div>

          {files.length > 0 && (
            <div style={{ marginTop: 14 }}>
              <table className="data-table">
                <thead><tr><th>File</th><th className="num">Size</th><th></th></tr></thead>
                <tbody>
                  {files.map((f, i) => (
                    <tr key={i}>
                      <td className="mono">{f.name}</td>
                      <td className="num">{fmtSize(f.size)}</td>
                      <td className="right"><button type="button" className="btn ghost sm" onClick={() => setFiles(files.filter((_, k) => k !== i))}>Remove</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="toolbar">
                <button type="button" className="btn primary" onClick={() => onUpload(files, effectiveTenant)} disabled={files.length === 0}>
                  Start migration for {orgLabel(effectiveTenant)} ({files.length} file{files.length > 1 ? "s" : ""})
                </button>
              </div>
            </div>
          )}
          {error && <div className="banner error" style={{ marginTop: 12 }}>{error}</div>}
        </div>
      </div>
    </div>
  );
}
