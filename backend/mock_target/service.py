"""Mock target HR system — a REAL writable HTTP boundary the migration delivers to (M3B).

This stands in for the already-confirmed target system. Migration code must NOT read or write
its tables directly; it talks only to this service via TargetEmployeeGateway. Records are
persisted in the service's OWN SQLite file (separate from the migration DB); every row carries a
deterministic ``revision`` starting at 1 that increments on each successful write (optimistic
concurrency). Storage is contract-agnostic: indexed identity columns (employee_id, work_email)
plus a JSON ``payload`` holding the whole record — scalar fields, ``collections`` and
``custom_attributes``. Synthetic seed data is deterministic so tests/demos are reproducible.

Organization isolation (M3G): the target is MULTI-ORGANIZATION. Every record, idempotency key and
write-log entry is scoped to an ``organization_id`` carried on each request via the
``X-Organization-ID`` header (defaulting to ``default`` when absent, for legacy/back-compat). The
identity boundary is ``(organization_id, employee_id)`` and the work-email uniqueness boundary is
``(organization_id, normalized_work_email)``. The SAME employee id may therefore exist in two
organizations (ABC/E1001 and BCD/E1001 are distinct records), and the SAME work email may exist in
two DIFFERENT organizations; a duplicate work email WITHIN one organization is still rejected.
Lookups, reads, writes, rollback and idempotency NEVER cross an organization boundary. There is no
mutable process-global organization state — the org is derived from the request header on every call.

Write API (M3B):
  POST   /employees                 {employee, idempotency_key}            -> 201 {employee, revision}
  PATCH  /employees/{id}            {patch, expected_revision, idempotency_key} -> 200 {employee, revision}
  DELETE /employees/{id}?expected_revision=&idempotency_key=                -> 200 {deleted, revision}
  GET    /employees/{id}                                                    -> 200 {employee} | 404
Errors carry a machine-readable ``code``:
  409 revision_conflict (current_revision + employee) | already_exists | email_in_use
  422 validation_error (errors[]) | idempotency_key_reuse
  404 not_found
Idempotency: the SAME idempotency key (WITHIN one organization) never performs the side effect
twice — the first accepted (2xx) result is persisted in ``idempotency_keys`` and REPLAYED
(``replayed: true``) on any later call with the same key, across service restarts. A key reused
with a different request body is rejected (422 idempotency_key_reuse). The same key value in a
DIFFERENT organization is unrelated (idempotency does not cross organizations). Rejections
(409/422) never reserve a key.

Validation follows the representative employee.v2 contract (schemas/employee.v2.yaml): known
fields only, required fields on CREATE, enum membership, collections as lists of items with known
item fields, custom_attributes as {definition_id|key, value} entries.

MOCK-ONLY controls (gated by MOCK_TARGET_ADMIN, default on for this mock):
  POST /_mock/failures  {mode: status|timeout, status, count, retry_after, scope}  queue injected failures
    scope = create|update|delete|lookup|read|any. 'read' (GET /employees/{id}, used by stale-target
    refetch) is opt-in ONLY: it is NOT matched by 'any', so write-failure tests are never disturbed.
    Failure injection is a global test control (not organization-scoped).
  GET/DELETE /_mock/failures ; POST /_mock/employees/{id}/mutate {fields} (external change, revision+1)
  POST /_mock/reset ; GET /_mock/stats (persisted call counters)

Run:  TARGET_DB_PATH=data/target.db uvicorn mock_target.service:app --port 8100
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

_LEGACY_FIELDS = ["employee_id", "full_name", "work_email", "department", "hire_date", "contract_start_date"]
_SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "schemas" / "employee.v2.yaml"

# Organization used when a request carries no explicit ``X-Organization-ID`` header. Legacy rows
# migrated from a pre-M3G single-organization database are also parked under this id (documented,
# never silently dropped).
DEFAULT_ORG = "default"

# Deterministic confirmed employees, each seeded into a specific organization as
# ``(organization_id, record)``. NOTE: synthetic, not real people. Chosen to exercise every
# reconciliation outcome against incoming candidates in the tests/fixtures. The manual-demo
# reconciliation set (100-401) belongs to the ``default`` organization; the M3A.2 structured demo
# runs under the ``beta`` organization, so its pre-existing employee (E103) is seeded there.
_SEED: list[tuple[str, dict]] = [
    # legacy six-field employees (manual-demo expectations depend on these exact values)
    (DEFAULT_ORG, {"employee_id": "100", "full_name": "Ada Existing", "work_email": "ada@corp.com",
     "department": "Engineering", "hire_date": "2019-01-01", "contract_start_date": None}),   # NO_CHANGE / conflict source
    (DEFAULT_ORG, {"employee_id": "200", "full_name": "Bob Existing", "work_email": "bob@corp.com",
     "department": None, "hire_date": "2020-02-02", "contract_start_date": None}),            # null dept -> READY_UPDATE
    (DEFAULT_ORG, {"employee_id": "300", "full_name": "Carol Existing", "work_email": "carol@corp.com",
     "department": "Sales", "hire_date": "2018-03-03", "contract_start_date": None}),         # email owned by another id
    (DEFAULT_ORG, {"employee_id": "400", "full_name": "Dave Existing", "work_email": "dave@corp.com",
     "department": "Finance", "hire_date": "2017-04-04", "contract_start_date": None}),       # id/email mismatch (id side)
    (DEFAULT_ORG, {"employee_id": "401", "full_name": "Erin Existing", "work_email": "erin@corp.com",
     "department": "People Operations", "hire_date": "2016-05-05", "contract_start_date": None}),  # (email side)
    # M3A.2 structured-demo employee (organization "beta"): exists in the target with ONE vehicle and
    # a blank department, so an incoming record can add a second vehicle + fill the department
    # (READY_UPDATE) and the version history shows a collection item being ADDED (v1 baseline -> v2).
    ("beta", {"employee_id": "E103", "full_name": "Rahul Verma", "work_email": "rahul.verma@beta.example",
     "department": None, "hire_date": "2019-06-17", "contract_start_date": None,
     "designation": "Analyst",
     "collections": {"vehicles": [{"type": "car", "registration_number": "KA05ZZ0001"}]},
     "custom_attributes": []}),
]


# ------------------------------------------------------------------------------------------
# Contract (the target's own view of the representative employee.v2 schema)
# ------------------------------------------------------------------------------------------
def _load_contract() -> dict:
    try:
        import yaml
        raw = yaml.safe_load(_SCHEMA_PATH.read_text(encoding="utf-8"))
    except Exception:  # pragma: no cover - defensive: fall back to the legacy six fields
        return {"fields": {f: {"type": "string"} for f in _LEGACY_FIELDS},
                "required": ["employee_id", "full_name", "work_email", "hire_date"], "collections": {}}
    fields = {f["key"]: f for f in raw.get("fields", [])}
    required = list((raw.get("business_rules") or {}).get("required_in_final_record", []))
    collections = {c["key"]: {i["key"]: i for i in c.get("fields", [])} for c in raw.get("collections", [])}
    return {"fields": fields, "required": required, "collections": collections}


CONTRACT = _load_contract()


def _validate(record: dict, *, partial: bool) -> list[str]:
    errors: list[str] = []
    if not isinstance(record, dict):
        return ["employee must be an object"]
    for k, v in record.items():
        if k in ("collections", "custom_attributes", "revision"):
            continue
        spec = CONTRACT["fields"].get(k)
        if spec is None:
            errors.append(f"unknown field '{k}'")
            continue
        if v is None:
            continue
        if not isinstance(v, (str, int, float)):
            errors.append(f"field '{k}' must be a scalar")
            continue
        if spec.get("type") == "enum" and str(v) not in [str(e) for e in spec.get("enum", [])]:
            errors.append(f"field '{k}': '{v}' is not one of {spec.get('enum')}")
    if not partial:
        for r in CONTRACT["required"]:
            if record.get(r) in (None, ""):
                errors.append(f"required field '{r}' is missing")
    colls = record.get("collections")
    if colls is not None:
        if not isinstance(colls, dict):
            errors.append("collections must be an object of lists")
        else:
            for ck, items in colls.items():
                spec = CONTRACT["collections"].get(ck)
                if spec is None:
                    errors.append(f"unknown collection '{ck}'")
                    continue
                if not isinstance(items, list):
                    errors.append(f"collection '{ck}' must be a list")
                    continue
                for it in items:
                    if not isinstance(it, dict):
                        errors.append(f"collection '{ck}' items must be objects")
                        continue
                    for ik, iv in it.items():
                        ispec = spec.get(ik)
                        if ispec is None:
                            errors.append(f"collection '{ck}': unknown item field '{ik}'")
                        elif iv is not None and ispec.get("type") == "enum" and str(iv) not in [str(e) for e in ispec.get("enum", [])]:
                            errors.append(f"collection '{ck}'.{ik}: '{iv}' is not one of {ispec.get('enum')}")
    cas = record.get("custom_attributes")
    if cas is not None:
        if not isinstance(cas, list):
            errors.append("custom_attributes must be a list")
        else:
            for ca in cas:
                if not isinstance(ca, dict) or not (ca.get("key") or ca.get("definition_id")):
                    errors.append("custom_attributes entries need a key or definition_id")
    return errors


# ------------------------------------------------------------------------------------------
# Storage
# ------------------------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_org(value: str | None) -> str:
    """Normalize an organization id. Missing/blank -> ``default`` (back-compat, never global)."""
    if value is None:
        return DEFAULT_ORG
    v = str(value).strip()
    return v or DEFAULT_ORG


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    # Identity boundary is (organization_id, employee_id): the SAME employee id can exist in two orgs.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS target_employees ("
        "organization_id TEXT NOT NULL DEFAULT 'default', employee_id TEXT NOT NULL, work_email TEXT, "
        "payload TEXT, revision INTEGER NOT NULL DEFAULT 1, "
        "PRIMARY KEY (organization_id, employee_id))")
    # Idempotency is scoped per organization: the same key value in another org is unrelated.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS idempotency_keys ("
        "organization_id TEXT NOT NULL DEFAULT 'default', key TEXT NOT NULL, operation TEXT NOT NULL, "
        "employee_id TEXT, request_hash TEXT NOT NULL, status_code INTEGER NOT NULL, response TEXT NOT NULL, "
        "created_at TEXT NOT NULL, PRIMARY KEY (organization_id, key))")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mock_failures ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL, status INTEGER, retry_after REAL, "
        "scope TEXT NOT NULL, remaining INTEGER NOT NULL, created_at TEXT NOT NULL)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mock_counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS write_log ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, organization_id TEXT, operation TEXT NOT NULL, "
        "employee_id TEXT, status_code INTEGER NOT NULL, idempotency_key TEXT, replayed INTEGER NOT NULL DEFAULT 0, "
        "request_id TEXT)")
    _migrate_legacy_columns(conn)
    _migrate_org_isolation(conn)
    conn.commit()
    return conn


def _migrate_legacy_columns(conn: sqlite3.Connection) -> None:
    """Older target DBs stored six scalar columns; fold them into the JSON payload once."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(target_employees)").fetchall()}
    if "payload" not in cols:
        conn.execute("ALTER TABLE target_employees ADD COLUMN payload TEXT")
        cols.add("payload")
    legacy = [c for c in _LEGACY_FIELDS if c in cols and c not in ("employee_id", "work_email")]
    if legacy:
        for row in conn.execute("SELECT * FROM target_employees WHERE payload IS NULL").fetchall():
            rec = {f: row[f] for f in _LEGACY_FIELDS if f in cols}
            conn.execute("UPDATE target_employees SET payload=? WHERE employee_id=?",
                         (json.dumps(rec), row["employee_id"]))


def _migrate_org_isolation(conn: sqlite3.Connection) -> None:
    """M3G: migrate a pre-organization single-tenant target DB in place, without dropping any row.

    A legacy ``target_employees`` (employee_id PRIMARY KEY, no organization_id) and
    ``idempotency_keys`` (key PRIMARY KEY, no organization_id) are recreated with the organization
    composite key and every existing row is moved to the documented ``default`` organization.
    ``write_log`` gains an ``organization_id`` column (also backfilled to ``default``). Brand-new
    databases already have the correct schema, so this is a no-op for them.
    """
    emp_cols = {r["name"] for r in conn.execute("PRAGMA table_info(target_employees)").fetchall()}
    if emp_cols and "organization_id" not in emp_cols:
        conn.execute("ALTER TABLE target_employees RENAME TO _legacy_target_employees")
        conn.execute(
            "CREATE TABLE target_employees ("
            "organization_id TEXT NOT NULL DEFAULT 'default', employee_id TEXT NOT NULL, work_email TEXT, "
            "payload TEXT, revision INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (organization_id, employee_id))")
        conn.execute(
            "INSERT INTO target_employees (organization_id, employee_id, work_email, payload, revision) "
            "SELECT ?, employee_id, work_email, payload, revision FROM _legacy_target_employees", (DEFAULT_ORG,))
        conn.execute("DROP TABLE _legacy_target_employees")

    idem_cols = {r["name"] for r in conn.execute("PRAGMA table_info(idempotency_keys)").fetchall()}
    if idem_cols and "organization_id" not in idem_cols:
        conn.execute("ALTER TABLE idempotency_keys RENAME TO _legacy_idempotency_keys")
        conn.execute(
            "CREATE TABLE idempotency_keys ("
            "organization_id TEXT NOT NULL DEFAULT 'default', key TEXT NOT NULL, operation TEXT NOT NULL, "
            "employee_id TEXT, request_hash TEXT NOT NULL, status_code INTEGER NOT NULL, response TEXT NOT NULL, "
            "created_at TEXT NOT NULL, PRIMARY KEY (organization_id, key))")
        conn.execute(
            "INSERT INTO idempotency_keys (organization_id, key, operation, employee_id, request_hash, status_code, "
            "response, created_at) SELECT ?, key, operation, employee_id, request_hash, status_code, response, "
            "created_at FROM _legacy_idempotency_keys", (DEFAULT_ORG,))
        conn.execute("DROP TABLE _legacy_idempotency_keys")

    wl_cols = {r["name"] for r in conn.execute("PRAGMA table_info(write_log)").fetchall()}
    if wl_cols and "organization_id" not in wl_cols:
        conn.execute("ALTER TABLE write_log ADD COLUMN organization_id TEXT")
        conn.execute("UPDATE write_log SET organization_id=? WHERE organization_id IS NULL", (DEFAULT_ORG,))


def _seed(conn: sqlite3.Connection, *, reset: bool = False) -> None:
    if reset:
        for t in ("target_employees", "idempotency_keys", "mock_failures", "mock_counters", "write_log"):
            conn.execute(f"DELETE FROM {t}")
    # Seed each record into its own organization. INSERT OR IGNORE keyed on (organization_id,
    # employee_id) is idempotent and additive: a restart never duplicates a seed row and never
    # overwrites a row a migration has since changed; seed rows introduced later are added if missing.
    for org, rec in _SEED:
        conn.execute("INSERT OR IGNORE INTO target_employees (organization_id, employee_id, work_email, payload, "
                     "revision) VALUES (?,?,?,?,1)",
                     (org, rec["employee_id"], rec.get("work_email"), json.dumps(rec)))
    conn.commit()


def _row_to_record(row: sqlite3.Row) -> dict:
    rec = json.loads(row["payload"]) if row["payload"] else {}
    rec["employee_id"] = row["employee_id"]
    if rec.get("work_email") is None and row["work_email"]:
        rec["work_email"] = row["work_email"]
    rec.setdefault("collections", {})
    rec.setdefault("custom_attributes", [])
    rec["revision"] = row["revision"]
    return rec


def _request_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def _bump(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("INSERT INTO mock_counters (name, value) VALUES (?,1) "
                 "ON CONFLICT(name) DO UPDATE SET value=value+1", (name,))


def _log_write(conn, organization_id, operation, employee_id, status_code, idem, replayed, request_id) -> None:
    conn.execute("INSERT INTO write_log (ts, organization_id, operation, employee_id, status_code, idempotency_key, "
                 "replayed, request_id) VALUES (?,?,?,?,?,?,?,?)",
                 (_now(), organization_id, operation, employee_id, status_code, idem, 1 if replayed else 0, request_id))


# ------------------------------------------------------------------------------------------
# Request models
# ------------------------------------------------------------------------------------------
class LookupRequest(BaseModel):
    employee_ids: list[str] = []
    work_emails: list[str] = []


class CreateRequest(BaseModel):
    employee: dict
    idempotency_key: str


class PatchRequest(BaseModel):
    patch: dict
    expected_revision: int
    idempotency_key: str


class ReplaceRequest(BaseModel):
    employee: dict
    expected_revision: int
    idempotency_key: str


class FailureRequest(BaseModel):
    mode: str = "status"            # status | timeout
    status: int | None = 500
    count: int = 1
    retry_after: float | None = None
    scope: str = "any"              # create | update | delete | lookup | read | any ('read' opts out of 'any')
    timeout_seconds: float = 3.0


class MutateRequest(BaseModel):
    fields: dict


def create_app(db_path: str | None = None, *, admin_enabled: bool | None = None) -> FastAPI:
    path = db_path or os.environ.get("TARGET_DB_PATH", "data/target.db")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    admin = admin_enabled if admin_enabled is not None else \
        os.environ.get("MOCK_TARGET_ADMIN", "1").strip().lower() not in ("0", "false", "no", "off")

    # Connect + seed eagerly so the app also works when driven via httpx ASGITransport
    # (which does not run the lifespan) — this is how the single-node app mounts it in-process.
    conn = _connect(path)
    _seed(conn)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            conn.close()

    app = FastAPI(title="Mock Target HR System", version="0.4.0", lifespan=lifespan)
    app.state.conn = conn

    def _err(status: int, code: str, **extra) -> JSONResponse:
        return JSONResponse(status_code=status, content={"code": code, **extra})

    def _org(request: Request) -> str:
        """Resolve the organization for this request from the explicit ``X-Organization-ID`` header.

        There is no process-global organization state: the scope is derived per request. A missing
        header maps to the documented ``default`` organization for back-compatibility."""
        return _norm_org(request.headers.get("X-Organization-ID"))

    # --- mock failure injection ---------------------------------------------------------
    async def _maybe_inject(scope: str, *, include_any: bool = True) -> Response | None:
        # ``read`` (GET /employees/{id}) is opted OUT of the broad ``any`` scope so pre-existing
        # write-failure tests that queue ``scope='any'`` are never disturbed by a stale-refresh GET;
        # a read failure must be requested explicitly with ``scope='read'``. Failure injection is a
        # global mock control (not organization-scoped) — it targets the operation, not a tenant.
        if include_any:
            row = conn.execute(
                "SELECT * FROM mock_failures WHERE remaining>0 AND (scope='any' OR scope=?) ORDER BY id LIMIT 1",
                (scope,)).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM mock_failures WHERE remaining>0 AND scope=? ORDER BY id LIMIT 1",
                (scope,)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE mock_failures SET remaining=remaining-1 WHERE id=?", (row["id"],))
        conn.commit()
        if row["mode"] == "timeout":
            await asyncio.sleep(max(0.0, float(row["retry_after"] or 3.0)))   # client times out first
            return _err(504, "injected_timeout_elapsed")
        headers = {}
        if row["retry_after"] is not None and row["status"] == 429:
            headers["Retry-After"] = str(row["retry_after"])
        return JSONResponse(status_code=int(row["status"] or 500),
                            content={"code": "injected_failure", "detail": f"mock-injected {row['status']}"},
                            headers=headers)

    # --- reads -----------------------------------------------------------------------------
    @app.get("/health")
    def health():
        n = conn.execute("SELECT COUNT(*) FROM target_employees").fetchone()[0]
        orgs = conn.execute("SELECT COUNT(DISTINCT organization_id) FROM target_employees").fetchone()[0]
        return {"status": "ok", "employees": n, "organizations": orgs, "admin": admin}

    @app.post("/employees/lookup")
    async def lookup(req: LookupRequest, request: Request):
        org = _org(request)
        inj = await _maybe_inject("lookup")
        if inj is not None:
            return inj
        _bump(conn, "lookup_calls"); conn.commit()
        by_id: dict[str, dict] = {}
        by_email: dict[str, dict] = {}
        ids = [i for i in dict.fromkeys(req.employee_ids) if i]
        emails = [e for e in dict.fromkeys(req.work_emails) if e]
        if ids:
            q = ",".join("?" * len(ids))
            for row in conn.execute(
                    f"SELECT * FROM target_employees WHERE organization_id=? AND employee_id IN ({q})",
                    [org, *ids]).fetchall():
                by_id[row["employee_id"]] = _row_to_record(row)
        if emails:
            lowered = [e.lower() for e in emails]
            q = ",".join("?" * len(lowered))
            for row in conn.execute(
                    f"SELECT * FROM target_employees WHERE organization_id=? AND lower(work_email) IN ({q})",
                    [org, *lowered]).fetchall():
                by_email[row["work_email"].lower()] = _row_to_record(row)
        return {"by_employee_id": by_id, "by_work_email": by_email}

    @app.get("/employees/{employee_id}")
    async def get_employee(employee_id: str, request: Request):
        # Stale-target recovery re-fetches via this endpoint; a MOCK-ONLY injected read failure
        # (scope='read') lets tests exercise transient GET failures without ever turning a refetch
        # into a write. Only explicit read-scoped failures apply (not the broad 'any' scope).
        org = _org(request)
        inj = await _maybe_inject("read", include_any=False)
        if inj is not None:
            return inj
        row = conn.execute("SELECT * FROM target_employees WHERE organization_id=? AND employee_id=?",
                           (org, employee_id)).fetchone()
        if row is None:
            return _err(404, "not_found", employee_id=employee_id)
        return {"employee": _row_to_record(row)}

    # --- idempotency helpers -------------------------------------------------------------
    def _replay(org: str, key: str, req_hash: str) -> Response | None:
        row = conn.execute("SELECT * FROM idempotency_keys WHERE organization_id=? AND key=?",
                           (org, key)).fetchone()
        if row is None:
            return None
        if row["request_hash"] != req_hash:
            return _err(422, "idempotency_key_reuse", detail="idempotency key already used with a different request")
        body = json.loads(row["response"])
        body["replayed"] = True
        _bump(conn, "replayed_calls")
        _log_write(conn, org, row["operation"], row["employee_id"], row["status_code"], key, True, None)
        conn.commit()
        return JSONResponse(status_code=row["status_code"], content=body, headers={"x-request-id": uuid.uuid4().hex[:12]})

    def _remember(org: str, key: str, operation: str, employee_id: str | None, req_hash: str,
                  status: int, body: dict) -> None:
        conn.execute("INSERT OR REPLACE INTO idempotency_keys (organization_id, key, operation, employee_id, "
                     "request_hash, status_code, response, created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (org, key, operation, employee_id, req_hash, status, json.dumps(body), _now()))

    def _email_owner(org: str, email: str | None, exclude_id: str | None) -> str | None:
        if not email:
            return None
        row = conn.execute(
            "SELECT employee_id FROM target_employees "
            "WHERE organization_id=? AND lower(work_email)=? AND employee_id<>?",
            (org, email.lower(), exclude_id or "")).fetchone()
        return row["employee_id"] if row else None

    # --- writes ----------------------------------------------------------------------------
    @app.post("/employees", status_code=201)
    async def create_employee(req: CreateRequest, request: Request):
        org = _org(request)
        inj = await _maybe_inject("create")
        if inj is not None:
            return inj
        rid = uuid.uuid4().hex[:12]
        req_hash = _request_hash({"op": "create", "org": org, "employee": req.employee})
        rep = _replay(org, req.idempotency_key, req_hash)
        if rep is not None:
            return rep
        emp = dict(req.employee)
        errors = _validate(emp, partial=False)
        if errors:
            _bump(conn, "rejected_calls"); conn.commit()
            return _err(422, "validation_error", errors=errors)
        eid = str(emp["employee_id"]).strip()
        emp["employee_id"] = eid
        existing = conn.execute("SELECT * FROM target_employees WHERE organization_id=? AND employee_id=?",
                               (org, eid)).fetchone()
        if existing is not None:
            _bump(conn, "rejected_calls"); conn.commit()
            return _err(409, "already_exists", employee=_row_to_record(existing), current_revision=existing["revision"])
        owner = _email_owner(org, emp.get("work_email"), eid)
        if owner:
            _bump(conn, "rejected_calls"); conn.commit()
            return _err(409, "email_in_use", owner_employee_id=owner)
        emp.setdefault("collections", {})
        emp.setdefault("custom_attributes", [])
        emp.pop("revision", None)
        conn.execute("INSERT INTO target_employees (organization_id, employee_id, work_email, payload, revision) "
                     "VALUES (?,?,?,?,1)", (org, eid, emp.get("work_email"), json.dumps(emp)))
        row = conn.execute("SELECT * FROM target_employees WHERE organization_id=? AND employee_id=?",
                           (org, eid)).fetchone()
        body = {"employee": _row_to_record(row), "revision": 1, "replayed": False, "request_id": rid}
        _remember(org, req.idempotency_key, "create", eid, req_hash, 201, body)
        _bump(conn, "create_calls"); _bump(conn, "write_calls")
        _log_write(conn, org, "create", eid, 201, req.idempotency_key, False, rid)
        conn.commit()
        return JSONResponse(status_code=201, content=body, headers={"x-request-id": rid})

    @app.patch("/employees/{employee_id}")
    async def patch_employee(employee_id: str, req: PatchRequest, request: Request):
        org = _org(request)
        inj = await _maybe_inject("update")
        if inj is not None:
            return inj
        rid = uuid.uuid4().hex[:12]
        req_hash = _request_hash({"op": "update", "org": org, "id": employee_id, "patch": req.patch,
                                  "expected_revision": req.expected_revision})
        rep = _replay(org, req.idempotency_key, req_hash)
        if rep is not None:
            return rep
        row = conn.execute("SELECT * FROM target_employees WHERE organization_id=? AND employee_id=?",
                           (org, employee_id)).fetchone()
        if row is None:
            _bump(conn, "rejected_calls"); conn.commit()
            return _err(404, "not_found", employee_id=employee_id)
        if row["revision"] != req.expected_revision:
            _bump(conn, "revision_conflicts"); conn.commit()
            return _err(409, "revision_conflict", current_revision=row["revision"], employee=_row_to_record(row))
        patch = dict(req.patch)
        if "employee_id" in patch and str(patch["employee_id"]) != employee_id:
            return _err(422, "validation_error", errors=["employee_id cannot be changed"])
        errors = _validate(patch, partial=True)
        if errors:
            _bump(conn, "rejected_calls"); conn.commit()
            return _err(422, "validation_error", errors=errors)
        new_email = patch.get("work_email")
        if new_email:
            owner = _email_owner(org, new_email, employee_id)
            if owner:
                _bump(conn, "rejected_calls"); conn.commit()
                return _err(409, "email_in_use", owner_employee_id=owner)
        current = _row_to_record(row)
        current.pop("revision", None)
        for k, v in patch.items():
            if k == "collections":
                colls = dict(current.get("collections") or {})
                for ck, items in (v or {}).items():
                    colls[ck] = items                  # the patch carries the full effective list per collection
                current["collections"] = colls
            elif k == "custom_attributes":
                by_key = {ca.get("key") or ca.get("definition_id"): ca for ca in (current.get("custom_attributes") or [])}
                for ca in v or []:
                    by_key[ca.get("key") or ca.get("definition_id")] = ca
                current["custom_attributes"] = [c for c in by_key.values() if c.get("value") not in (None, "", [])]
            else:
                current[k] = v
        new_rev = row["revision"] + 1
        conn.execute("UPDATE target_employees SET work_email=?, payload=?, revision=? "
                     "WHERE organization_id=? AND employee_id=? AND revision=?",
                     (current.get("work_email"), json.dumps(current), new_rev, org, employee_id, row["revision"]))
        row2 = conn.execute("SELECT * FROM target_employees WHERE organization_id=? AND employee_id=?",
                           (org, employee_id)).fetchone()
        body = {"employee": _row_to_record(row2), "revision": new_rev, "replayed": False, "request_id": rid}
        _remember(org, req.idempotency_key, "update", employee_id, req_hash, 200, body)
        _bump(conn, "update_calls"); _bump(conn, "write_calls")
        _log_write(conn, org, "update", employee_id, 200, req.idempotency_key, False, rid)
        conn.commit()
        return JSONResponse(status_code=200, content=body, headers={"x-request-id": rid})

    @app.put("/employees/{employee_id}")
    async def replace_employee(employee_id: str, req: ReplaceRequest, request: Request):
        """Full replace (not merge) — used by rollback to restore exact before_snapshot."""
        org = _org(request)
        inj = await _maybe_inject("update")
        if inj is not None:
            return inj
        rid = uuid.uuid4().hex[:12]
        req_hash = _request_hash({"op": "replace", "org": org, "id": employee_id, "employee": req.employee,
                                  "expected_revision": req.expected_revision})
        rep = _replay(org, req.idempotency_key, req_hash)
        if rep is not None:
            return rep
        row = conn.execute("SELECT * FROM target_employees WHERE organization_id=? AND employee_id=?",
                           (org, employee_id)).fetchone()
        if row is None:
            _bump(conn, "rejected_calls"); conn.commit()
            return _err(404, "not_found", employee_id=employee_id)
        if row["revision"] != req.expected_revision:
            _bump(conn, "revision_conflicts"); conn.commit()
            return _err(409, "revision_conflict", current_revision=row["revision"], employee=_row_to_record(row))
        replacement = dict(req.employee)
        replacement["employee_id"] = employee_id
        replacement.pop("revision", None)
        replacement.setdefault("collections", {})
        replacement.setdefault("custom_attributes", [])
        new_rev = row["revision"] + 1
        conn.execute("UPDATE target_employees SET work_email=?, payload=?, revision=? "
                     "WHERE organization_id=? AND employee_id=? AND revision=?",
                     (replacement.get("work_email"), json.dumps(replacement), new_rev, org, employee_id, row["revision"]))
        row2 = conn.execute("SELECT * FROM target_employees WHERE organization_id=? AND employee_id=?",
                           (org, employee_id)).fetchone()
        body = {"employee": _row_to_record(row2), "revision": new_rev, "replayed": False, "request_id": rid}
        _remember(org, req.idempotency_key, "replace", employee_id, req_hash, 200, body)
        _bump(conn, "update_calls"); _bump(conn, "write_calls")
        _log_write(conn, org, "replace", employee_id, 200, req.idempotency_key, False, rid)
        conn.commit()
        return JSONResponse(status_code=200, content=body, headers={"x-request-id": rid})

    @app.delete("/employees/{employee_id}")
    async def delete_employee(employee_id: str, idempotency_key: str, request: Request,
                              expected_revision: int | None = None):
        org = _org(request)
        inj = await _maybe_inject("delete")
        if inj is not None:
            return inj
        rid = uuid.uuid4().hex[:12]
        req_hash = _request_hash({"op": "delete", "org": org, "id": employee_id, "expected_revision": expected_revision})
        rep = _replay(org, idempotency_key, req_hash)
        if rep is not None:
            return rep
        row = conn.execute("SELECT * FROM target_employees WHERE organization_id=? AND employee_id=?",
                           (org, employee_id)).fetchone()
        if row is None:
            _bump(conn, "rejected_calls"); conn.commit()
            return _err(404, "not_found", employee_id=employee_id)
        if expected_revision is not None and row["revision"] != expected_revision:
            _bump(conn, "revision_conflicts"); conn.commit()
            return _err(409, "revision_conflict", current_revision=row["revision"], employee=_row_to_record(row))
        conn.execute("DELETE FROM target_employees WHERE organization_id=? AND employee_id=?", (org, employee_id))
        body = {"deleted": True, "employee_id": employee_id, "revision": row["revision"], "replayed": False,
                "request_id": rid}
        _remember(org, idempotency_key, "delete", employee_id, req_hash, 200, body)
        _bump(conn, "delete_calls"); _bump(conn, "write_calls")
        _log_write(conn, org, "delete", employee_id, 200, idempotency_key, False, rid)
        conn.commit()
        return JSONResponse(status_code=200, content=body, headers={"x-request-id": rid})

    # --- MOCK-ONLY controls (tests/demo) ---------------------------------------------------
    if admin:
        @app.post("/_mock/failures")
        def add_failure(req: FailureRequest):
            if req.mode not in ("status", "timeout"):
                return _err(422, "validation_error", errors=["mode must be status or timeout"])
            if req.scope not in ("create", "update", "delete", "lookup", "read", "any"):
                return _err(422, "validation_error", errors=["bad scope"])
            conn.execute("INSERT INTO mock_failures (mode, status, retry_after, scope, remaining, created_at) VALUES (?,?,?,?,?,?)",
                         (req.mode, req.status if req.mode == "status" else 504,
                          req.retry_after if req.mode == "status" else req.timeout_seconds, req.scope,
                          max(0, req.count), _now()))
            conn.commit()
            return {"queued": [dict(r) for r in conn.execute("SELECT * FROM mock_failures WHERE remaining>0").fetchall()]}

        @app.get("/_mock/failures")
        def list_failures():
            return {"queued": [dict(r) for r in conn.execute("SELECT * FROM mock_failures WHERE remaining>0").fetchall()]}

        @app.delete("/_mock/failures")
        def clear_failures():
            conn.execute("DELETE FROM mock_failures"); conn.commit()
            return {"queued": []}

        @app.post("/_mock/employees/{employee_id}/mutate")
        def mutate_employee(employee_id: str, req: MutateRequest, request: Request):
            """Simulate an EXTERNAL change in the target between reconciliation and delivery."""
            org = _org(request)
            row = conn.execute("SELECT * FROM target_employees WHERE organization_id=? AND employee_id=?",
                               (org, employee_id)).fetchone()
            if row is None:
                return _err(404, "not_found", employee_id=employee_id)
            current = _row_to_record(row)
            current.pop("revision", None)
            for k, v in req.fields.items():
                if k == "collections":
                    colls = dict(current.get("collections") or {})
                    colls.update(v or {})
                    current["collections"] = colls
                else:
                    current[k] = v
            conn.execute("UPDATE target_employees SET work_email=?, payload=?, revision=revision+1 "
                         "WHERE organization_id=? AND employee_id=?",
                         (current.get("work_email"), json.dumps(current), org, employee_id))
            _bump(conn, "external_mutations"); conn.commit()
            row2 = conn.execute("SELECT * FROM target_employees WHERE organization_id=? AND employee_id=?",
                               (org, employee_id)).fetchone()
            return {"employee": _row_to_record(row2), "revision": row2["revision"]}

        @app.post("/_mock/reset")
        def reset():
            _seed(conn, reset=True)
            return {"reset": True, "employees": conn.execute("SELECT COUNT(*) FROM target_employees").fetchone()[0]}

        @app.get("/_mock/stats")
        def stats():
            counters = {r["name"]: r["value"] for r in conn.execute("SELECT * FROM mock_counters").fetchall()}
            log_rows = [dict(r) for r in conn.execute("SELECT * FROM write_log ORDER BY id DESC LIMIT 200").fetchall()]
            by_org = {r["organization_id"]: r["n"] for r in conn.execute(
                "SELECT organization_id, COUNT(*) AS n FROM target_employees GROUP BY organization_id").fetchall()}
            return {"counters": counters, "employees": conn.execute("SELECT COUNT(*) FROM target_employees").fetchone()[0],
                    "employees_by_organization": by_org,
                    "idempotency_keys": conn.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0],
                    "write_log": log_rows}

    return app


app = create_app()
