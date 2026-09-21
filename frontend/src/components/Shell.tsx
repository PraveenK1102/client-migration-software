import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { Health, Job } from "../types";
import type { Section } from "../viewtypes";
import { useTheme } from "../theme";
import { Icon, cx } from "./ui";
import { NAV_LABELS } from "../labels";

/** Job sections in workflow order, with plain-English labels (addendum §4). */
export const JOB_NAV: { key: Section; label: string; icon: string }[] = [
  { key: "overview", label: NAV_LABELS.overview, icon: "overview" },
  { key: "files", label: NAV_LABELS.files, icon: "files" },
  { key: "mapping", label: NAV_LABELS.mapping, icon: "mapping" },
  { key: "prepared", label: NAV_LABELS.prepared, icon: "prepared" },
  { key: "reviews", label: NAV_LABELS.reviews, icon: "reviews" },
  { key: "reconcile", label: NAV_LABELS.reconcile, icon: "reconcile" },
  { key: "delivery", label: NAV_LABELS.delivery, icon: "delivery" },
  { key: "metrics", label: NAV_LABELS.metrics, icon: "metrics" },
  { key: "audit", label: NAV_LABELS.audit, icon: "audit" },
];

interface ShellProps {
  active: "migrations" | "new" | "schema" | "job";
  title: string;
  subtitle?: ReactNode;
  job?: Job | null;
  section?: Section;
  onSection?: (s: Section) => void;
  badge?: (s: Section) => { n: number; warn: boolean } | null;
  banner?: ReactNode;
  headerActions?: ReactNode;
  children: ReactNode;
}

/** Shared app frame: brand + topbar + always-available sidebar (Migrations / New migration / this
 * migration's sections / Target fields) + main. Navigation is never hidden, so from any screen you can
 * get back to the full migration list. */
export function Shell({ active, title, subtitle, job, section, onSection, badge, banner, headerActions, children }: ShellProps) {
  const { theme, toggle } = useTheme();
  const [health, setHealth] = useState<Health | null>(null);
  const [provOpen, setProvOpen] = useState(false);

  useEffect(() => { api.health().then(setHealth).catch(() => setHealth(null)); }, []);

  return (
    <div className="shell">
      <div className="brand">
        <div className="logo">EM</div>
        <div className="name">Employee Migration</div>
      </div>

      <div className="topbar">
        <div className="stack" style={{ gap: 0, minWidth: 0 }}>
          <div className="crumb">{title}</div>
          {subtitle && <div className="sub">{subtitle}</div>}
        </div>
        <div className="spacer" />
        {health && (
          <div style={{ position: "relative" }}>
            <button className="provider" onClick={() => setProvOpen((o) => !o)} aria-expanded={provOpen}>
              <span className="lbl">Model</span>
              <span className="st">{(health.model_id || "").replace("openai/", "").toUpperCase()}</span>
              <span className={cx("chip", health.configured ? "green" : "")} style={{ padding: "0 7px" }}>
                <span className="dot" />{health.configured ? "configured" : "not configured"}</span>
            </button>
            {provOpen && (
              <div className="panel" style={{ position: "absolute", right: 0, top: 38, width: 320, zIndex: 30 }}>
                <div className="panel-b small muted">
                  Clear column matches are handled automatically. Columns that need interpretation pause for
                  your review — or wait for the AI model provider if one is configured.
                  {health.env_file_exists ? "" : " No backend/.env found."}
                </div>
              </div>
            )}
          </div>
        )}
        <button className="icon-btn" onClick={toggle} aria-label="Toggle theme"
          title={theme === "light" ? "Dark mode" : "Light mode"}>
          <Icon name={theme === "light" ? "moon" : "sun"} />
        </button>
        {/* "New migration" lives only in the global sidebar + Migrations landing (not duplicated here). */}
        {headerActions}
      </div>

      <nav className="sidebar">
        <div className="nav-group">
          <Link className={cx("nav-item", active === "migrations" && "active")} to="/migrations">
            <Icon name="collection" className="ico" /> {NAV_LABELS.migrations}
          </Link>
          <Link className={cx("nav-item", active === "new" && "active")} to="/migrations/new">
            <Icon name="plus" className="ico" /> {NAV_LABELS.new}
          </Link>

          {job && onSection && (
            <>
              <div className="nav-label">This migration</div>
              {JOB_NAV.map((n) => {
                const b = badge?.(n.key) ?? null;
                return (
                  <button key={n.key} className={cx("nav-item", section === n.key && "active", b?.warn && "warn")}
                    onClick={() => onSection(n.key)}>
                    <Icon name={n.icon} className="ico" /> {n.label}
                    {b && <span className="count">{b.n}</span>}
                  </button>
                );
              })}
            </>
          )}

          <div className="nav-label">Reference</div>
          <Link className={cx("nav-item", active === "schema" && "active")} to="/schema">
            <Icon name="schema" className="ico" /> {NAV_LABELS.schema}
          </Link>
        </div>
      </nav>

      <main className="main">
        <div className="main-inner">
          {banner}
          {children}
        </div>
      </main>
    </div>
  );
}
