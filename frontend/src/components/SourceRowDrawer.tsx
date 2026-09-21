import { useEffect, useState } from "react";
import { api } from "../api";
import type { SourceRowContext } from "../types";
import type { SourceRef } from "../viewtypes";
import { Drawer, cx } from "./ui";

/** Focused view of ONE persisted source row (File -> Sheet -> Row -> Header) with the referenced
 *  cell highlighted and a little surrounding context. Loads real staged rows from the backend. */
export function SourceRowDrawer({ jobId, source, onClose }: { jobId: string; source: SourceRef; onClose: () => void }) {
  const ref = source;   // `ref` is reserved as a React prop name, so the caller passes `source`
  const [data, setData] = useState<SourceRowContext | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let ok = true;
    setData(null); setErr(null);
    api.getSourceRow(jobId, ref.table_id, ref.row_number, { header: ref.header ?? null, col_index: ref.col_index ?? null, context: 2 })
      .then((d) => { if (ok) setData(d); })
      .catch((e) => { if (ok) setErr(e instanceof Error ? e.message : String(e)); });
    return () => { ok = false; };
  }, [jobId, ref.table_id, ref.row_number, ref.header, ref.col_index]);

  const file = data?.original_filename ?? ref.original_filename ?? "source file";
  const sheet = data?.sheet_name ?? ref.sheet_name ?? null;
  const hi = data?.highlight_col_index ?? null;
  const cell = hi != null && data?.row ? data.row.cells.find((c) => c.col_index === hi) : undefined;

  return (
    <Drawer wide title="Source evidence" subtitle={<span className="mono">{file}{sheet ? ` · ${sheet}` : ""} · row {ref.row_number}</span>} onClose={onClose}>
      <div className="crumbs" aria-label="Provenance path">
        <span>{file}</span>
        {sheet && <><span className="sep">›</span><span>{sheet}</span></>}
        <span className="sep">›</span><span>row {ref.row_number}</span>
        {(data?.highlight_header ?? ref.header) && <><span className="sep">›</span><span className="mono">{data?.highlight_header ?? ref.header}</span></>}
      </div>

      {cell && (
        <div className="dl" style={{ margin: "12px 0" }}>
          <div className="k">Referenced cell</div><div><span className="mono">{cell.header}</span></div>
          <div className="k">Raw value</div><div><b>{cell.value == null || cell.value === "" ? <span className="cell-empty">null</span> : cell.value}</b></div>
        </div>
      )}
      {err && <div className="banner error">{err}</div>}
      {!data && !err && <div className="muted small"><span className="spin" /> loading source row…</div>}

      {data && (
        <>
          <div className="row" style={{ justifyContent: "space-between", margin: "10px 0 6px" }}>
            <span className="muted small">Raw staged data (source, not target). Rows {data.context[0]?.row_number}–{data.context[data.context.length - 1]?.row_number} of {data.total_rows}.</span>
          </div>
          <div className="table-wrap" style={{ maxHeight: "50vh", overflow: "auto" }}>
            <table className="grid-preview">
              <thead>
                <tr><th className="rownum">#</th>{data.headers.map((h, i) => <th key={i} className={cx(i === hi && "hdr-hi")}>{h}</th>)}</tr>
              </thead>
              <tbody>
                {data.context.map((r) => (
                  <tr key={r.row_number} className={cx(r.row_number === data.row_number && "row-hi")}>
                    <td className="rownum">{r.row_number}</td>
                    {data.headers.map((_, i) => {
                      const c = r.cells.find((x) => x.col_index === i);
                      const v = c?.value;
                      const isHi = r.row_number === data.row_number && i === hi;
                      return (
                        <td key={i} className={cx(isHi && "cell-hi")} aria-label={isHi ? "referenced cell" : undefined}>
                          {v == null || v === "" ? <span className="cell-empty">null</span> : v}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Drawer>
  );
}
