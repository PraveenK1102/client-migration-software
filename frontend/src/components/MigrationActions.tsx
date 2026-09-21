import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import type { Job } from "../types";
import { ConfirmDialog, OverflowMenu } from "./ui";
import { humanStatus, orgLabel } from "../labels";

/** True when the target still holds (or may hold) migration changes that were not rolled back — the
 *  same rule the backend enforces (SUCCEEDED / in-flight / failed-rollback delivery operations). */
export function hasUnrevertedTargetWrites(job: Job): boolean {
  const c = job.counts ?? {};
  return (Number(c.delivery_succeeded) || 0) + (Number(c.delivery_processing) || 0)
    + (Number(c.delivery_rollback_failed) || 0) > 0;
}

export function DeleteMigrationDialog({ job, onClose, onDeleted }:
  { job: Job; onClose: () => void; onDeleted: () => void }) {
  const nav = useNavigate();
  const files = job.source_filenames?.length ?? job.counts?.files ?? 0;
  const employees = job.row_count ?? job.counts?.candidates ?? 0;

  if (hasUnrevertedTargetWrites(job)) {
    // Synced target: deletion is not offered at all — the only action is to undo the sync first.
    return (
      <ConfirmDialog title="Undo the sync first" confirmLabel="Undo the sync"
        tone="primary" onConfirm={() => nav(`/jobs/${job.id}/delivery`)} onClose={onClose}>
        <p>This migration has already been synced to the target system. Without undoing the sync, you
          cannot delete the migration. Undo the sync first, then delete it.</p>
      </ConfirmDialog>
    );
  }
  return (
    <ConfirmDialog title="Delete this migration?" confirmLabel="Delete migration" busyLabel="Deleting…"
      onConfirm={async () => { await api.deleteMigration(job.id); onDeleted(); nav("/migrations"); }} onClose={onClose}>
      <dl className="confirm-dl">
        <div><dt>Organization</dt><dd>{orgLabel(job.tenant_id)}</dd></div>
        <div><dt>Source files</dt><dd>{files}</dd></div>
        <div><dt>Employees</dt><dd>{employees}</dd></div>
        <div><dt>Current stage</dt><dd>{humanStatus(job.status)}</dd></div>
      </dl>
      <p>This <strong>permanently deletes</strong> the migration and all of its data — uploaded source
        files, field mappings, prepared employees, comparison results, versions, metrics and the
        activity log. If this is the organization's only migration, the <strong>organization and its
        fields are removed too</strong>. <strong>This cannot be undone.</strong> It does not undo
        changes already synced to the target.</p>
    </ConfirmDialog>
  );
}

/** Overflow (⋯) menu for the migration header and list cards. A synced migration cannot be deleted
 *  until its target writes are undone, so it offers "Undo the sync" (routes to Sync) instead of a
 *  Delete action — the destructive button never appears while the target still holds this migration. */
export function MigrationOverflow({ job, onDeleted, size, stop }:
  { job: Job; onDeleted: () => void; size?: "sm"; stop?: boolean }) {
  const nav = useNavigate();
  const [open, setOpen] = useState(false);
  const synced = hasUnrevertedTargetWrites(job);
  return (
    <>
      <OverflowMenu size={size} stop={stop} label="Migration actions" items={
        synced
          ? [{ key: "undo", label: "Undo the sync", icon: "refresh", onClick: () => nav(`/jobs/${job.id}/delivery`) }]
          : [{ key: "delete", label: "Delete migration", icon: "trash", danger: true, onClick: () => setOpen(true) }]
      } />
      {open && <DeleteMigrationDialog job={job} onClose={() => setOpen(false)}
        onDeleted={() => { setOpen(false); onDeleted(); }} />}
    </>
  );
}
