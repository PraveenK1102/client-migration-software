"""HTTP boundary to the confirmed target system.

Migration/reconciliation/delivery code depends ONLY on this gateway, never on the target
service's tables. Lookups are batched (by employee_id and work_email). Since M3B the gateway
also carries the WRITE operations used by delivery and compensation:

    create_employee(record, idempotency_key)                     POST   /employees
    update_employee(id, patch, expected_revision, idempotency_key) PATCH  /employees/{id}
    delete_employee(id, expected_revision, idempotency_key)      DELETE /employees/{id}
    get_employee(id)                                             GET    /employees/{id}

Every call has a bounded timeout. Transport failures (connection, timeout) raise
``TargetUnavailable`` (retryable). Any non-2xx response raises ``TargetResponseError`` carrying
the status, the target's machine-readable ``code`` and a SANITIZED body so the delivery layer can
classify it (409 revision conflict / already exists / email in use, 422 validation, 401/403 auth,
429 with Retry-After, 5xx). The gateway never classifies or retries by itself.

Async so it can be exercised in tests via httpx ASGITransport without a live port, and against
a real remote service (or the mock target run as a separate process) in production.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx


class TargetUnavailable(Exception):
    """The target system could not be reached (connection error / timeout). Retryable."""


class TargetResponseError(Exception):
    """The target answered with a non-2xx status. Carries what delivery needs to classify it."""

    def __init__(self, status_code: int, body: dict | None, *, retry_after: float | None = None,
                 request_id: str | None = None) -> None:
        self.status_code = status_code
        self.body = body or {}
        self.code = str(self.body.get("code") or "") if isinstance(self.body, dict) else ""
        self.retry_after = retry_after
        self.request_id = request_id
        detail = self.body.get("detail") if isinstance(self.body, dict) else None
        super().__init__(f"target responded {status_code}{(' ' + self.code) if self.code else ''}"
                         f"{(': ' + str(detail)[:200]) if detail else ''}")


@dataclass
class TargetWriteResult:
    status_code: int
    employee: dict | None
    revision: int | None
    replayed: bool = False
    request_id: str | None = None
    deleted: bool = False


@dataclass
class TargetLookup:
    by_id: dict[str, dict] = field(default_factory=dict)
    by_email: dict[str, dict] = field(default_factory=dict)   # keyed by lowercased work_email

    def for_id(self, employee_id: str | None) -> dict | None:
        return self.by_id.get(employee_id) if employee_id else None

    def for_email(self, email: str | None) -> dict | None:
        return self.by_email.get(email.lower()) if email else None


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    val = resp.headers.get("retry-after")
    if val is None:
        return None
    try:
        v = float(val)
        return v if v >= 0 else None
    except (TypeError, ValueError):
        return None   # HTTP-date form is not honoured (kept simple + deterministic)


def _safe_json(resp: httpx.Response) -> dict | None:
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return {"detail": (resp.text or "")[:300]}
    if isinstance(data, dict):
        # Sanitize: keep only small, known metadata keys; never echo whole payloads back into logs.
        keep = {k: data[k] for k in ("code", "detail", "current_revision", "owner_employee_id",
                                     "employee_id", "revision", "request_id", "errors", "replayed",
                                     "deleted") if k in data}
        if "employee" in data and isinstance(data["employee"], dict):
            keep["employee"] = data["employee"]
        return keep
    return {"detail": str(data)[:300]}


class TargetEmployeeGateway:
    def __init__(self, *, base_url: str | None = None, client: httpx.AsyncClient | None = None,
                 batch_size: int = 200, timeout: float = 10.0, organization_id: str | None = None) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url or "", timeout=timeout)
        self._owns = client is None
        self._timeout = float(timeout)
        self.batch_size = max(1, batch_size)
        # Organization isolation (M3G): every request this gateway makes carries the bound
        # organization in the ``X-Organization-ID`` header, so the target scopes identity, email
        # uniqueness, idempotency and reads/writes to it. ``None`` sends no header (the target then
        # falls back to its ``default`` organization) — legacy/back-compat only.
        self.organization_id = str(organization_id) if organization_id is not None else None

    def for_organization(self, organization_id: str) -> "TargetEmployeeGateway":
        """Return an immutable view of this gateway bound to one organization.

        The returned gateway shares (does not own) the same underlying HTTP client and injects
        ``X-Organization-ID`` on every request. Delivery/reconciliation create one such scoped view
        per job so that the organization is carried EXPLICITLY through every target operation, with
        no mutable process-global organization state."""
        scoped = TargetEmployeeGateway(client=self._client, batch_size=self.batch_size,
                                       timeout=self._timeout, organization_id=str(organization_id))
        return scoped

    async def aclose(self) -> None:
        if self._owns:
            await self._client.aclose()

    # --- transport wrapper ---------------------------------------------------------------
    async def _request(self, method: str, url: str, **kw) -> httpx.Response:
        if self.organization_id is not None:
            headers = dict(kw.pop("headers", None) or {})
            headers.setdefault("X-Organization-ID", self.organization_id)
            kw["headers"] = headers
        try:
            return await self._client.request(method, url, timeout=self._timeout, **kw)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError, httpx.RemoteProtocolError) as e:
            raise TargetUnavailable(f"{type(e).__name__}: {str(e)[:200]}") from e
        except httpx.HTTPError as e:  # pragma: no cover - other transport-level failures
            raise TargetUnavailable(f"{type(e).__name__}: {str(e)[:200]}") from e

    @staticmethod
    def _raise_for(resp: httpx.Response) -> None:
        if 200 <= resp.status_code < 300:
            return
        raise TargetResponseError(resp.status_code, _safe_json(resp), retry_after=_retry_after_seconds(resp),
                                  request_id=resp.headers.get("x-request-id"))

    @staticmethod
    def _write_result(resp: httpx.Response) -> TargetWriteResult:
        body = resp.json() if resp.content else {}
        emp = body.get("employee") if isinstance(body, dict) else None
        rev = body.get("revision") if isinstance(body, dict) else None
        if rev is None and isinstance(emp, dict):
            rev = emp.get("revision")
        return TargetWriteResult(status_code=resp.status_code, employee=emp, revision=rev,
                                 replayed=bool(body.get("replayed")) if isinstance(body, dict) else False,
                                 request_id=resp.headers.get("x-request-id"),
                                 deleted=bool(body.get("deleted")) if isinstance(body, dict) else False)

    # --- reads -----------------------------------------------------------------------------
    async def health(self) -> dict:
        r = await self._request("GET", "/health")
        self._raise_for(r)
        return r.json()

    async def lookup(self, employee_ids: list[str], work_emails: list[str]) -> TargetLookup:
        ids = [i for i in dict.fromkeys(employee_ids) if i]
        emails = [e for e in dict.fromkeys(work_emails) if e]
        result = TargetLookup()
        # Batch ids and emails independently to keep request bodies bounded.
        batches: list[tuple[list[str], list[str]]] = []
        for chunk in _chunks(ids, self.batch_size) or [[]]:
            batches.append((chunk, []))
        for chunk in _chunks(emails, self.batch_size):
            batches.append(([], chunk))
        for id_chunk, email_chunk in batches:
            if not id_chunk and not email_chunk:
                continue
            r = await self._request("POST", "/employees/lookup",
                                    json={"employee_ids": id_chunk, "work_emails": email_chunk})
            try:
                self._raise_for(r)
                data = r.json()
            except TargetResponseError as e:
                raise TargetUnavailable(f"lookup failed: {e}") from e
            result.by_id.update(data.get("by_employee_id", {}))
            result.by_email.update(data.get("by_work_email", {}))
        return result

    async def get_employee(self, employee_id: str) -> dict | None:
        """Current target record (with revision) or None if it does not exist."""
        r = await self._request("GET", f"/employees/{employee_id}")
        if r.status_code == 404:
            return None
        self._raise_for(r)
        body = r.json()
        return body.get("employee") if isinstance(body, dict) and "employee" in body else body

    # --- writes (M3B delivery + compensation) --------------------------------------------
    async def create_employee(self, record: dict, *, idempotency_key: str) -> TargetWriteResult:
        r = await self._request("POST", "/employees", json={"employee": record, "idempotency_key": idempotency_key})
        self._raise_for(r)
        return self._write_result(r)

    async def update_employee(self, employee_id: str, patch: dict, *, expected_revision: int,
                              idempotency_key: str) -> TargetWriteResult:
        r = await self._request("PATCH", f"/employees/{employee_id}",
                                json={"patch": patch, "expected_revision": expected_revision,
                                      "idempotency_key": idempotency_key})
        self._raise_for(r)
        return self._write_result(r)

    async def replace_employee(self, employee_id: str, full_record: dict, *,
                               expected_revision: int, idempotency_key: str) -> TargetWriteResult:
        """Full replace (PUT) — used by rollback to restore the exact before_snapshot."""
        r = await self._request("PUT", f"/employees/{employee_id}",
                                json={"employee": full_record, "expected_revision": expected_revision,
                                      "idempotency_key": idempotency_key})
        self._raise_for(r)
        return self._write_result(r)

    async def delete_employee(self, employee_id: str, *, expected_revision: int | None,
                              idempotency_key: str) -> TargetWriteResult:
        params = {"idempotency_key": idempotency_key}
        if expected_revision is not None:
            params["expected_revision"] = str(expected_revision)
        r = await self._request("DELETE", f"/employees/{employee_id}", params=params)
        self._raise_for(r)
        return self._write_result(r)
