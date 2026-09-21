"""M3A.2 (order G3.10-G3.16): tenant-scoped custom fields + the proposal / approval / ignore workflow.

Driven over the HTTP API (TestClient) in the NO-PROVIDER configuration (LLM_PROVIDER=groq with no
key): rule-resolvable columns still map deterministically, every unresolved column becomes a
custom-field PROPOSAL (never an auto-created target field) and the job is ``blocked_provider``
until a consultant decides each proposal. Synthetic fixture only:
sample-data/structured-demo/01_employees.csv (tenant "beta" seeds ONE custom field, badge_colour).

Everything here is offline: no Groq client is ever built because the key resolves to None.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.custom_fields import proposal_id as deterministic_proposal_id
from app.main import create_app

DEMO = Path(__file__).resolve().parent.parent.parent / "sample-data" / "structured-demo"
CSV = "01_employees.csv"
BADGE_PATH = "custom_attributes.badge_colour"
TSHIRT_PATH = "custom_attributes.tshirt_size"
TSHIRT_BY_EMPLOYEE = {"E100": "M", "E101": "L", "E102": "XL", "E103": "M"}
BLOCKED = {"blocked_provider", "awaiting_review", "mapping_complete", "awaiting_record_review",
           "preparation_complete", "error"}
AFTER_REMAP = {"preparation_complete", "awaiting_record_review", "error"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """App with the Groq provider selected but NO key -> adapter is None (no model calls possible).

    The key is set to an explicit blank instead of merely deleted: a blank process-env value
    overrides any GROQ_API_KEY an operator may have placed in backend/.env, and Settings.groq_key
    treats blank as unconfigured, so this test can never build a real Groq client.
    """
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AUTO_CONTINUE", "false")
    from app import config
    config.get_settings.cache_clear()
    settings = config.get_settings()
    assert settings.groq_key is None, "no-provider configuration expected"
    settings.ensure_dirs()
    try:
        with TestClient(create_app()) as c:
            health = c.get("/api/health").json()
            assert health["provider"] == "groq" and health["configured"] is False
            yield c
    finally:
        config.get_settings.cache_clear()


# ----------------------------------------------------------------------------- helpers
def _poll(c, jid, want, tries=400, delay=0.03):
    for _ in range(tries):
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] in want:
            return j
        time.sleep(delay)
    return c.get(f"/api/jobs/{jid}").json()


def _upload_csv(c, tenant: str) -> str:
    files = [("files", (CSV, (DEMO / CSV).read_bytes(), "text/csv"))]
    r = c.post("/api/jobs", files=files, data={"tenant_id": tenant})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _wait_work_quiescent(c, jid, tries=400, delay=0.03) -> list[dict]:
    """Wait until no work item of the job is pending/processing/retryable.

    The graph publishes ``blocked_provider`` from inside the MAP work item, a few milliseconds
    BEFORE the worker marks that item ``succeeded``. A proposal decision landing in that window
    finds the MAP item still ``processing`` and the automatic re-run is skipped (``already_active``),
    leaving the job blocked with zero open proposals (observed 5/25 in a stress probe; see the
    suspected-app-bug note in the handoff). Waiting for quiescence keeps these tests deterministic
    and mirrors the human latency of a real UI decision.
    """
    for _ in range(tries):
        items = c.get(f"/api/jobs/{jid}/work-items").json()
        if not any(w["status"] in ("pending", "processing", "retryable") for w in items):
            return items
        time.sleep(delay)
    return c.get(f"/api/jobs/{jid}/work-items").json()


def _blocked_job(c, tenant: str) -> str:
    jid = _upload_csv(c, tenant)
    j = _poll(c, jid, BLOCKED)
    assert j["status"] == "blocked_provider", (j["status"], j.get("error"))
    items = _wait_work_quiescent(c, jid)
    assert [w["status"] for w in items if w["kind"] == "MAP"] == ["succeeded"]
    return jid


def _defs(c, tenant: str) -> list[dict]:
    r = c.get(f"/api/tenants/{tenant}/custom-fields")
    assert r.status_code == 200, r.text
    return r.json()


def _def_keys(c, tenant: str) -> list[str]:
    return [d["key"] for d in _defs(c, tenant)]


def _proposals(c, jid: str, status: str = "open") -> list[dict]:
    r = c.get(f"/api/jobs/{jid}/custom-field-proposals", params={"status": status})
    assert r.status_code == 200, r.text
    return r.json()


def _proposal(c, jid: str, header: str, status: str = "open") -> dict:
    return next(p for p in _proposals(c, jid, status) if p["source_header"] == header)


def _decide(c, jid: str, p: dict, action: str, **extra):
    body = {"version": p["version"], "action": action, **extra}
    return c.post(f"/api/jobs/{jid}/custom-field-proposals/{p['id']}/decision", json=body)


def _mappings(c, jid: str) -> dict:
    r = c.get(f"/api/jobs/{jid}/mappings")
    assert r.status_code == 200, r.text
    return r.json()


def _row(rows: list[dict], header: str) -> dict:
    return next(r for r in rows if r["source_header"] == header)


def _audit(c, jid: str, event_type: str) -> list[dict]:
    return [a for a in c.get(f"/api/jobs/{jid}/audit").json() if a["event_type"] == event_type]


def _approve_tshirt_and_ignore_legacy(c, jid: str) -> tuple[dict, dict]:
    """The two human decisions that settle every proposal of a beta job. Returns (approve, ignore)."""
    approve = _decide(c, jid, _proposal(c, jid, "T-Shirt Size"), "approve", note="confirmed with Beta HR")
    assert approve.status_code == 200, approve.text
    ignore = _decide(c, jid, _proposal(c, jid, "Legacy Payroll Code"), "ignore",
                     note="legacy system reference", reason="not needed in target")
    assert ignore.status_code == 200, ignore.text
    assert ignore.json()["remap_triggered"] is True, ignore.json()   # last open proposal -> MAP re-run
    return approve.json(), ignore.json()


# ----------------------------------------------------------------------------- G3.10
def test_seeded_tenant_custom_field_maps_by_rule(client):
    seeded = _defs(client, "beta")
    assert [d["key"] for d in seeded] == ["badge_colour"]
    badge = seeded[0]
    assert badge["origin"] == "seed" and badge["tenant_id"] == "beta" and badge["path"] == BADGE_PATH
    assert badge["type"] == "enum" and sorted(badge["options"]) == ["Blue", "Green", "Red"]

    jid = _blocked_job(client, "beta")
    m = _mappings(client, jid)
    row = _row(m["accepted"], "Badge Colour")
    assert row["destination_kind"] == "CUSTOM_FIELD"
    assert row["target_field"] == BADGE_PATH
    assert row["custom_definition_id"] == badge["id"]
    assert row["method"] == "rule" and row["actor"] == "system" and row["status"] == "auto_accepted"
    assert m["counts"]["custom"] == 1 and m["counts"]["core"] == 8 and m["counts"]["collection"] == 0
    # No proposal is raised for a column an existing tenant definition already resolves.
    assert "Badge Colour" not in {p["source_header"] for p in _proposals(client, jid)}
    # The rule acceptance is audited with its destination type (ZERO model involvement).
    ev = [a for a in _audit(client, jid, "mapping_rule_accepted") if a["after"].get("target") == BADGE_PATH]
    assert len(ev) == 1 and ev[0]["actor"] == "system" and ev[0]["after"]["destination_kind"] == "CUSTOM_FIELD"


# ----------------------------------------------------------------------------- G3.11
def test_unknown_business_field_is_not_auto_created_and_job_is_blocked(client):
    jid = _blocked_job(client, "beta")
    assert _def_keys(client, "beta") == ["badge_colour"]               # nothing auto-created
    eff = client.get("/api/schema", params={"tenant_id": "beta"}).json()
    assert [f["name"] for f in eff["custom_fields"]] == ["badge_colour"]
    assert TSHIRT_PATH not in eff["target_paths"] and BADGE_PATH in eff["target_paths"]

    m = _mappings(client, jid)
    headers_accepted = {r["source_header"] for r in m["accepted"]}
    assert "T-Shirt Size" not in headers_accepted and "Legacy Payroll Code" not in headers_accepted
    assert m["ignored"] == []
    proposal_rows = {r["source_header"]: r for r in m["proposals"]}
    assert set(proposal_rows) == {"T-Shirt Size", "Legacy Payroll Code"}
    assert all(r["destination_kind"] == "PROPOSAL" and r["status"] == "proposal" and r["proposal_id"]
               for r in proposal_rows.values())
    assert m["counts"]["proposals"] == 2 and m["counts"]["unmapped"] == 0 and m["counts"]["needs_review"] == 0
    job = client.get(f"/api/jobs/{jid}").json()
    assert job["status"] == "blocked_provider" and "GROQ_API_KEY" in (job["error"] or "")
    # No record preparation happened while the job is blocked.
    assert client.get(f"/api/jobs/{jid}/candidates").json() == []


# ----------------------------------------------------------------------------- G3.12
def test_proposal_exists_for_unknown_field_with_deterministic_suggestion(client):
    jid = _blocked_job(client, "beta")
    p = _proposal(client, jid, "T-Shirt Size")
    assert p["status"] == "open" and p["version"] == 1 and p["origin"] == "no_provider"
    assert p["tenant_id"] == "beta" and p["job_id"] == jid and p["definition_id"] is None
    assert p["suggestion"] == {"key": "tshirt_size", "label": "T-Shirt Size", "type": "enum",
                               "options": ["L", "M", "XL"], "multi_value": False, "required": False,
                               "path": TSHIRT_PATH}
    assert p["observed_values"] == ["M", "L", "XL"] and p["non_empty_count"] == 4
    assert p["id"] == deterministic_proposal_id(jid, p["profile_id"])   # replay-safe identity

    legacy = _proposal(client, jid, "Legacy Payroll Code")
    assert legacy["suggestion"]["type"] == "string" and legacy["suggestion"]["key"] == "legacy_payroll_code"
    assert legacy["suggestion"]["options"] is None

    # The mapping view links the proposal row to the proposal id.
    assert _row(_mappings(client, jid)["proposals"], "T-Shirt Size")["proposal_id"] == p["id"]

    ev = _audit(client, jid, "custom_field_proposed")
    assert len(ev) == 2 and all(a["actor"] == "system" and a["category"] == "mapping" for a in ev)
    mine = next(a for a in ev if a["issue_id"] == p["id"])
    assert mine["after"]["suggested"]["key"] == "tshirt_size"
    assert mine["after"]["origin"] == "no_provider"
    assert mine["source_ref"]["header"] == "T-Shirt Size" and mine["source_ref"]["tenant_id"] == "beta"


# ----------------------------------------------------------------------------- G3.13
def test_approve_persists_definition_then_remaps_after_last_decision(client):
    jid = _blocked_job(client, "beta")
    ts = _proposal(client, jid, "T-Shirt Size")

    approve = _decide(client, jid, ts, "approve", note="confirmed with Beta HR")
    assert approve.status_code == 200, approve.text
    a = approve.json()
    assert a["outcome"] == "resolved"
    assert a["remap_triggered"] is False and a["remap_status"] is None   # one proposal still open
    assert a["open_proposals_remaining"] == 1
    assert a["proposal"]["status"] == "approved" and a["proposal"]["version"] == 2
    created = a["definition"]
    assert created["key"] == "tshirt_size" and created["tenant_id"] == "beta"
    assert created["origin"] == "proposal" and created["origin_proposal_id"] == ts["id"]
    assert created["origin_job_id"] == jid and created["created_by"] == "human"
    assert created["type"] == "enum" and created["options"] == ["L", "M", "XL"]
    assert a["proposal"]["definition_id"] == created["id"]

    defs = {d["key"]: d for d in _defs(client, "beta")}
    assert set(defs) == {"badge_colour", "tshirt_size"}
    assert defs["tshirt_size"]["id"] == created["id"] and defs["tshirt_size"]["origin"] == "proposal"
    assert defs["tshirt_size"]["path"] == TSHIRT_PATH
    # The effective contract for the tenant now carries the approved field.
    eff = client.get("/api/schema", params={"tenant_id": "beta"}).json()
    assert TSHIRT_PATH in eff["target_paths"]
    # Still blocked: the last proposal has not been decided, so no re-run yet.
    assert client.get(f"/api/jobs/{jid}").json()["status"] == "blocked_provider"

    ignore = _decide(client, jid, _proposal(client, jid, "Legacy Payroll Code"), "ignore",
                     note="legacy system reference")
    assert ignore.status_code == 200, ignore.text
    i = ignore.json()
    assert i["remap_triggered"] is True and i["remap_status"] == "queued"
    assert i["open_proposals_remaining"] == 0 and i["definition"] is None

    job = _poll(client, jid, AFTER_REMAP)
    assert job["status"] == "preparation_complete", (job["status"], job.get("error"))
    assert job["prep_summary"]["counts"]["eligible"] == 4
    map_items = [w for w in client.get(f"/api/jobs/{jid}/work-items").json() if w["kind"] == "MAP"]
    assert len(map_items) == 2 and all(w["status"] == "succeeded" for w in map_items)

    m = _mappings(client, jid)
    row = _row(m["accepted"], "T-Shirt Size")
    assert row["target_field"] == TSHIRT_PATH and row["destination_kind"] == "CUSTOM_FIELD"
    assert row["method"] == "human" and row["actor"] == "human" and row["status"] == "approved"
    assert row["custom_definition_id"] == created["id"] and row["note"] == "confirmed with Beta HR"
    assert m["counts"] == {"accepted": 10, "needs_review": 0, "unmapped": 0, "ignored": 1,
                           "proposals": 0, "core": 8, "collection": 0, "custom": 2}
    assert _proposals(client, jid) == []
    # The rule mapping of the seeded field survived the re-run untouched.
    assert _row(m["accepted"], "Badge Colour")["method"] == "rule"

    ev = _audit(client, jid, "custom_field_created")
    assert len(ev) == 1 and ev[0]["actor"] == "human" and ev[0]["category"] == "human"
    assert ev[0]["issue_id"] == ts["id"] and ev[0]["after"]["key"] == "tshirt_size"
    assert ev[0]["after"]["definition_id"] == created["id"] and ev[0]["reason"] == "confirmed with Beta HR"

    cands = {c["business_key"]: c for c in client.get(f"/api/jobs/{jid}/candidates").json()}
    assert set(cands) == set(TSHIRT_BY_EMPLOYEE)
    for bk, expected in TSHIRT_BY_EMPLOYEE.items():
        attr = next(x for x in cands[bk]["custom_attributes"] if x["key"] == "tshirt_size")
        assert attr["value"] == expected and attr["definition_id"] == created["id"]
        assert attr["status"] == "resolved" and attr["type"] == "enum" and attr["label"] == "T-Shirt Size"
        assert attr["provenance"], "custom attribute must keep its source provenance"
    ds = client.get(f"/api/jobs/{jid}/prepared-dataset").json()
    assert ds["tenant_id"] == "beta" and ds["ready_for_target"] == 4
    for emp in ds["employees"]:
        got = next(x for x in emp["custom_attributes"] if x["key"] == "tshirt_size")
        assert got == {"definition_id": created["id"], "key": "tshirt_size",
                       "value": TSHIRT_BY_EMPLOYEE[emp["employee_id"]]}


# ----------------------------------------------------------------------------- G3.14
def test_ignored_source_field_is_explicit_and_audited(client):
    jid = _blocked_job(client, "beta")
    legacy = _proposal(client, jid, "Legacy Payroll Code")
    _approve_tshirt_and_ignore_legacy(client, jid)
    assert _poll(client, jid, AFTER_REMAP)["status"] == "preparation_complete"

    m = _mappings(client, jid)
    row = _row(m["ignored"], "Legacy Payroll Code")
    assert row["destination_kind"] == "IGNORED" and row["status"] == "ignored"
    assert row["target_field"] is None and row["method"] == "human" and row["actor"] == "human"
    assert row["note"] == "legacy system reference"
    assert m["counts"]["ignored"] == 1
    for bucket in ("accepted", "unresolved", "proposals"):
        assert "Legacy Payroll Code" not in {r["source_header"] for r in m[bucket]}

    ev = _audit(client, jid, "source_field_ignored")
    assert len(ev) == 1 and ev[0]["actor"] == "human" and ev[0]["category"] == "human"
    assert ev[0]["issue_id"] == legacy["id"]
    assert ev[0]["after"]["note"] == "legacy system reference"
    assert ev[0]["after"]["destination_kind"] == "IGNORED" and ev[0]["after"]["status"] == "ignored"
    assert ev[0]["source_ref"]["header"] == "Legacy Payroll Code"

    # Ignoring creates NO definition and the proposal is closed as ignored (not superseded/deleted).
    assert "legacy_payroll_code" not in _def_keys(client, "beta")
    closed = _proposal(client, jid, "Legacy Payroll Code", status="all")
    assert closed["status"] == "ignored" and closed["version"] == 2 and closed["definition_id"] is None
    assert closed["resolution"]["action"] == "ignore" and closed["resolution"]["actor"] == "human"
    for cand in client.get(f"/api/jobs/{jid}/candidates").json():
        assert "legacy_payroll_code" not in {x["key"] for x in cand["custom_attributes"]}
        assert "legacy_payroll_code" not in cand["record"]


# ----------------------------------------------------------------------------- G3.15
def test_tenant_isolation_gives_each_tenant_its_own_definitions(client):
    assert client.get("/api/tenants/gamma/custom-fields").json() == []
    beta_before = _defs(client, "beta")
    assert [d["key"] for d in beta_before] == ["badge_colour"]

    jb = _blocked_job(client, "beta")
    jg = _blocked_job(client, "gamma")

    # gamma has no badge_colour definition -> the column is a proposal there, not a rule mapping.
    gamma_props = _proposals(client, jg)
    assert sorted(p["source_header"] for p in gamma_props) == ["Badge Colour", "Legacy Payroll Code", "T-Shirt Size"]
    assert all(p["tenant_id"] == "gamma" for p in gamma_props)
    gm = _mappings(client, jg)
    assert gm["counts"]["custom"] == 0 and gm["counts"]["proposals"] == 3
    assert _row(gm["proposals"], "Badge Colour")["destination_kind"] == "PROPOSAL"
    assert sorted(p["source_header"] for p in _proposals(client, jb)) == ["Legacy Payroll Code", "T-Shirt Size"]

    # Approve T-Shirt Size for beta: gamma's namespace is untouched.
    beta_def = _decide(client, jb, _proposal(client, jb, "T-Shirt Size"), "approve", note="beta").json()["definition"]
    assert beta_def["tenant_id"] == "beta"
    assert client.get("/api/tenants/gamma/custom-fields").json() == []
    assert "badge_colour" not in _def_keys(client, "gamma") and "tshirt_size" not in _def_keys(client, "gamma")

    # Approve the same business field for gamma: a SEPARATE definition, beta unchanged.
    r = _decide(client, jg, _proposal(client, jg, "T-Shirt Size"), "approve", note="gamma")
    assert r.status_code == 200, r.text
    gamma_def = r.json()["definition"]
    assert gamma_def["id"] != beta_def["id"]
    assert gamma_def["tenant_id"] == "gamma" and gamma_def["key"] == "tshirt_size" == beta_def["key"]
    assert _def_keys(client, "gamma") == ["tshirt_size"]
    beta_after = _defs(client, "beta")
    assert [(d["id"], d["key"]) for d in beta_after] == [(beta_before[0]["id"], "badge_colour"),
                                                          (beta_def["id"], "tshirt_size")]
    tenants = {t["id"]: t for t in client.get("/api/tenants").json()}
    assert tenants["beta"]["custom_field_count"] == 2 and tenants["gamma"]["custom_field_count"] == 1

    # A gamma proposal cannot be mapped onto beta's definition.
    r = _decide(client, jg, _proposal(client, jg, "Legacy Payroll Code"), "map_existing",
                definition_id=beta_def["id"])
    assert r.status_code == 400
    # Registering a field for gamma while citing a beta job is rejected too.
    r = client.post("/api/tenants/gamma/custom-fields",
                    json={"key": "locker_no", "label": "Locker", "type": "string", "job_id": jb})
    assert r.status_code == 400
    assert _def_keys(client, "gamma") == ["tshirt_size"]


# ----------------------------------------------------------------------------- G3.16
def test_replaying_the_same_approve_creates_no_duplicate_definition(client):
    jid = _blocked_job(client, "beta")
    ts = _proposal(client, jid, "T-Shirt Size")
    body = {"version": ts["version"], "action": "approve", "note": "confirmed with Beta HR"}
    url = f"/api/jobs/{jid}/custom-field-proposals/{ts['id']}/decision"
    first = client.post(url, json=body)
    assert first.status_code == 200 and first.json()["outcome"] == "resolved"

    replay = client.post(url, json=body)                               # stale version, same intent
    assert replay.status_code == 409 or replay.json().get("outcome") == "noop"
    replay_new = client.post(url, json=dict(body, version=ts["version"] + 1))
    assert replay_new.status_code == 409 or replay_new.json().get("outcome") == "noop"
    assert _def_keys(client, "beta").count("tshirt_size") == 1
    current = _proposal(client, jid, "T-Shirt Size", status="all")
    assert current["status"] == "approved" and current["version"] == 2
    assert len(_audit(client, jid, "custom_field_created")) == 1

    # Replaying the *ignore* decision: identical intent is a no-op, a different one is stale.
    lg = _proposal(client, jid, "Legacy Payroll Code")
    ign_body = {"version": lg["version"], "action": "ignore", "note": "legacy system reference"}
    ign_url = f"/api/jobs/{jid}/custom-field-proposals/{lg['id']}/decision"
    assert client.post(ign_url, json=ign_body).status_code == 200
    same = client.post(ign_url, json=ign_body)
    assert same.status_code == 200 and same.json()["outcome"] == "noop" and same.json()["remap_triggered"] is False
    different = client.post(ign_url, json=dict(ign_body, note="changed my mind"))
    assert different.status_code == 409
    assert len(_audit(client, jid, "source_field_ignored")) == 1
    assert _poll(client, jid, AFTER_REMAP)["status"] == "preparation_complete"
    assert _def_keys(client, "beta") == ["badge_colour", "tshirt_size"]


def test_approving_a_key_that_already_exists_in_the_tenant_is_rejected(client):
    jid = _blocked_job(client, "beta")
    lg = _proposal(client, jid, "Legacy Payroll Code")
    r = _decide(client, jid, lg, "approve", key="badge_colour", note="collides with the seed")
    assert r.status_code == 409, r.text
    assert "badge_colour" in r.json()["detail"] and "map_existing" in r.json()["detail"]
    # Nothing was written: proposal still open at v1, definitions unchanged, no audit.
    after = _proposal(client, jid, "Legacy Payroll Code")
    assert after["status"] == "open" and after["version"] == 1
    assert _def_keys(client, "beta") == ["badge_colour"]
    assert _audit(client, jid, "custom_field_created") == []
    assert client.get(f"/api/jobs/{jid}").json()["status"] == "blocked_provider"

    # Same within one job: approve T-Shirt Size, then try to approve Legacy under that key.
    assert _decide(client, jid, _proposal(client, jid, "T-Shirt Size"), "approve").status_code == 200
    r = _decide(client, jid, _proposal(client, jid, "Legacy Payroll Code"), "approve", key="tshirt_size")
    assert r.status_code == 409
    assert _def_keys(client, "beta").count("tshirt_size") == 1


def test_tenant_custom_field_api_rejects_duplicates_and_invalid_keys(client):
    url = "/api/tenants/beta/custom-fields"
    dup = client.post(url, json={"key": "badge_colour", "label": "Badge Colour", "type": "enum",
                                 "options": ["Red"]})
    assert dup.status_code == 409, dup.text
    bad = client.post(url, json={"key": "Bad Key!", "label": "Bad", "type": "string"})
    assert bad.status_code == 400 and "key must match" in bad.json()["detail"]
    core_clash = client.post(url, json={"key": "employee_id", "label": "Emp", "type": "string"})
    assert core_clash.status_code == 400
    bad_type = client.post(url, json={"key": "locker_no", "label": "Locker", "type": "blob"})
    assert bad_type.status_code == 400
    enum_no_options = client.post(url, json={"key": "locker_no", "label": "Locker", "type": "enum"})
    assert enum_no_options.status_code == 400
    assert _def_keys(client, "beta") == ["badge_colour"]               # none of the above wrote anything

    ok = client.post(url, json={"key": "locker_no", "label": "Locker Number", "type": "string",
                                "aliases": ["locker", "locker #"], "description": "Physical locker"})
    assert ok.status_code == 201, ok.text
    d = ok.json()
    assert d["key"] == "locker_no" and d["path"] == "custom_attributes.locker_no"
    assert d["origin"] == "api" and d["created_by"] == "human" and d["tenant_id"] == "beta"
    assert d["aliases"] == ["locker", "locker #"] and d["origin_proposal_id"] is None
    assert _def_keys(client, "beta") == ["badge_colour", "locker_no"]
    again = client.post(url, json={"key": "locker_no", "label": "Locker Number", "type": "string"})
    assert again.status_code == 409
    assert _def_keys(client, "beta") == ["badge_colour", "locker_no"]
    # Only beta sees it.
    assert client.get("/api/tenants/gamma/custom-fields").json() == []
    assert "locker_no" not in _def_keys(client, "default")


def test_map_existing_maps_proposal_to_existing_definition_without_creating_one(client):
    jid = _blocked_job(client, "beta")
    badge = _defs(client, "beta")[0]
    lg = _proposal(client, jid, "Legacy Payroll Code")

    assert _decide(client, jid, lg, "map_existing").status_code == 400                    # no id
    assert _decide(client, jid, lg, "map_existing", definition_id="cfd_doesnotexist").status_code == 400
    assert _proposal(client, jid, "Legacy Payroll Code")["version"] == 1                  # untouched

    r = _decide(client, jid, lg, "map_existing", definition_id=badge["id"], note="same badge attribute")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["outcome"] == "resolved" and out["definition"] is None                     # nothing created
    assert out["proposal"]["status"] == "mapped_existing" and out["proposal"]["definition_id"] == badge["id"]
    assert out["proposal"]["resolution"]["definition_id"] == badge["id"]
    assert _def_keys(client, "beta") == ["badge_colour"]

    row = _row(_mappings(client, jid)["accepted"], "Legacy Payroll Code")
    assert row["destination_kind"] == "CUSTOM_FIELD" and row["target_field"] == BADGE_PATH
    assert row["custom_definition_id"] == badge["id"]
    assert row["method"] == "human" and row["actor"] == "human" and row["status"] == "approved"
    assert _mappings(client, jid)["counts"]["custom"] == 2

    ev = _audit(client, jid, "custom_field_mapped_existing")
    assert len(ev) == 1 and ev[0]["actor"] == "human" and ev[0]["issue_id"] == lg["id"]
    assert ev[0]["after"]["definition_id"] == badge["id"] and ev[0]["after"]["target"] == BADGE_PATH
    assert _audit(client, jid, "custom_field_created") == []


def test_map_target_maps_proposal_to_a_core_path_and_rejects_unknown_paths(client):
    jid = _blocked_job(client, "beta")
    ts = _proposal(client, jid, "T-Shirt Size")

    bad = _decide(client, jid, ts, "map_target", target_path="nonexistent_path")
    assert bad.status_code == 400, bad.text
    # A custom path is not a map_target destination (map_existing owns that case).
    assert _decide(client, jid, ts, "map_target", target_path=BADGE_PATH).status_code == 400
    assert _decide(client, jid, ts, "map_target").status_code == 400                       # missing path
    assert _decide(client, jid, ts, "bogus").status_code == 422                           # unknown action
    assert _proposal(client, jid, "T-Shirt Size")["version"] == 1                          # untouched

    r = _decide(client, jid, ts, "map_target", target_path="nationality", note="client stores size here")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["outcome"] == "resolved" and out["definition"] is None
    assert out["proposal"]["status"] == "mapped_target"
    assert out["proposal"]["resolution"]["target_path"] == "nationality"
    assert _def_keys(client, "beta") == ["badge_colour"]                                   # no definition

    m = _mappings(client, jid)
    row = _row(m["accepted"], "T-Shirt Size")
    assert row["destination_kind"] == "CORE_FIELD" and row["target_field"] == "nationality"
    assert row["status"] == "corrected" and row["method"] == "human" and row["custom_definition_id"] is None
    assert m["counts"]["core"] == 9 and m["counts"]["custom"] == 1

    # Settle the last proposal -> re-run -> the human core mapping is honoured downstream.
    ignore = _decide(client, jid, _proposal(client, jid, "Legacy Payroll Code"), "ignore", note="legacy")
    assert ignore.status_code == 200 and ignore.json()["remap_triggered"] is True
    job = _poll(client, jid, AFTER_REMAP)
    assert job["status"] == "preparation_complete", (job["status"], job.get("error"))
    assert _row(_mappings(client, jid)["accepted"], "T-Shirt Size")["target_field"] == "nationality"
    cands = {c["business_key"]: c for c in client.get(f"/api/jobs/{jid}/candidates").json()}
    for bk, expected in TSHIRT_BY_EMPLOYEE.items():
        assert cands[bk]["record"]["nationality"]["value"] == expected
        assert "tshirt_size" not in {x["key"] for x in cands[bk]["custom_attributes"]}
