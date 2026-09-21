import { useEffect, useRef, useState, type ReactNode } from "react";
import { methodLabel } from "../labels";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

/* ---- Minimal stroke icon set (16x16, currentColor) ---- */
const PATHS: Record<string, ReactNode> = {
  overview: <><rect x="2" y="2" width="5" height="5" rx="1" /><rect x="9" y="2" width="5" height="5" rx="1" /><rect x="2" y="9" width="5" height="5" rx="1" /><rect x="9" y="9" width="5" height="5" rx="1" /></>,
  files: <><path d="M4 2h5l3 3v9H4z" /><path d="M9 2v3h3" /></>,
  mapping: <><circle cx="4" cy="8" r="2" /><circle cx="12" cy="8" r="2" /><path d="M6 8h4" /></>,
  prepared: <><circle cx="8" cy="5" r="2.4" /><path d="M3 14c0-2.8 2.2-4.5 5-4.5s5 1.7 5 4.5" /></>,
  reviews: <><path d="M8 2l1.6 3.7 4 .3-3 2.6.9 3.9L8 10.9 4.5 12.5l.9-3.9-3-2.6 4-.3z" /></>,
  reconcile: <><path d="M3 5h7" /><path d="M8 3l2 2-2 2" /><path d="M13 11H6" /><path d="M8 9l-2 2 2 2" /></>,
  audit: <><path d="M3 3h10v10H3z" /><path d="M5 6h6M5 8.5h6M5 11h3" /></>,
  schema: <><path d="M2 4h12M2 8h12M2 12h12" /><path d="M6 2v12" /></>,
  sun: <><circle cx="8" cy="8" r="3" /><path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.4 1.4M11.6 11.6L13 13M13 3l-1.4 1.4M4.4 11.6L3 13" /></>,
  moon: <path d="M13 9.5A5.5 5.5 0 016.5 3 5.5 5.5 0 1013 9.5z" />,
  close: <path d="M4 4l8 8M12 4l-8 8" />,
  inspect: <><circle cx="7" cy="7" r="4" /><path d="M10 10l4 4" /></>,
  chevron: <path d="M6 4l4 4-4 4" />,
  check: <path d="M3 8.5l3 3 7-7" />,
  dot: <circle cx="8" cy="8" r="3" />,
  upload: <><path d="M8 10V3M5 6l3-3 3 3" /><path d="M3 11v2h10v-2" /></>,
  refresh: <><path d="M13 8a5 5 0 10-1.5 3.5" /><path d="M13 5v3h-3" /></>,
  link: <><path d="M6.5 9.5l3-3" /><path d="M7 4.5l1.2-1.2a2.5 2.5 0 013.5 3.5L10.5 8" /><path d="M9 11.5l-1.2 1.2a2.5 2.5 0 01-3.5-3.5L5.5 8" /></>,
  warn: <><path d="M8 2.5l6 11H2z" /><path d="M8 6.5v3" /><circle cx="8" cy="11.6" r=".6" /></>,
  plus: <path d="M8 3v10M3 8h10" />,
  collection: <><rect x="2" y="3" width="12" height="3" rx="1" /><rect x="2" y="9.5" width="12" height="3" rx="1" /></>,
  custom: <><path d="M8 2l1.5 3.2 3.5.4-2.6 2.4.7 3.5L8 9.8 4.9 11.5l.7-3.5L3 5.6l3.5-.4z" /></>,
  tenant: <><path d="M3 13V6l5-3 5 3v7" /><path d="M6 13v-4h4v4" /></>,
  arrow: <path d="M3 8h9M9 5l3 3-3 3" />,
  delivery: <><path d="M4 2h8v12H4z" /><path d="M7 7h2M7 9.5h2" /><path d="M6 4.5l1.3 1.3 2.7-2.8" /></>,
  rollback: <><path d="M3 8a5 5 0 019.5-2" /><path d="M3 5v3h3" /><path d="M13 8a5 5 0 01-9.5 2" /><path d="M13 11v-3h-3" /></>,
  metrics: <><path d="M2 14V2" /><path d="M2 14h12" /><rect x="4" y="8" width="2.4" height="4" /><rect x="8" y="5" width="2.4" height="7" /><rect x="12" y="9.5" width="0" height="2.5" /><path d="M12 12V6" /></>,
  copy: <><rect x="5" y="5" width="8" height="8" rx="1" /><path d="M3 10V3h7" /></>,
  dots: <><circle cx="8" cy="3.2" r="1.1" /><circle cx="8" cy="8" r="1.1" /><circle cx="8" cy="12.8" r="1.1" /></>,
  trash: <><path d="M3 4.5h10" /><path d="M5.5 4.5V3h5v1.5" /><path d="M4.5 4.5l.7 8.5h5.6l.7-8.5" /><path d="M6.7 7v4M9.3 7v4" /></>,
  edit: <><path d="M11 3.5l1.5 1.5-7 7L3.5 13l1-2z" /><path d="M9.5 5l1.5 1.5" /></>,
};

export function Icon({ name, className }: { name: string; className?: string }) {
  return (
    <svg className={className} viewBox="0 0 16 16" width="16" height="16" fill="none"
      stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      {PATHS[name] ?? PATHS.dot}
    </svg>
  );
}

export function Drawer({ title, subtitle, onClose, children, wide }:
  { title: string; subtitle?: ReactNode; onClose: () => void; children: ReactNode; wide?: boolean }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);
  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className={cx("drawer", wide && "wide")} role="dialog" aria-label={title}>
        <div className="drawer-h">
          <div className="stack" style={{ gap: 1 }}>
            <h3>{title}</h3>
            {subtitle && <div className="small muted">{subtitle}</div>}
          </div>
          <div className="spacer" />
          <button className="icon-btn" onClick={onClose} aria-label="Close"><Icon name="close" /></button>
        </div>
        <div className="drawer-b">{children}</div>
      </aside>
    </>
  );
}

/* ---- Status chips ---- */
const PARSE_CLASS: Record<string, string> = {
  parsed: "green", failed: "red", processing: "blue", queued: "", uploaded: "",
};
export function StatusChip({ status }: { status: string | null | undefined }) {
  const s = status || "unknown";
  return <span className={cx("chip", PARSE_CLASS[s])}><span className="dot" />{s}</span>;
}

const WORK_CLASS: Record<string, string> = {
  succeeded: "green", failed: "red", processing: "blue", retryable: "amber", pending: "", cancelled: "",
};
export function WorkChip({ status }: { status: string }) {
  return <span className={cx("chip", WORK_CLASS[status])}><span className="dot" />{status}</span>;
}

const OUTCOME: Record<string, { cls: string; label: string }> = {
  READY_CREATE: { cls: "blue", label: "New employee" },
  READY_UPDATE: { cls: "blue", label: "Existing employee" },
  NO_CHANGE: { cls: "", label: "No changes" },
  REVIEW_REQUIRED: { cls: "amber", label: "Needs review" },
  EXCLUDED: { cls: "red", label: "Excluded" },
};
export function OutcomeChip({ outcome }: { outcome: string }) {
  const o = OUTCOME[outcome] ?? { cls: "", label: outcome };
  return <span className={cx("chip", o.cls)}><span className="dot" />{o.label}</span>;
}

export function MethodTag({ method }: { method: string | null | undefined }) {
  const m = (method || "").toLowerCase();
  const known = ["rule", "model", "human"].includes(m);
  return <span className={cx("tag", known ? m : "unresolved")}>{methodLabel(method)}</span>;
}

/* ---- Mapping destination kind (M3G §E1/§R): human category labels, colour class preserved ---- */
const DEST: Record<string, { cls: string; label: string }> = {
  CORE_FIELD: { cls: "core", label: "Standard" },
  COLLECTION_FIELD: { cls: "structured", label: "Related record" },
  CUSTOM_FIELD: { cls: "custom", label: "Organization field" },
  UNMAPPED: { cls: "unmapped", label: "Not mapped" },
  NEEDS_REVIEW: { cls: "unresolved", label: "Needs review" },
  IGNORED: { cls: "ignored", label: "Not migrated" },
  PROPOSAL: { cls: "proposal", label: "New organization field" },
};
export function DestinationTag({ kind }: { kind: string | null | undefined }) {
  const d = DEST[kind || ""] ?? { cls: "unmapped", label: "Not mapped" };
  return <span className={cx("tag", d.cls)}>{d.label}</span>;
}

/** Explicit textual label for a field that requires review — never colour alone. */
export function ConflictTag({ label = "Conflict" }: { label?: string }) {
  return <span className="tag conflict" role="status"><Icon name="warn" className="ico-xs" /> {label}</span>;
}

/** One provenance line: file · sheet · row · header  [raw]  [View source]. */
export function ProvLine({ p, onView }: { p: any; onView?: (p: any) => void }) {
  if (!p) return null;
  return (
    <span className="prov">
      <span>{p.original_filename}</span>
      {p.sheet_name && <><span className="arrow">·</span><span>{p.sheet_name}</span></>}
      {p.row_number != null && <><span className="arrow">·</span><span>row {p.row_number}</span></>}
      {p.header && <><span className="arrow">·</span><span className="mono">{p.header}</span></>}
      {p.raw != null && p.raw !== "" && <span className="chip sq">{String(p.raw)}</span>}
      {onView && p.table_id && p.row_number != null && (
        <button type="button" className="link-btn" onClick={(e) => { e.preventDefault(); e.stopPropagation(); onView(p); }}>
          <Icon name="link" className="ico-xs" /> View source
        </button>
      )}
    </span>
  );
}

/** Copy the internal migration (job) ID. Keeps the raw ID out of primary titles (M3G §C2/§D):
 * the ID lives behind this small secondary action, with the full value in the tooltip. */
export function CopyId({ id, label = "Copy migration ID", className }:
  { id: string; label?: string; className?: string }) {
  const [done, setDone] = useState(false);
  const copy = async (e: React.MouseEvent) => {
    e.preventDefault(); e.stopPropagation();
    try { await navigator.clipboard.writeText(id); } catch { /* clipboard blocked — ignore */ }
    setDone(true); setTimeout(() => setDone(false), 1400);
  };
  return (
    <button type="button" className={cx("btn ghost sm copy-id", className)} onClick={copy}
      title={`Migration ID: ${id}`} aria-label={label}>
      <Icon name={done ? "check" : "copy"} className="ico-xs" /> {done ? "Copied" : label}
    </button>
  );
}

/** Centered confirmation modal for a destructive action. Handles the async confirm, a busy state,
 *  and an inline error (so a 409 "wait for the current step" is shown in place, never a raw alert). */
export function ConfirmDialog({ title, children, confirmLabel, tone = "danger", busyLabel,
  onConfirm, onClose, disabled }:
  { title: string; children: ReactNode; confirmLabel: string; tone?: "danger" | "primary";
    busyLabel?: string; onConfirm: () => Promise<void> | void; onClose: () => void; disabled?: boolean }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose, busy]);
  const confirm = async () => {
    setBusy(true); setErr(null);
    try { await onConfirm(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); setBusy(false); }
  };
  return (
    <>
      <div className="modal-backdrop" onClick={() => !busy && onClose()} />
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal-h"><h3>{title}</h3></div>
        <div className="modal-b">{children}{err && <div className="banner error" style={{ marginTop: 12 }}>{err}</div>}</div>
        <div className="modal-f">
          <button className="btn" onClick={onClose} disabled={busy}>Cancel</button>
          <button className={cx("btn", tone)} onClick={confirm} disabled={busy || disabled}>
            {busy ? (busyLabel ?? "Working…") : confirmLabel}
          </button>
        </div>
      </div>
    </>
  );
}

/** Small overflow (⋯) menu. Opens on click; closes on Escape or a mousedown OUTSIDE its container
 *  (so a menu-item click still fires). `stop` guards clicks from a wrapping <Link>/<a>. */
export function OverflowMenu({ items, label = "More actions", size, stop }:
  { items: { key: string; label: string; icon?: string; danger?: boolean; onClick: () => void }[];
    label?: string; size?: "sm"; stop?: boolean }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [open]);
  const guard = (e: React.MouseEvent) => { if (stop) { e.preventDefault(); e.stopPropagation(); } };
  return (
    <span ref={ref} className="ovf" style={{ position: "relative" }} onClick={guard}>
      <button type="button" className={cx("icon-btn", size === "sm" && "sm")} aria-haspopup="menu"
        aria-expanded={open} aria-label={label} title={label}
        onClick={(e) => { guard(e); setOpen((o) => !o); }}>
        <Icon name="dots" />
      </button>
      {open && (
        <div className="menu" role="menu">
          {items.map((it) => (
            <button key={it.key} type="button" role="menuitem"
              className={cx("menu-item", it.danger && "danger")}
              onClick={(e) => { guard(e); setOpen(false); it.onClick(); }}>
              {it.icon && <Icon name={it.icon} className="ico-xs" />} {it.label}
            </button>
          ))}
        </div>
      )}
    </span>
  );
}

export function KPI({ label, value, sub, tone }:
  { label: string; value: ReactNode; sub?: ReactNode; tone?: "amber" | "green" | "red" }) {
  const color = tone === "amber" ? "var(--amber)" : tone === "green" ? "var(--green)" : tone === "red" ? "var(--red)" : undefined;
  return (
    <div className="kpi">
      <div className="k">{label}</div>
      <div className="v" style={color ? { color } : undefined}>{value}{sub != null && <small> {sub}</small>}</div>
    </div>
  );
}
