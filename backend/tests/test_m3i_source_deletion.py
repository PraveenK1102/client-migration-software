"""M3I source-set mutation — DB-level mechanics for safe file removal + migration delete (order §C/§D).

These drive the transactional repository methods directly against a real ``Database`` + a real
``LocalBlobStore`` (no worker, no HTTP) so the source-set-rebuild and tombstone semantics are provable
deterministically. The API-contract + full-pipeline-rebuild proofs live in test_m3i_deletion_api.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.blobstore import LocalBlobStore
from app.db import Database, _DERIVED_TABLES_BY_JOB
from app.profiling import profile_table
from app.source_records import RawCell, RawType, SourceRecord, SourceRef, SourceTable

HEADERS = ["employee_id", "full_name", "work_email", "hire_date"]


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "app.db", busy_timeout_ms=2000)
    store = LocalBlobStore(tmp_path / "blobs")
    try:
        yield db, store
    finally:
        db.close()


def _seed_file(db, store, job_id, *, file_id, filename, employees, derived=True):
    """One source file: a blob + source_file + a table + rows + profiles, plus (optionally) a slice of
    downstream derived state so a rebuild's clearing is observable. Returns the table_id + blob_key."""
    data = ("\n".join([",".join(HEADERS)] + [",".join(e) for e in employees])).encode()
    info = store.put(data, suffix=".csv")
    db.add_source_file(job_id, file_id=file_id, original_filename=filename, stored_name=info.key,
                       content_type="text/csv", size_bytes=info.size_bytes, blob_key=info.key,
                       sha256=info.sha256, storage_status="stored", parse_status="parsed")
    table_id = f"t_{file_id}"
    table = SourceTable(table_id=table_id, file_id=file_id, original_filename=filename,
                        headers=list(HEADERS), n_rows=len(employees))
    db.add_source_table(job_id, table)
    recs = []
    for i, emp in enumerate(employees, start=1):
        ref = SourceRef(file_id=file_id, original_filename=filename, table_id=table_id,
                        row_number=i, col_index=0, header=HEADERS[0])
        cells = [RawCell(col_index=ci, header=h, value=v, raw_type=RawType.TEXT, ref=ref)
                 for ci, (h, v) in enumerate(zip(HEADERS, emp))]
        recs.append(SourceRecord(ref=ref, cells=cells))
    db.add_source_rows(job_id, recs)
    profiles = profile_table(table, recs)
    db.add_profiles(job_id, profiles)
    if derived:
        pid = profiles[0].profile_id
        db.upsert_decision(job_id, profile_id=pid, table_id=table_id, source_header=HEADERS[0],
                           target_field="employee_id", status="auto_accepted", actor="system",
                           method="rule", reason="seed")
    return table_id, info.key


def _seed_derived(db, job_id, employees):
    """A representative slice of job-scoped DERIVED state so we can prove a rebuild clears it all."""
    cands = []
    for e in employees:
        rec = {h: {"value": v, "status": "resolved"} for h, v in zip(HEADERS, e)}
        cands.append({"id": f"cand_{e[0]}", "business_key": e[0], "eligibility": "eligible",
                      "record": rec, "source_refs": [], "issue_ids": []})
    db.replace_candidates(job_id, cands)
    db.add_model_call(job_id, kind="mapping_proposal", table_id="t_A", adapter_kind="fake",
                      model_id="m", attempts=1, latency_ms=1.0, prompt_tokens=1, completion_tokens=1,
                      total_tokens=2, status="ok", error_category=None, n_columns=1, n_proposals=1)
    db.add_stage_timing(job_id, stage="mapping", duration_ms=5.0)
    db.add_employee_version(job_id, "cand_" + employees[0][0], snapshot={"employee_id": employees[0][0]},
                            origin="migration", created_by="system", business_key=employees[0][0])


# =========================================================================== §C: file removal
def test_bulk_remove_two_of_three_leaves_one_and_rebuilds(env):
    db, store = env
    job = db.create_job(schema_version="employee.v2", provider="fake", model_id="m", adapter_kind="fake")
    tA, kA = _seed_file(db, store, job, file_id="fileA", filename="a.csv",
                        employees=[["E1", "A One", "e1@x.com", "2020-01-01"]])
    tB, kB = _seed_file(db, store, job, file_id="fileB", filename="b.csv",
                        employees=[["E2", "B Two", "e2@x.com", "2020-02-02"]])
    tC, kC = _seed_file(db, store, job, file_id="fileC", filename="c.csv",
                        employees=[["E3", "C Three", "e3@x.com", "2020-03-03"]])
    _seed_derived(db, job, [["E1", "A One", "e1@x.com", "2020-01-01"],
                            ["E2", "B Two", "e2@x.com", "2020-02-02"],
                            ["E3", "C Three", "e3@x.com", "2020-03-03"]])

    res = db.remove_source_files(job, ["fileA", "fileB"], actor="human", note="wrong files")
    assert res["status"] == "ok" and res["remaining"] == 1

    # (1) only one file remains; (2) removed tables/rows/profiles gone; (3) remaining data intact.
    files = db.get_source_files(job)
    assert [f["id"] for f in files] == ["fileC"]
    assert db.get_rows_for_table(tA) == [] and db.get_rows_for_table(tB) == []
    assert len(db.get_rows_for_table(tC)) == 1
    prof_tables = {p["table_id"] for p in db.get_profiles(job)}
    assert prof_tables <= {tC}                       # removed-file profiles gone (all cleared on rebuild)

    # (5) candidates + ALL derived state cleared for the rebuild.
    assert db.get_candidates(job) == []
    assert db.get_model_calls(job) == []
    assert db.get_employee_versions(job, "cand_E1") == []
    for tbl in _DERIVED_TABLES_BY_JOB:
        n = db._fetchone(f"SELECT COUNT(*) AS n FROM {tbl} WHERE job_id=?", (job,))["n"]
        assert n == 0, f"{tbl} not cleared: {n}"

    # (6) a fresh generation-keyed MAP work item is enqueued pending.
    maps = [w for w in db.get_work_items(job) if w["kind"] == "MAP"]
    assert len(maps) == 1 and maps[0]["status"] == "pending"
    assert maps[0]["idempotency_key"].startswith(f"{job}:map:rebuild:")

    # (15) audit records who removed which files.
    ev = [a for a in db.get_audit(job) if a["event_type"] == "source_files_removed"]
    assert len(ev) == 1 and ev[0]["actor"] == "human"

    # (13) removed blobs deleted; the remaining blob stays.
    for k in res["blob_keys"]:
        store.delete(k)  # route does this; assert idempotent + that removal targeted A/B
    assert not store.exists(kA) and not store.exists(kB) and store.exists(kC)


def test_remove_cancels_pending_work_but_not_processing(env):
    db, store = env
    job = db.create_job(schema_version="employee.v2", provider="fake", model_id="m", adapter_kind="fake")
    _seed_file(db, store, job, file_id="fA", filename="a.csv", employees=[["E1", "A", "a@x.com", "2020-01-01"]])
    _seed_file(db, store, job, file_id="fB", filename="b.csv", employees=[["E2", "B", "b@x.com", "2020-01-01"]])
    db.enqueue_work(job_id=job, kind="PREPARE", idempotency_key=f"{job}:prepare")
    res = db.remove_source_files(job, ["fB"])
    assert res["status"] == "ok"
    prepare = [w for w in db.get_work_items(job) if w["kind"] == "PREPARE"]
    assert prepare and prepare[0]["status"] == "cancelled"


def test_remove_deferred_when_worker_owns_work(env):
    """A destructive mutation must not race a worker that currently OWNS a work item."""
    db, store = env
    job = db.create_job(schema_version="employee.v2", provider="fake", model_id="m", adapter_kind="fake")
    _seed_file(db, store, job, file_id="fA", filename="a.csv", employees=[["E1", "A", "a@x.com", "2020-01-01"]])
    _seed_file(db, store, job, file_id="fB", filename="b.csv", employees=[["E2", "B", "b@x.com", "2020-01-01"]])
    db.enqueue_work(job_id=job, kind="MAP", idempotency_key=f"{job}:map")
    claimed = db.claim_next_work("w1", lease_seconds=30)          # a worker now owns it
    assert claimed is not None and db.has_active_work(job)
    res = db.remove_source_files(job, ["fB"])
    assert res["status"] == "active_work"
    assert len(db.get_source_files(job)) == 2                     # nothing mutated


def test_remove_preserves_tenant_custom_field_definitions(env):
    db, store = env
    job = db.create_job(schema_version="employee.v2", provider="fake", model_id="m",
                        adapter_kind="fake", tenant_id="beta")
    _seed_file(db, store, job, file_id="fA", filename="a.csv", employees=[["E1", "A", "a@x.com", "2020-01-01"]])
    _seed_file(db, store, job, file_id="fB", filename="b.csv", employees=[["E2", "B", "b@x.com", "2020-01-01"]])
    db.add_custom_field_definition(tenant_id="beta", key="badge_colour", label="Badge Colour",
                                   type="string", origin="proposal", origin_job_id=job)
    res = db.remove_source_files(job, ["fB"])
    assert res["status"] == "ok" and res["preserved_custom_field_definitions"] == 1
    # the organization schema is untouched.
    assert [d["key"] for d in db.get_custom_field_definitions("beta")] == ["badge_colour"]


def test_has_target_delivery_activity_blocks_after_a_delivery_op(env):
    db, store = env
    job = db.create_job(schema_version="employee.v2", provider="fake", model_id="m", adapter_kind="fake")
    assert db.has_target_delivery_activity(job) is False
    db.create_delivery_operation(job_id=job, candidate_id="c1", employee_id="E1", op_type="CREATE",
                                 payload={}, expected_target_revision=None, target_record_id=None,
                                 before_snapshot=None, desired_version_id=None,
                                 idempotency_key=f"deliver:{job}:c1")
    db.transition_delivery_op(  # simulate a write attempt having started
        db.get_delivery_operations(job)[0]["id"], "PLANNED", "PROCESSING")
    assert db.has_target_delivery_activity(job) is True


# =========================================================================== §D: migration delete
def test_delete_removes_everything_and_reports_blobs(env):
    db, store = env
    job = db.create_job(schema_version="employee.v2", provider="fake", model_id="m", adapter_kind="fake")
    _, k = _seed_file(db, store, job, file_id="fA", filename="a.csv",
                      employees=[["E1", "A", "a@x.com", "2020-01-01"]])
    res = db.delete_job(job, actor="human")
    assert res["status"] == "ok" and k in res["blob_keys"]
    assert db.job_is_deleted(job) and db.list_jobs() == []
    assert db.get_job(job) is None                              # complete delete: the jobs row is gone
    assert db.get_audit(job) == []                              # the audit trail is purged too
    assert db.get_source_files(job) == []


def test_has_unreverted_target_writes_semantics(env):
    db, store = env
    job = db.create_job(schema_version="employee.v2", provider="fake", model_id="m", adapter_kind="fake")
    db.create_delivery_operation(job_id=job, candidate_id="c1", employee_id="E1", op_type="CREATE",
                                 payload={}, expected_target_revision=None, target_record_id=None,
                                 before_snapshot=None, desired_version_id=None,
                                 idempotency_key=f"deliver:{job}:c1")
    op_id = db.get_delivery_operations(job)[0]["id"]
    db.transition_delivery_op(op_id, "PLANNED", "SUCCEEDED")
    assert db.has_unreverted_target_writes(job) is True         # a live target write -> delete blocked
    db.update_delivery_operation(op_id, status="ROLLED_BACK")
    assert db.has_unreverted_target_writes(job) is False        # fully rolled back -> delete allowed


def test_delete_does_not_touch_other_job_or_org(env):
    db, store = env
    keep = db.create_job(schema_version="employee.v2", provider="fake", model_id="m",
                         adapter_kind="fake", tenant_id="beta")
    _, keep_key = _seed_file(db, store, keep, file_id="kf", filename="keep.csv",
                             employees=[["E9", "Keep", "keep@x.com", "2020-01-01"]])
    db.add_custom_field_definition(tenant_id="beta", key="badge_colour", label="Badge", type="string")
    victim = db.create_job(schema_version="employee.v2", provider="fake", model_id="m",
                           adapter_kind="fake", tenant_id="beta")
    _seed_file(db, store, victim, file_id="vf", filename="v.csv",
               employees=[["E1", "V", "v@x.com", "2020-01-01"]])
    res = db.delete_job(victim, actor="human")
    # the other migration under the SAME org is untouched, and org custom fields survive (the org is
    # NOT deleted while another migration still references it).
    assert res.get("org_deleted") is None
    assert [j["id"] for j in db.list_jobs()] == [keep]
    assert len(db.get_source_files(keep)) == 1 and store.exists(keep_key)
    assert [d["key"] for d in db.get_custom_field_definitions("beta")] == ["badge_colour"]


def test_delete_last_migration_deletes_the_org(env):
    db, store = env
    db.upsert_tenant("acme", "Acme")
    job = db.create_job(schema_version="employee.v2", provider="fake", model_id="m",
                        adapter_kind="fake", tenant_id="acme")
    _seed_file(db, store, job, file_id="af", filename="a.csv",
               employees=[["E1", "A", "a@x.com", "2020-01-01"]])
    db.add_custom_field_definition(tenant_id="acme", key="parking_zone", label="Parking", type="enum",
                                   options=["Zone A", "Zone B"])
    assert db.get_tenant("acme") is not None
    res = db.delete_job(job, actor="human")
    # last migration for the org -> the organization (tenant + its custom fields) is removed too.
    assert res.get("org_deleted") == "acme"
    assert db.get_tenant("acme") is None
    assert db.get_custom_field_definitions("acme") == []


def test_delete_never_removes_the_default_org(env):
    db, store = env
    db.upsert_tenant("default", "default")
    job = db.create_job(schema_version="employee.v2", provider="fake", model_id="m",
                        adapter_kind="fake", tenant_id="default")
    _seed_file(db, store, job, file_id="df", filename="d.csv",
               employees=[["E1", "A", "a@x.com", "2020-01-01"]])
    res = db.delete_job(job, actor="human")
    assert res.get("org_deleted") is None            # the internal default org is never auto-deleted
    assert db.get_tenant("default") is not None


def test_delete_deferred_when_worker_owns_work(env):
    db, store = env
    job = db.create_job(schema_version="employee.v2", provider="fake", model_id="m", adapter_kind="fake")
    _seed_file(db, store, job, file_id="fA", filename="a.csv", employees=[["E1", "A", "a@x.com", "2020-01-01"]])
    db.enqueue_work(job_id=job, kind="MAP", idempotency_key=f"{job}:map")
    db.claim_next_work("w1", lease_seconds=30)
    assert db.delete_job(job)["status"] == "active_work"
    assert not db.job_is_deleted(job)                           # still exists — delete was refused
