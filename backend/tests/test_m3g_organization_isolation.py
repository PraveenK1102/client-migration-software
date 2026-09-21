"""M3G: organization isolation in the mock target + gateway + full app path.

The target is multi-organization. The identity boundary is (organization_id, employee_id) and the
work-email uniqueness boundary is (organization_id, normalized_work_email). Nothing — lookup, read,
create, update, replace/rollback, delete or idempotency — may cross an organization boundary.

Every test runs OFFLINE (no Groq key) against a fresh seeded mock target exercised in-process via
httpx ASGITransport (no live port). The scoped gateway carries the organization on the
``X-Organization-ID`` header. The eight scenarios required by the M3G order are covered:

  1. same employee id in two organizations                        -> test_same_employee_id_two_orgs
  2. update isolation                                             -> test_update_isolation
  3. same email across organizations allowed                      -> test_same_email_across_orgs_allowed
  4. duplicate email within same organization rejected           -> test_duplicate_email_within_org_rejected
  5. reconciliation cannot cross organizations by id or email    -> test_lookup_cannot_cross_orgs
                                                                     test_reconcile_cannot_cross_orgs
  6. rollback/delete cannot cross organizations                  -> test_delete_cannot_cross_orgs
                                                                     test_replace_rollback_cannot_cross_orgs
  7. idempotency cannot cross organizations                      -> test_idempotency_cannot_cross_orgs
  8. full app path: two orgs, same employee id, both deliver     -> test_full_app_path_two_orgs_same_id
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.db import Database
from app.delivery import execute_delivery_operation, plan_delivery
from app.reconcile_target import reconcile
from app.schema_loader import get_target_schema
from app.target_gateway import TargetEmployeeGateway, TargetResponseError
from mock_target.service import create_app as create_target_app

ABC, BCD = "ABC Pvt Ltd", "BCD Pvt Ltd"
REQUIRED = ["employee_id", "full_name", "work_email", "hire_date"]


@pytest.fixture
async def env(tmp_path):
    """Fresh mock target (its own SQLite) + a base gateway + a migration DB, all in-process."""
    db = Database(tmp_path / "app.db")
    target_app = create_target_app(db_path=str(tmp_path / "target.db"))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=target_app), base_url="http://target")
    gw = TargetEmployeeGateway(client=client)
    try:
        yield db, gw
    finally:
        await client.aclose()
        db.close()


def _emp(employee_id: str, *, full_name: str, work_email: str, hire_date: str = "2020-01-01") -> dict:
    return {"employee_id": employee_id, "full_name": full_name, "work_email": work_email,
            "hire_date": hire_date}


# ------------------------------------------------------------------------------------------
# 1. same employee id in two organizations
# ------------------------------------------------------------------------------------------
async def test_same_employee_id_two_orgs(env):
    db, gw = env
    abc = gw.for_organization(ABC)
    bcd = gw.for_organization(BCD)

    r1 = await abc.create_employee(_emp("E1001", full_name="Alice ABC", work_email="alice@abc.example"),
                                   idempotency_key="abc:E1001:create")
    r2 = await bcd.create_employee(_emp("E1001", full_name="Bob BCD", work_email="bob@bcd.example"),
                                   idempotency_key="bcd:E1001:create")
    assert r1.status_code == 201 and r2.status_code == 201

    a = await abc.get_employee("E1001")
    b = await bcd.get_employee("E1001")
    assert a["full_name"] == "Alice ABC" and b["full_name"] == "Bob BCD"
    # Two distinct records, each starting at revision 1 in its own organization.
    assert a["revision"] == 1 and b["revision"] == 1


# ------------------------------------------------------------------------------------------
# 2. update isolation
# ------------------------------------------------------------------------------------------
async def test_update_isolation(env):
    db, gw = env
    abc, bcd = gw.for_organization(ABC), gw.for_organization(BCD)
    await abc.create_employee(_emp("E1001", full_name="Alice ABC", work_email="alice@abc.example"),
                              idempotency_key="abc:create")
    await bcd.create_employee(_emp("E1001", full_name="Bob BCD", work_email="bob@bcd.example"),
                              idempotency_key="bcd:create")

    upd = await abc.update_employee("E1001", {"department": "Engineering"}, expected_revision=1,
                                    idempotency_key="abc:update")
    assert upd.status_code == 200 and upd.revision == 2

    a = await abc.get_employee("E1001")
    b = await bcd.get_employee("E1001")
    assert a["department"] == "Engineering" and a["revision"] == 2
    # BCD's same-id record is completely untouched.
    assert b.get("department") is None and b["revision"] == 1 and b["full_name"] == "Bob BCD"


# ------------------------------------------------------------------------------------------
# 3. same email across organizations allowed
# ------------------------------------------------------------------------------------------
async def test_same_email_across_orgs_allowed(env):
    db, gw = env
    abc, bcd = gw.for_organization(ABC), gw.for_organization(BCD)
    shared = "shared@demo.example"
    r1 = await abc.create_employee(_emp("E1", full_name="A", work_email=shared),
                                   idempotency_key="abc:e1")
    r2 = await bcd.create_employee(_emp("E1", full_name="B", work_email=shared),
                                   idempotency_key="bcd:e1")
    assert r1.status_code == 201 and r2.status_code == 201
    assert (await abc.get_employee("E1"))["work_email"] == shared
    assert (await bcd.get_employee("E1"))["work_email"] == shared


# ------------------------------------------------------------------------------------------
# 4. duplicate email within the same organization rejected
# ------------------------------------------------------------------------------------------
async def test_duplicate_email_within_org_rejected(env):
    db, gw = env
    abc = gw.for_organization(ABC)
    dup = "dup@abc.example"
    await abc.create_employee(_emp("E1", full_name="A", work_email=dup), idempotency_key="abc:e1")
    with pytest.raises(TargetResponseError) as ei:
        await abc.create_employee(_emp("E2", full_name="B", work_email=dup), idempotency_key="abc:e2")
    assert ei.value.status_code == 409 and ei.value.code == "email_in_use"
    assert ei.value.body.get("owner_employee_id") == "E1"


# ------------------------------------------------------------------------------------------
# 5. lookup / reconciliation cannot cross organizations (by id or email)
# ------------------------------------------------------------------------------------------
async def test_lookup_cannot_cross_orgs(env):
    db, gw = env
    abc, bcd = gw.for_organization(ABC), gw.for_organization(BCD)
    await abc.create_employee(_emp("E1001", full_name="Alice ABC", work_email="alice@abc.example"),
                              idempotency_key="abc:create")

    # BCD sees nothing for the same id OR the same email.
    miss = await bcd.lookup(["E1001"], ["alice@abc.example"])
    assert miss.for_id("E1001") is None
    assert miss.for_email("alice@abc.example") is None
    # ABC sees its own record by id AND by email.
    hit = await abc.lookup(["E1001"], ["alice@abc.example"])
    assert hit.for_id("E1001")["full_name"] == "Alice ABC"
    assert hit.for_email("alice@abc.example")["employee_id"] == "E1001"


async def test_reconcile_cannot_cross_orgs(env):
    """An identical incoming employee in a DIFFERENT org must reconcile as a brand-new CREATE, never
    match the other org's existing record by id or email."""
    db, gw = env
    schema = get_target_schema()
    # Existing record in ABC's target.
    await gw.for_organization(ABC).create_employee(
        _emp("E1001", full_name="Alice ABC", work_email="alice@shared.example"), idempotency_key="abc:create")

    # A BCD job with the SAME id and SAME email incoming.
    job = _make_job(db, BCD)
    db.replace_candidates(job, [_candidate("c1", "E1001", "Alice BCD", "alice@shared.example")])
    res = await reconcile(db, gw, job, schema)
    outcome = res["results"][0]["outcome"]
    assert outcome == "READY_CREATE", f"BCD must not match ABC's record; got {outcome}"


# ------------------------------------------------------------------------------------------
# 6. rollback (delete + replace) cannot cross organizations
# ------------------------------------------------------------------------------------------
async def test_delete_cannot_cross_orgs(env):
    db, gw = env
    abc, bcd = gw.for_organization(ABC), gw.for_organization(BCD)
    await abc.create_employee(_emp("E1001", full_name="Alice ABC", work_email="a@abc.example"),
                              idempotency_key="abc:create")
    await bcd.create_employee(_emp("E1001", full_name="Bob BCD", work_email="b@bcd.example"),
                              idempotency_key="bcd:create")

    # Compensating DELETE in ABC (as rollback would issue) removes ONLY ABC's record.
    d = await abc.delete_employee("E1001", expected_revision=1, idempotency_key="abc:rollback")
    assert d.status_code == 200 and d.deleted
    assert await abc.get_employee("E1001") is None
    # BCD's same-id record survives.
    assert (await bcd.get_employee("E1001"))["full_name"] == "Bob BCD"


async def test_replace_rollback_cannot_cross_orgs(env):
    db, gw = env
    abc, bcd = gw.for_organization(ABC), gw.for_organization(BCD)
    await abc.create_employee(_emp("E1001", full_name="Alice ABC", work_email="a@abc.example"),
                              idempotency_key="abc:create")
    await bcd.create_employee(_emp("E1001", full_name="Bob BCD", work_email="b@bcd.example"),
                              idempotency_key="bcd:create")

    # A rollback PUT/replace in ABC restores a snapshot into ABC only.
    restored = _emp("E1001", full_name="Alice Restored", work_email="a@abc.example")
    rep = await abc.replace_employee("E1001", restored, expected_revision=1, idempotency_key="abc:replace")
    assert rep.status_code == 200
    assert (await abc.get_employee("E1001"))["full_name"] == "Alice Restored"
    # BCD untouched.
    assert (await bcd.get_employee("E1001"))["full_name"] == "Bob BCD"
    assert (await bcd.get_employee("E1001"))["revision"] == 1


# ------------------------------------------------------------------------------------------
# 7. idempotency cannot cross organizations
# ------------------------------------------------------------------------------------------
async def test_idempotency_cannot_cross_orgs(env):
    db, gw = env
    abc, bcd = gw.for_organization(ABC), gw.for_organization(BCD)
    key = "shared-idempotency-key"

    # Same key, different orgs, different bodies -> NOT a replay; each org gets its own record.
    r1 = await abc.create_employee(_emp("E1001", full_name="Alice ABC", work_email="a@abc.example"),
                                   idempotency_key=key)
    r2 = await bcd.create_employee(_emp("E2002", full_name="Bob BCD", work_email="b@bcd.example"),
                                   idempotency_key=key)
    assert r1.status_code == 201 and not r1.replayed
    assert r2.status_code == 201 and not r2.replayed          # NOT replayed from ABC's key
    assert (await bcd.get_employee("E2002"))["full_name"] == "Bob BCD"
    assert await bcd.get_employee("E1001") is None            # ABC's record never leaked to BCD

    # Same key + same body WITHIN ABC replays (real idempotency still works per-org).
    r3 = await abc.create_employee(_emp("E1001", full_name="Alice ABC", work_email="a@abc.example"),
                                   idempotency_key=key)
    assert r3.status_code == 201 and r3.replayed


# ------------------------------------------------------------------------------------------
# 8. full app path: two organizations, same employee id, both deliver successfully
# ------------------------------------------------------------------------------------------
async def test_full_app_path_two_orgs_same_id(env):
    db, gw = env
    schema = get_target_schema()
    settings = SimpleNamespace(delivery_crash_after_send=False)

    async def run_migration(org: str, name: str, email: str) -> str:
        job = _make_job(db, org)
        db.replace_candidates(job, [_candidate(f"{job}:c1", "E1001", name, email)])
        res = await reconcile(db, gw, job, schema)
        db.replace_target_snapshots(job, res["snapshots"])
        db.replace_target_reconciliation(job, res["results"])
        assert res["results"][0]["outcome"] == "READY_CREATE"
        plan = plan_delivery(db, job, schema)
        assert plan["planned"] == 1
        for op in db.get_delivery_operations(job, status="PLANNED"):
            status = await execute_delivery_operation(db, gw, op, settings)
            assert status == "SUCCEEDED", f"{org} delivery failed: {status}"
        return job

    await run_migration(ABC, "Alice ABC", "alice@abc.example")
    await run_migration(BCD, "Bob BCD", "bob@bcd.example")

    # Both organizations now hold their own E1001, fully isolated.
    a = await gw.for_organization(ABC).get_employee("E1001")
    b = await gw.for_organization(BCD).get_employee("E1001")
    assert a["full_name"] == "Alice ABC" and a["work_email"] == "alice@abc.example"
    assert b["full_name"] == "Bob BCD" and b["work_email"] == "bob@bcd.example"

    # And the mock target reports both organizations, one employee each (no cross-leak).
    stats = (await gw._request("GET", "/_mock/stats")).json()  # noqa: SLF001 (test-only admin read)
    by_org = stats["employees_by_organization"]
    assert by_org.get(ABC) == 1 and by_org.get(BCD) == 1


# ------------------------------------------------------------------------------------------
# helpers for the reconcile / app-path tests
# ------------------------------------------------------------------------------------------
def _make_job(db: Database, tenant_id: str) -> str:
    return db.create_job(schema_version="employee.v2", provider="fake", model_id="m",
                         adapter_kind="fake", tenant_id=tenant_id)


def _candidate(cid: str, employee_id: str, full_name: str, work_email: str) -> dict:
    record = {
        "employee_id": {"value": employee_id, "status": "resolved"},
        "full_name": {"value": full_name, "status": "resolved"},
        "work_email": {"value": work_email, "status": "resolved"},
        "hire_date": {"value": "2020-01-01", "status": "resolved"},
    }
    return {"id": cid, "business_key": employee_id, "eligibility": "eligible",
            "record": record, "source_refs": [], "issue_ids": []}
