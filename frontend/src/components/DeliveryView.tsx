import { useCallback, useState } from "react";
import { api } from "../api";
import type { DeliveryAttempt, DeliveryOperation, Job } from "../types";
import type { JobBundle, Section } from "../viewtypes";
import { Drawer, Icon, KPI, cx } from "./ui";

/* ---- Status styling ---- */
// "Synced" is used ONLY for a successful target write (SUCCEEDED). Everything else uses its own word.
const OP_STATUS: Record<string, { cls: string; label: string }> = {
  PLANNED:             { cls: "",      label: "Waiting" },
  IN_PROGRESS:         { cls: "blue",  label: "Syncing" },
  SUCCEEDED:           { cls: "green", label: "Synced" },
  FAILED:              { cls: "red",   label: "Failed" },
  RETRYABLE:           { cls: "amber", label: "Waiting to retry" },
  STALE_TARGET:        { cls: "amber", label: "Needs review" },
  ROLLBACK_PLANNED:    { cls: "",      label: "Undo planned" },
  ROLLBACK_IN_PROGRESS:{ cls: "blue",  label: "Undoing" },
  ROLLED_BACK:         { cls: "",      label: "Undone" },
  ROLLBACK_FAILED:     { cls: "red",   label: "Undo failed" },
  NO_CHANGE:           { cls: "",      label: "No change" },
  EXCLUDED:            { cls: "",      label: "Excluded" },
  SKIPPED_NO_CHANGE:   { cls: "",      label: "Skipped · no change" },
  SKIPPED_EXCLUDED:    { cls: "",      label: "Skipped · excluded" },
};
function OpChip({ status }: { status: string }) {
  const o = OP_STATUS[status] ?? { cls: "", label: status.replace(/_/g, " ").toLowerCase() };
  return <span className={cx("chip", o.cls)}><span className="dot" />{o.label}</span>;
}

/** Canonical order for the sync status tiles. `always` keeps a genuine health-check visible even at
 * zero (did anything fail?); everything else only earns a tile when it actually occurred — a
 * completed all-CREATE-succeeded run shows just "All" + "Synced" + "Failed", not ten empty cards. */
const STATUS_TILES: { key: string; label: string; tone?: "amber" | "green" | "red"; always?: boolean }[] = [
  { key: "PLANNED", label: "Planned" },
  { key: "SUCCEEDED", label: "Synced", tone: "green", always: true },
  { key: "FAILED", label: "Failed", tone: "red", always: true },
  { key: "RETRYABLE", label: "Waiting to retry", tone: "amber" },
  { key: "STALE_TARGET", label: "Needs review", tone: "amber" },
  { key: "ROLLBACK_FAILED", label: "Undo failed", tone: "red" },
  { key: "ROLLED_BACK", label: "Undone" },
  { key: "NO_CHANGE", label: "No change" },
  { key: "EXCLUDED", label: "Excluded" },
  { key: "SKIPPED_NO_CHANGE", label: "Skipped · no change" },
  { key: "SKIPPED_EXCLUDED", label: "Skipped · excluded" },
];

/** One clickable stat tile that IS the filter control — no separate, duplicate filter-pills row. */
function FilterTile({ label, value, tone, active, onClick }:
  { label: string; value: number; tone?: "amber" | "green" | "red"; active: boolean; onClick: () => void }) {
  const color = tone === "amber" ? "var(--amber)" : tone === "green" ? "var(--green)" : tone === "red" ? "var(--red)" : undefined;
  return (
    <button type="button" className={cx("kpi", "kpi-btn", active && "kpi-btn-active")} onClick={onClick}
      aria-pressed={active}>
      <div className="k">{label}</div>
      <div className="v" style={color ? { color } : undefined}>{value}</div>
    </button>
  );
}

const ATTEMPT_CLS: Record<string, string> = {
  succeeded: "green", failed: "red", retryable: "amber", stale_target: "amber", in_progress: "blue",
};

/* ---- Delivery states for the job ---- */
const DELIVERY_ACTIVE = new Set(["delivering", "rollback_in_progress"]);
const DELIVERY_FINAL = new Set(["migration_complete", "delivery_partial_failure", "rollback_complete", "rollback_partial_failure"]);
const CAN_DELIVER = new Set(["reconciliation_complete", "delivery_partial_failure", "stale_target_review_required"]);
const CAN_ROLLBACK = new Set(["migration_complete", "delivery_partial_failure"]);

export function DeliveryView({ job, bundle, onRefresh, go }: {
  job: Job; bundle: JobBundle; onRefresh: () => void; go: (s: Section) => void;
}) {
  const [sel, setSel] = useState<DeliveryOperation | null>(null);
  const [attempts, setAttempts] = useState<DeliveryAttempt[]>([]);
  const [busy, setBusy] = useState(false);
  const [retryBusy, setRetryBusy] = useState<string | null>(null);
  const [filter, setFilter] = useState<string>("all");

  const del = bundle.delivery;
  const ops = del?.operations ?? [];
  const counts = del?.counts ?? {};
  const hasOps = ops.length > 0;
  const isActive = DELIVERY_ACTIVE.has(job.status);

  const filtered = filter === "all" ? ops : ops.filter((o) => o.status === filter);

  const openDrawer = useCallback(async (op: DeliveryOperation) => {
    setSel(op);
    try {
      const a = await api.getDeliveryAttempts(job.id, op.id);
      setAttempts(a);
    } catch { setAttempts([]); }
  }, [job.id]);

  const nameOf = (cid: string) =>
    bundle.candidates.find((x) => x.id === cid)?.record?.full_name?.value;

  const startDelivery = async () => {
    setBusy(true);
    try { await api.startDelivery(job.id); onRefresh(); }
    catch (e) { alert(`Sync failed: ${e instanceof Error ? e.message : String(e)}`); }
    finally { setBusy(false); }
  };

  const startRollback = async () => {
    if (!confirm("Undo this sync? Every synced employee change will be reversed in the target system.")) return;
    setBusy(true);
    try { await api.startRollback(job.id); onRefresh(); }
    catch (e) { alert(`Undo failed: ${e instanceof Error ? e.message : String(e)}`); }
    finally { setBusy(false); }
  };

  const retryOp = async (opId: string) => {
    setRetryBusy(opId);
    try {
      await api.retryDeliveryOp(job.id, opId, "Manual retry from UI");
      onRefresh();
      if (sel?.id === opId) {
        const a = await api.getDeliveryAttempts(job.id, opId);
        setAttempts(a);
      }
    } catch (e) { alert(`Retry failed: ${e instanceof Error ? e.message : String(e)}`); }
    finally { setRetryBusy(null); }
  };

  // Pre-sync summary (from the completed comparison) + prerequisites for the blocked state (§K).
  const rc = bundle.reconciliation?.counts ?? {};
  const willCreate = rc.ready_create ?? 0, willUpdate = rc.ready_update ?? 0;
  const noChange = rc.no_change ?? 0, excluded = rc.excluded_target ?? 0;
  const compared = bundle.reconciliation?.summary?.result === "reconciliation_complete"
    || (bundle.reconciliation?.results?.length ?? 0) > 0;
  const openDecisions = bundle.reviews.length + bundle.proposals.length + bundle.recordReviews.length + bundle.targetReviews.length;

  return (
    <div>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <div className="section-title">Sync</div>
          <div className="section-desc">
            {isActive ? "Sending approved employee changes to the target system." :
             job.status === "migration_complete" ? "All approved employee changes were synced to the target system." :
             DELIVERY_FINAL.has(job.status) ? "Review each employee below and retry or undo as needed." :
             hasOps ? "Review what will be sent to the target system, then start the sync." :
             "Review what will be sent to the target system."}
          </div>
        </div>
        <div className="row" style={{ gap: 8 }}>
          {CAN_DELIVER.has(job.status) && (
            <button className="btn primary" disabled={busy} onClick={startDelivery}>
              {busy ? <span className="spin" /> : <Icon name="delivery" />} {hasOps ? "Sync again" : "Start sync"}
            </button>
          )}
          {CAN_ROLLBACK.has(job.status) && (counts.SUCCEEDED ?? 0) > 0 && (
            <button className="btn danger" disabled={busy} onClick={startRollback}>
              {busy ? <span className="spin" /> : <Icon name="rollback" />} Undo this sync
            </button>
          )}
        </div>
      </div>

      {/* Ready to sync, not yet started (§K): say exactly what will happen before the first write. */}
      {CAN_DELIVER.has(job.status) && !hasOps && (
        <div className="panel" style={{ marginTop: 14 }}>
          <div className="panel-b">
            <div className="stack" style={{ gap: 8 }}>
              <div><b>{willCreate + willUpdate} employee change{willCreate + willUpdate === 1 ? "" : "s"}</b> will be synced to the target system when you start.</div>
              <div className="kpis">
                <KPI label="New employees" value={willCreate} />
                <KPI label="Updates" value={willUpdate} />
                <KPI label="Unchanged (skipped)" value={noChange} />
                <KPI label="Excluded" value={excluded} />
              </div>
              <div className="muted small">Each change is sent through the target's write API with retry, optimistic concurrency, and rollback. Unchanged and excluded employees are not written.</div>
            </div>
          </div>
        </div>
      )}

      {/* Status tiles — ZERO-NOISE (§6) and INTERACTIVE: each tile IS the filter control for the
          table below (clicking it sets the same `filter` state the old separate pills used), so
          there is exactly one place that shows "what happened", not two. An "All" tile resets it. */}
      {hasOps && (() => {
        const visible = STATUS_TILES.filter((t) => t.always || (counts[t.key] ?? 0) > 0);
        return (
          <div className="kpis" style={{ marginTop: 14 }}>
            <FilterTile label="All" value={ops.length} active={filter === "all"} onClick={() => setFilter("all")} />
            {visible.map((t) => (
              <FilterTile key={t.key} label={t.label} value={counts[t.key] ?? 0} tone={t.tone}
                active={filter === t.key} onClick={() => setFilter(t.key)} />
            ))}
          </div>
        );
      })()}

      {isActive && (
        <div className="banner info" style={{ marginTop: 14 }}>
          <span className="spin" style={{ marginRight: 8 }} />
          {job.status === "delivering" ? "Syncing employee changes to the target system." :
           "Undoing the sync. Earlier changes are being reversed in the target system."}
        </div>
      )}

      {job.status === "delivery_partial_failure" && (
        <div className="banner warn" style={{ marginTop: 14 }}>
          <Icon name="warn" className="ico-xs" /> Some employee changes failed to sync. Retry the failed records, or undo this sync.
        </div>
      )}

      {job.status === "migration_complete" && (
        <div className="banner info" style={{ marginTop: 14 }}>
          Migration complete. Every approved employee change was synced.
        </div>
      )}

      {/* Operations table */}
      {hasOps && (
        <div className="panel" style={{ marginTop: 14 }}>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Employee</th>
                  <th>Name</th>
                  <th>Type</th>
                  <th>Status</th>
                  <th className="num">Attempts</th>
                  <th>Target id</th>
                  <th>Error</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {filtered.map((op) => (
                  <tr key={op.id} className="clickable" onClick={() => openDrawer(op)}>
                    <td><span className="mono">{op.employee_id ?? op.candidate_id.slice(0, 8)}</span></td>
                    <td>{nameOf(op.candidate_id) ?? <span className="cell-empty">-</span>}</td>
                    <td><span className={cx("tag", op.op_type === "CREATE" ? "core" : "structured")}>{op.op_type}</span></td>
                    <td><OpChip status={op.status} /></td>
                    <td className="num">{op.attempt_count}</td>
                    <td className="mono small">{op.target_record_id ?? "-"}</td>
                    <td className="small muted" style={{ maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {op.last_error ?? ""}
                    </td>
                    <td style={{ whiteSpace: "nowrap" }}>
                      {["FAILED", "RETRYABLE", "STALE_TARGET"].includes(op.status) && (
                        <button className="btn sm" disabled={retryBusy === op.id}
                          onClick={(e) => { e.stopPropagation(); retryOp(op.id); }}>
                          {retryBusy === op.id ? <span className="spin" /> : <Icon name="refresh" />} Retry
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {!hasOps && !isActive && !CAN_DELIVER.has(job.status) && (
        <div className="empty-state" style={{ marginTop: 20 }}>
          <Icon name="delivery" />
          <div className="stack" style={{ gap: 6, alignItems: "center", textAlign: "center" }}>
            <strong>Sync isn't available yet</strong>
            {openDecisions > 0 ? (
              <div className="muted">Sync unlocks after {openDecisions} review decision{openDecisions === 1 ? "" : "s"} {openDecisions === 1 ? "is" : "are"} resolved and the comparison finishes.</div>
            ) : !compared ? (
              <div className="muted">Sync unlocks after you compare the cleaned employees with the target system.</div>
            ) : (
              <div className="muted">Sync unlocks once the comparison is complete with no open conflicts.</div>
            )}
            <div className="prereqs">
              <span className={cx("prereq", compared && "done")}><Icon name={compared ? "check" : "dot"} className="ico-xs" /> Compared with target</span>
              <span className={cx("prereq", openDecisions === 0 && "done")}><Icon name={openDecisions === 0 ? "check" : "dot"} className="ico-xs" /> Decisions resolved</span>
            </div>
            <div className="row" style={{ gap: 8, marginTop: 4, justifyContent: "center" }}>
              {openDecisions > 0 && <button type="button" className="btn primary" onClick={() => go("reviews")}>Go to Needs review <Icon name="arrow" /></button>}
              {openDecisions === 0 && <button type="button" className="btn" onClick={() => go("reconcile")}>Open Compare with target <Icon name="arrow" /></button>}
            </div>
          </div>
        </div>
      )}

      {/* Attempt drawer */}
      {sel && (
        <Drawer wide
          title={`${nameOf(sel.candidate_id) ?? "Employee"} · ${sel.op_type}`}
          subtitle={<><OpChip status={sel.status} /> <span className="mono small" style={{ marginLeft: 6 }}>{sel.id}</span></>}
          onClose={() => { setSel(null); setAttempts([]); }}>
          <div className="dl" style={{ marginBottom: 14 }}>
            <div className="k">Operation type</div><div>{sel.op_type}</div>
            <div className="k">Employee</div><div className="mono">{sel.candidate_id}</div>
            <div className="k">Employee id</div><div className="mono">{sel.employee_id ?? "-"}</div>
            <div className="k">Target record</div><div className="mono">{sel.target_record_id ?? "-"}</div>
            <div className="k">Expected revision</div><div>{sel.expected_target_revision ?? "-"}</div>
            <div className="k">Revision after</div><div>{sel.target_revision_after ?? "-"}</div>
            <div className="k">Idempotency key</div><div className="mono small">{sel.idempotency_key}</div>
            <div className="k">Target request id</div><div className="mono small">{sel.target_request_id ?? "-"}</div>
            {sel.last_error && <>
              <div className="k" style={{ color: "var(--red)" }}>Last error</div>
              <div style={{ color: "var(--red)" }}>{sel.last_error}</div>
            </>}
          </div>

          {["FAILED", "RETRYABLE", "STALE_TARGET"].includes(sel.status) && (
            <div className="toolbar">
              <button className="btn primary" disabled={retryBusy === sel.id}
                onClick={() => retryOp(sel.id)}>
                {retryBusy === sel.id ? <span className="spin" /> : <Icon name="refresh" />} Retry this operation
              </button>
            </div>
          )}

          <h4 className="h5">Attempt ledger ({attempts.length})</h4>
          {attempts.length > 0 ? (
            <div className="panel" style={{ marginTop: 6 }}>
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th className="num">#</th>
                      <th>Action</th>
                      <th>Result</th>
                      <th className="num">HTTP</th>
                      <th>Error category</th>
                      <th>Request id</th>
                      <th>Started</th>
                      <th>Duration</th>
                    </tr>
                  </thead>
                  <tbody>
                    {attempts.map((a) => {
                      const dur = a.completed_at && a.started_at
                        ? `${((new Date(a.completed_at).getTime() - new Date(a.started_at).getTime()) / 1000).toFixed(2)}s`
                        : "-";
                      return (
                        <tr key={a.id}>
                          <td className="num">{a.attempt_no}</td>
                          <td className="small">{a.action}</td>
                          <td><span className={cx("chip", ATTEMPT_CLS[a.result] ?? "")}><span className="dot" />{a.result}</span></td>
                          <td className="num">{a.http_status ?? "-"}</td>
                          <td className="small muted">{a.error_category ?? "-"}</td>
                          <td className="mono small">{a.target_request_id ?? "-"}</td>
                          <td className="small muted nowrap">{a.started_at ? new Date(a.started_at).toLocaleTimeString() : "-"}</td>
                          <td className="small muted">{dur}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          ) : (
            <div className="muted small" style={{ marginTop: 4 }}>No attempts recorded yet.</div>
          )}

          {sel.payload && (
            <>
              <h4 className="h5">Payload</h4>
              <pre className="tech-json">{JSON.stringify(sel.payload, null, 2)}</pre>
            </>
          )}

          {sel.before_snapshot && (
            <>
              <h4 className="h5">Before snapshot</h4>
              <pre className="tech-json">{JSON.stringify(sel.before_snapshot, null, 2)}</pre>
            </>
          )}
        </Drawer>
      )}
    </div>
  );
}
