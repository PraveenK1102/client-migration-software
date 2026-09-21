import { useCallback, useEffect, useRef, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "./api";
import type {
  CustomFieldProposal, DecisionAction, Health, Job, ProposalDecisionBody, RecordAction, RecordIssue,
  ReviewIssue, TargetAction, TargetReviewIssue, TargetSchema, Tenant,
} from "./types";
import type { JobBundle, Section } from "./viewtypes";
import { Icon } from "./components/ui";
import { Shell, JOB_NAV } from "./components/Shell";
import { MigrationsView } from "./components/MigrationsView";
import { MigrationOverflow } from "./components/MigrationActions";
import { UploadPanel } from "./components/UploadPanel";
import { Overview } from "./components/Overview";
import { SourceFilesView } from "./components/SourceFilesView";
import { MappingView } from "./components/MappingView";
import { PreparedView } from "./components/PreparedView";
import { ReviewsView } from "./components/ReviewsView";
import { ReconciliationView } from "./components/ReconciliationView";
import { DeliveryView } from "./components/DeliveryView";
import { MetricsView } from "./components/MetricsView";
import { AuditView } from "./components/AuditView";
import { SchemaView } from "./components/SchemaView";
import { NAV_LABELS, humanStatus, migrationName, orgLabel } from "./labels";
import type { RecordDecisionExtra } from "./components/RecordReviewCard";

const TERMINAL = new Set(["reconciliation_complete", "migration_complete", "delivery_partial_failure",
  "rollback_complete", "rollback_partial_failure", "error"]);
const EMPTY: JobBundle = {
  files: [], workItems: [], profiles: null, mappings: null, reviews: [], recordReviews: [],
  candidates: [], reconciliation: null, targetReviews: [], audit: [], proposals: [], tenantFields: [],
  delivery: null,
};
const JOB_SECTIONS = new Set<Section>(JOB_NAV.map((n) => n.key));

// ─────────────────────────────────────────────────────────────────────────────────────────────────
// Migration list (landing) — /migrations
// ─────────────────────────────────────────────────────────────────────────────────────────────────
function MigrationsPage() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setJobs(await api.listJobs()); setError(null); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 4000); // keep running migrations fresh
    return () => window.clearInterval(t);
  }, [load]);

  return (
    <Shell active="migrations" title="Migrations" subtitle="Your migration workspace">
      <MigrationsView jobs={jobs} loading={loading} error={error} onRefresh={load} />
    </Shell>
  );
}

// ─────────────────────────────────────────────────────────────────────────────────────────────────
// New migration — /migrations/new
// ─────────────────────────────────────────────────────────────────────────────────────────────────
function NewMigrationPage() {
  const nav = useNavigate();
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.listTenants().then(setTenants).catch(() => setTenants([]));
    api.health().then(setHealth).catch(() => setHealth(null));
  }, []);

  const onUpload = async (files: File[], tenantId: string) => {
    setError(null);
    try {
      const j = await api.createJob(files, tenantId);
      nav(`/jobs/${j.id}`);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  return (
    <Shell active="new" title="New migration" subtitle="Upload a source HR export to begin">
      <UploadPanel onUpload={onUpload} error={error} tenants={tenants}
        defaultTenant={health?.default_tenant_id ?? "default"} />
    </Shell>
  );
}

// ─────────────────────────────────────────────────────────────────────────────────────────────────
// Target fields (reference schema) — /schema
// ─────────────────────────────────────────────────────────────────────────────────────────────────
function SchemaPage() {
  const [schema, setSchema] = useState<TargetSchema | null>(null);
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [tenantId, setTenantId] = useState<string | null>(null);

  useEffect(() => {
    api.health().then((h) => setTenantId((t) => t ?? h.default_tenant_id ?? "default")).catch(() => setTenantId("default"));
    api.listTenants().then(setTenants).catch(() => setTenants([]));
  }, []);
  useEffect(() => { api.schema(tenantId).then(setSchema).catch(() => setSchema(null)); }, [tenantId]);

  return (
    <Shell active="schema" title={NAV_LABELS.schema} subtitle="The employee fields this migration can write to">
      <SchemaView schema={schema} tenants={tenants} tenantId={tenantId} onTenantChange={setTenantId} />
    </Shell>
  );
}

// ─────────────────────────────────────────────────────────────────────────────────────────────────
// One migration — /jobs/:jobId[/:section]
// ─────────────────────────────────────────────────────────────────────────────────────────────────
function JobPage() {
  const nav = useNavigate();
  const { jobId: urlJobId, section: urlSection } = useParams();
  const [jobSchema, setJobSchema] = useState<TargetSchema | null>(null);
  const [baseSchema, setBaseSchema] = useState<TargetSchema | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [bundle, setBundle] = useState<JobBundle>(EMPTY);
  const [reconBusy, setReconBusy] = useState(false);
  const [notFound, setNotFound] = useState(false);
  const timer = useRef<number | null>(null);

  const section: Section = urlSection && JOB_SECTIONS.has(urlSection as Section)
    ? (urlSection as Section) : "overview";

  useEffect(() => {
    api.schema().then(setBaseSchema).catch(() => setBaseSchema(null));
  }, []);

  const refresh = useCallback(async (jobId: string) => {
    try {
      const j = await api.getJob(jobId);
      setJob(j);
      const reachedPrep = ["preparation_complete", "reconciling_target", "awaiting_target_review",
        "reconciliation_complete", "delivering", "migration_complete", "delivery_partial_failure",
        "stale_target_review_required", "rollback_in_progress", "rollback_complete", "rollback_partial_failure"].includes(j.status);
      const reachedDelivery = ["delivering", "migration_complete", "delivery_partial_failure",
        "stale_target_review_required", "rollback_in_progress", "rollback_complete", "rollback_partial_failure"].includes(j.status);
      const [profiles, mappings, reviews, audit, recordReviews, candidates, files, workItems, reconciliation, targetReviews, proposals, tenantFields, schema, delivery] =
        await Promise.all([
          api.getProfiles(jobId).catch(() => null),
          api.getMappings(jobId).catch(() => null),
          api.getReviews(jobId).catch(() => []),
          api.getAudit(jobId).catch(() => []),
          api.getRecordReviews(jobId).catch(() => []),
          api.getCandidates(jobId).catch(() => []),
          api.getFiles(jobId).catch(() => []),
          api.getWorkItems(jobId).catch(() => []),
          reachedPrep ? api.getReconciliation(jobId).catch(() => null) : Promise.resolve(null),
          api.getTargetReviews(jobId).catch(() => []),
          api.getProposals(jobId).catch(() => []),
          api.getTenantCustomFields(j.tenant_id).catch(() => []),
          api.getJobSchema(jobId).catch(() => null),
          reachedDelivery ? api.getDelivery(jobId).catch(() => null) : Promise.resolve(null),
        ]);
      setBundle({ profiles, mappings, reviews: reviews || [], audit: audit || [],
        recordReviews: recordReviews || [], candidates: candidates || [], files: files || [],
        workItems: workItems || [], reconciliation, targetReviews: targetReviews || [],
        proposals: proposals || [], tenantFields: tenantFields || [], delivery });
      if (schema) setJobSchema(schema);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) setNotFound(true);
    }
  }, []);

  useEffect(() => { if (urlJobId) { setNotFound(false); refresh(urlJobId); } }, [urlJobId, refresh]);

  useEffect(() => {
    if (timer.current) window.clearInterval(timer.current);
    if (job) {
      const ms = TERMINAL.has(job.status) ? 5000 : 1500;
      timer.current = window.setInterval(() => refresh(job.id), ms);
    }
    return () => { if (timer.current) window.clearInterval(timer.current); };
  }, [job?.id, job?.status, refresh]);

  const go = useCallback((s: Section) => {
    if (!urlJobId) return;
    if (s === "schema") { nav("/schema"); return; }
    nav(s === "overview" ? `/jobs/${urlJobId}` : `/jobs/${urlJobId}/${s}`);
  }, [urlJobId, nav]);

  const onDecision = async (issue: ReviewIssue, action: DecisionAction, correctedTarget: string | null, reason: string) => {
    if (!job) return;
    try {
      await api.submitDecision(job.id, issue.id, { version: issue.version, action, corrected_target: correctedTarget, reason: reason || null });
      await refresh(job.id);
    } catch (e) { alert(`Decision failed: ${e instanceof Error ? e.message : String(e)}`); await refresh(job.id); }
  };
  const onRecordDecision = async (issue: RecordIssue, action: RecordAction, extra?: RecordDecisionExtra) => {
    if (!job) return;
    try {
      await api.submitRecordDecision(job.id, issue.id, { version: issue.version, action, ...extra });
      await refresh(job.id);
    } catch (e) { alert(`Record decision failed: ${e instanceof Error ? e.message : String(e)}`); await refresh(job.id); }
  };
  const onReconcile = async () => {
    if (!job) return;
    setReconBusy(true);
    try { await api.startReconciliation(job.id); await refresh(job.id); }
    catch (e) { alert(`Compare failed: ${e instanceof Error ? e.message : String(e)}`); }
    finally { setReconBusy(false); }
  };
  const onTargetDecision = async (issue: TargetReviewIssue, action: TargetAction, note: string | null) => {
    if (!job) return;
    try {
      await api.submitTargetDecision(job.id, issue.id, { version: issue.version, action, note });
      await refresh(job.id);
    } catch (e) { alert(`Decision failed: ${e instanceof Error ? e.message : String(e)}`); await refresh(job.id); }
  };
  const onProposalDecision = async (p: CustomFieldProposal, body: Omit<ProposalDecisionBody, "version">) => {
    if (!job) return;
    await api.decideProposal(job.id, p.id, { version: p.version, ...body });
    await refresh(job.id);
  };
  const onRetryMapping = async () => {
    if (!job) return;
    try { await api.retryMapping(job.id); await refresh(job.id); }
    catch (e) { alert(`Retry failed: ${e instanceof Error ? e.message : String(e)}`); }
  };

  const openReviews = bundle.reviews.length + bundle.recordReviews.length + bundle.targetReviews.length + bundle.proposals.length;
  const badge = (k: Section): { n: number; warn: boolean } | null => {
    if (!job) return null;
    if (k === "files") return { n: job.counts.files ?? 0, warn: (job.counts.files_failed ?? 0) > 0 };
    if (k === "prepared") return { n: job.counts.candidates ?? 0, warn: false };
    if (k === "reviews") return openReviews ? { n: openReviews, warn: true } : null;
    if (k === "reconcile") return bundle.targetReviews.length ? { n: bundle.targetReviews.length, warn: true } : null;
    if (k === "delivery") {
      const dops = bundle.delivery?.operations?.length ?? 0;
      const dfail = (bundle.delivery?.counts?.FAILED ?? 0) + (bundle.delivery?.counts?.RETRYABLE ?? 0) + (bundle.delivery?.counts?.STALE_TARGET ?? 0);
      return dops ? { n: dops, warn: dfail > 0 } : null;
    }
    return null;
  };

  const schema = job ? (jobSchema ?? baseSchema) : baseSchema;
  const title = job ? migrationName(job.source_filenames, job.id) : "Migration";
  const subtitle = job
    ? <>{orgLabel(job.tenant_id)} · {humanStatus(job.status)}</>
    : "Loading…";

  const banner = notFound
    ? <div className="banner error">This migration was not found. <button className="btn sm" onClick={() => nav("/migrations")} style={{ marginLeft: 6 }}>Back to Migrations</button></div>
    : job?.status === "blocked_provider"
      ? <div className="banner warn">
          {bundle.proposals.length
            ? <>{bundle.proposals.length} source field{bundle.proposals.length === 1 ? "" : "s"} could not be matched automatically and no AI model provider is configured. Decide {bundle.proposals.length === 1 ? "it" : "them"} under <button className="btn sm" onClick={() => go("reviews")} style={{ margin: "0 4px" }}>{NAV_LABELS.reviews}</button> — matching re-runs automatically — or configure <code className="inline">GROQ_API_KEY</code> in backend/.env, restart, then <button className="btn sm" onClick={onRetryMapping} style={{ marginLeft: 6 }}>Retry matching</button>.</>
            : <>This migration needs AI interpretation for some columns and no model provider is configured. Automatic matches are preserved. Configure <code className="inline">GROQ_API_KEY</code> in backend/.env, restart, then <button className="btn sm" onClick={onRetryMapping} style={{ marginLeft: 6 }}>Retry matching</button>.</>}
        </div>
      : job?.status === "error"
        ? <div className="banner error">Migration error: {job.error}</div>
        : null;

  const headerActions = job
    ? <MigrationOverflow job={job} onDeleted={() => nav("/migrations")} />
    : undefined;

  return (
    <Shell active="job" title={title} subtitle={subtitle} job={job} section={section} onSection={go} badge={badge} banner={banner} headerActions={headerActions}>
      {!job ? (
        notFound ? null : <div className="empty"><Icon name="refresh" /><div className="muted">Loading migration…</div></div>
      ) : (
        <>
          {section === "overview" && <Overview job={job} bundle={bundle} go={go} />}
          {section === "files" && <SourceFilesView jobId={job.id} bundle={bundle} onChanged={() => refresh(job.id)} />}
          {section === "mapping" && <MappingView bundle={bundle} schema={schema} go={go} />}
          {section === "prepared" && <PreparedView jobId={job.id} bundle={bundle} schema={schema} jobStatus={job.status} onChanged={() => refresh(job.id)} />}
          {section === "reviews" && <ReviewsView jobId={job.id} bundle={bundle} schema={schema} onDecision={onDecision}
            onRecordDecision={onRecordDecision} onTargetDecision={onTargetDecision} onProposalDecision={onProposalDecision} />}
          {section === "reconcile" && <ReconciliationView job={job} bundle={bundle} schema={schema} onReconcile={onReconcile} busy={reconBusy} go={go} />}
          {section === "delivery" && <DeliveryView job={job} bundle={bundle} onRefresh={() => refresh(job.id)} go={go} />}
          {section === "metrics" && <MetricsView jobId={job.id} />}
          {section === "audit" && <AuditView jobId={job.id} bundle={bundle} schema={schema} jobStatus={job.status} onChanged={() => refresh(job.id)} />}
        </>
      )}
    </Shell>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Navigate to="/migrations" replace />} />
        <Route path="/migrations" element={<MigrationsPage />} />
        <Route path="/migrations/new" element={<NewMigrationPage />} />
        <Route path="/schema" element={<SchemaPage />} />
        <Route path="/jobs/:jobId" element={<JobPage />} />
        <Route path="/jobs/:jobId/:section" element={<JobPage />} />
        <Route path="*" element={<Navigate to="/migrations" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
