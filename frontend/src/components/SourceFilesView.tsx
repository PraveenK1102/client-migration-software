import { useEffect, useState } from "react";
import { api } from "../api";
import type { SourceFile, SourceTable, StagedRows, WorkItem } from "../types";
import type { JobBundle } from "../viewtypes";
import { ConfirmDialog, Drawer, Icon, OverflowMenu, StatusChip, WorkChip, cx } from "./ui";

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function SourceFilesView({ jobId, bundle, onChanged }:
  { jobId: string; bundle: JobBundle; onChanged?: () => void }) {
  const [inspect, setInspect] = useState<SourceFile | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [removing, setRemoving] = useState<SourceFile[] | null>(null);   // files pending the confirm dialog
  const [notice, setNotice] = useState<string | null>(null);

  const files = bundle.files;
  // Keep selection in sync if files change underneath us (e.g. after a rebuild).
  useEffect(() => {
    setSelected((prev) => new Set([...prev].filter((id) => files.some((f) => f.id === id))));
  }, [files]);

  const tablesByFile = (fileId: string): SourceTable[] =>
    (bundle.profiles?.tables ?? []).filter((t) => t.file_id === fileId);
  const workFor = (fileId: string): WorkItem | undefined =>
    bundle.workItems.filter((w) => w.source_file_id === fileId).slice(-1)[0];

  const toggle = (id: string) =>
    setSelected((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const allSelected = files.length > 0 && selected.size === files.length;
  const toggleAll = () => setSelected(allSelected ? new Set() : new Set(files.map((f) => f.id)));

  const selectedFiles = files.filter((f) => selected.has(f.id));
  const removesAll = removing != null && removing.length >= files.length;   // would leave zero files

  const doRemove = async () => {
    if (!removing) return;
    const ids = removing.map((f) => f.id);
    const res = await api.removeFiles(jobId, ids);
    setRemoving(null);
    setSelected(new Set());
    setNotice(`${res.removed_filenames.length} file${res.removed_filenames.length === 1 ? "" : "s"} removed. `
      + `Rebuilding the migration from ${res.remaining_files} remaining file${res.remaining_files === 1 ? "" : "s"}.`);
    onChanged?.();
  };

  return (
    <div>
      <div className="row" style={{ alignItems: "flex-start" }}>
        <div className="stack" style={{ gap: 1 }}>
          <div className="section-title">Source files &amp; ingestion</div>
          <div className="section-desc">Each uploaded file, its durable work-item status, and the tables/sheets discovered inside it.</div>
        </div>
        <div className="spacer" />
        {selected.size > 0 && (
          <div className="row" style={{ gap: 8 }}>
            <span className="muted small">{selected.size} selected</span>
            <button className="btn sm" onClick={() => setSelected(new Set())}>Clear</button>
            <button className="btn sm danger" onClick={() => setRemoving(selectedFiles)}>
              <Icon name="trash" className="ico-xs" /> Remove selected file{selected.size === 1 ? "" : "s"}
            </button>
          </div>
        )}
      </div>

      {notice && (
        <div className="banner ok" style={{ marginTop: 12 }}>
          <Icon name="refresh" className="ico-xs" /> {notice}
          <button className="link-btn" style={{ marginLeft: 8 }} onClick={() => setNotice(null)}>Dismiss</button>
        </div>
      )}

      <div className="panel" style={{ marginTop: 16 }}>
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th className="chk"><input type="checkbox" aria-label="Select all files" checked={allSelected}
                  ref={(el) => { if (el) el.indeterminate = selected.size > 0 && !allSelected; }}
                  onChange={toggleAll} /></th>
                <th>File</th><th>Type</th><th className="num">Size</th><th>Parse status</th>
                <th>Tables / sheets</th><th className="num">Rows</th><th>Work</th><th className="num">Attempt</th><th></th></tr>
            </thead>
            <tbody>
              {files.map((f) => {
                const tabs = tablesByFile(f.id);
                const rows = tabs.reduce((n, t) => n + t.n_rows, 0);
                const w = workFor(f.id);
                return (
                  <tr key={f.id} className={cx("clickable", selected.has(f.id) && "row-selected")}
                    onClick={() => setInspect(f)}>
                    <td className="chk" onClick={(e) => e.stopPropagation()}>
                      <input type="checkbox" aria-label={`Select ${f.original_filename}`}
                        checked={selected.has(f.id)} onChange={() => toggle(f.id)} />
                    </td>
                    <td><span className="mono">{f.original_filename}</span></td>
                    <td><span className="tag">{(f.content_type || "").includes("sheet") ? "XLSX" : "CSV"}</span></td>
                    <td className="num">{fmtSize(f.size_bytes)}</td>
                    <td><StatusChip status={f.parse_status} />{f.parse_status === "failed" && f.parse_error && <div className="muted small">{f.parse_error}</div>}</td>
                    <td>{tabs.length ? `${tabs.length} ${tabs.length === 1 ? "table" : "sheets"}` : <span className="muted">—</span>}</td>
                    <td className="num">{rows || "—"}</td>
                    <td>{w ? <WorkChip status={w.status} /> : <span className="muted">—</span>}</td>
                    <td className="num">{w ? `${w.attempt}/${w.max_attempts}` : "—"}</td>
                    <td className="right" onClick={(e) => e.stopPropagation()}>
                      <div className="row" style={{ gap: 4, justifyContent: "flex-end" }}>
                        <button className="btn sm ghost" onClick={() => setInspect(f)}><Icon name="inspect" /> Inspect</button>
                        <OverflowMenu size="sm" label={`Actions for ${f.original_filename}`} items={[
                          { key: "remove", label: "Remove from migration", icon: "trash", danger: true,
                            onClick: () => setRemoving([f]) },
                        ]} />
                      </div>
                    </td>
                  </tr>
                );
              })}
              {files.length === 0 && <tr><td colSpan={10} className="empty">No files yet.</td></tr>}
            </tbody>
          </table>
        </div>
      </div>

      {removing && (
        removesAll ? (
          <ConfirmDialog title="Can’t remove every source file" confirmLabel="OK" tone="primary"
            onConfirm={() => setRemoving(null)} onClose={() => setRemoving(null)}>
            <p>A migration needs at least one source file. To remove everything, delete the migration instead
              (from the migration header menu or the Migrations list).</p>
          </ConfirmDialog>
        ) : (
          <ConfirmDialog
            title={`Remove ${removing.length} file${removing.length === 1 ? "" : "s"} from this migration?`}
            confirmLabel="Remove files & rebuild" busyLabel="Removing…"
            onConfirm={doRemove} onClose={() => setRemoving(null)}>
            <ul className="plain-list">
              {removing.map((f) => <li key={f.id}><span className="mono">{f.original_filename}</span></li>)}
            </ul>
            <p>Their staged rows and any mapping/preparation results derived from them will be removed. The
              migration will be rebuilt from the remaining source files. Nothing will be deleted from the
              target system.</p>
          </ConfirmDialog>
        )
      )}

      {inspect && (
        <Drawer wide title={inspect.original_filename}
          subtitle={<span className="mono">sha256 {inspect.sha256?.slice(0, 12)}… · {fmtSize(inspect.size_bytes)}</span>}
          onClose={() => setInspect(null)}>
          <FileInspector jobId={jobId} file={inspect} tables={tablesByFile(inspect.id)} work={workFor(inspect.id)} />
        </Drawer>
      )}
    </div>
  );
}

function FileInspector({ jobId, file, tables, work }:
  { jobId: string; file: SourceFile; tables: SourceTable[]; work?: WorkItem }) {
  return (
    <div>
      <div className="kv" style={{ marginBottom: 14 }}>
        <div className="k">Parse status</div><div><StatusChip status={file.parse_status} /></div>
        <div className="k">Storage</div><div>{file.storage_status}</div>
        <div className="k">Work item</div><div>{work ? <>{work.kind} · <WorkChip status={work.status} /> · attempt {work.attempt}/{work.max_attempts}</> : "—"}</div>
        {file.parse_error && (<><div className="k">Parse error</div><div className="mono" style={{ color: "var(--red)" }}>{file.parse_error}</div></>)}
        <div className="k">Checksum</div><div className="mono small">{file.sha256}</div>
      </div>
      {tables.length === 0 ? (
        <div className="empty">No tables parsed{file.parse_status === "failed" ? " — ingestion failed." : " yet."}</div>
      ) : (
        <StagedDataInspector jobId={jobId} tables={tables} />
      )}
    </div>
  );
}

function StagedDataInspector({ jobId, tables }: { jobId: string; tables: SourceTable[] }) {
  const [active, setActive] = useState(0);
  const [page, setPage] = useState<StagedRows | null>(null);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const limit = 25;
  const safeActive = Math.min(active, Math.max(0, tables.length - 1));
  const t = tables[safeActive];

  useEffect(() => { setOffset(0); }, [safeActive]);
  useEffect(() => {
    if (!t) return;
    let ok = true;
    setLoading(true);
    api.getStagedRows(jobId, t.table_id, offset, limit)
      .then((r) => { if (ok) setPage(r); })
      .finally(() => { if (ok) setLoading(false); });
    return () => { ok = false; };
  }, [jobId, t?.table_id, offset]);

  if (!t) return null;
  return (
    <div>
      {tables.length > 1 && (
        <div className="tabs">
          {tables.map((tb, i) => (
            <button key={tb.table_id} className={cx("tab", i === safeActive && "active")} onClick={() => setActive(i)}>
              {tb.sheet_name ?? "Table"} <span className="muted">({tb.n_rows})</span>
            </button>
          ))}
        </div>
      )}
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
        <div className="prov">
          <span>{t.original_filename}</span>{t.sheet_name && <><span className="arrow">›</span><span>{t.sheet_name}</span></>}
          <span className="chip">{t.headers.length} columns</span><span className="chip">{t.n_rows} rows</span>
        </div>
        <span className="muted small">Raw staged data (not target data)</span>
      </div>
      <div className="table-wrap" style={{ maxHeight: "48vh", overflow: "auto" }}>
        <table className="grid-preview">
          <thead>
            <tr><th className="rownum">#</th>{t.headers.map((h, i) => <th key={i}>{h}</th>)}</tr>
          </thead>
          <tbody>
            {loading && !page ? (
              <tr><td className="rownum">…</td>{t.headers.map((_, i) => <td key={i}><div className="skl" /></td>)}</tr>
            ) : (page?.rows ?? []).map((r) => (
              <tr key={r.row_number}>
                <td className="rownum">{r.row_number}</td>
                {t.headers.map((_, i) => {
                  const cell = r.cells.find((c) => c.col_index === i);
                  const v = cell?.value;
                  return <td key={i}>{v == null || v === "" ? <span className="cell-empty">null</span> : v}</td>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {page && page.total > limit && (
        <div className="row" style={{ justifyContent: "flex-end", marginTop: 8 }}>
          <span className="muted small">{offset + 1}–{Math.min(offset + limit, page.total)} of {page.total}</span>
          <button className="btn sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>Prev</button>
          <button className="btn sm" disabled={offset + limit >= page.total} onClick={() => setOffset(offset + limit)}>Next</button>
        </div>
      )}
    </div>
  );
}
