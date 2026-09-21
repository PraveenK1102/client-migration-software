"""LIVE Groq smoke test — REAL model call.

Skipped by default. It runs when RUN_LIVE_GROQ=1 AND a Groq key is available either
in the process environment (GROQ_API_KEY) OR in backend/.env (loaded by the app's
settings — the same source the application uses). Gating on the process env alone
would wrongly skip when the key lives only in backend/.env, so we check both.

It exercises one real structured mapping call on a tiny synthetic table and asserts
a schema-valid proposal comes back. This is the ONLY test that proves live execution;
the fake-adapter and mocked tests do NOT. Run it explicitly, e.g.:

    RUN_LIVE_GROQ=1 pytest tests/test_smoke_groq_live.py -v -s
"""
from __future__ import annotations

import asyncio
import os

import pytest


def live_enabled() -> bool:
    """True only when live runs are requested AND a key is configured.

    Checks the process env first, then the app settings (which load backend/.env),
    so a key that lives only in backend/.env still enables the test.
    """
    if os.getenv("RUN_LIVE_GROQ") != "1":
        return False
    if os.getenv("GROQ_API_KEY"):
        return True
    try:
        from app.config import get_settings
        return bool(get_settings().groq_api_key)
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not live_enabled(),
    reason="Live Groq test: set RUN_LIVE_GROQ=1 and a GROQ_API_KEY (env or backend/.env).",
)


@pytest.mark.live
async def test_live_groq_structured_mapping():
    from groq import AsyncGroq

    from app.config import get_settings
    from app.llm.base import ProposalColumn, ProposalRequest
    from app.llm.groq_adapter import GroqAdapter
    from app.schema_loader import get_target_schema

    settings = get_settings()
    schema = get_target_schema()
    client = AsyncGroq(api_key=settings.groq_api_key, timeout=settings.llm_timeout_seconds,
                       max_retries=0)
    adapter = GroqAdapter(
        client=client, model_id=settings.groq_model, max_attempts=settings.llm_max_attempts,
        operation_deadline_seconds=settings.llm_operation_deadline_seconds,
        semaphore=asyncio.Semaphore(settings.llm_max_concurrency),
        target_field_names=list(schema.field_names),
    )
    try:
        req = ProposalRequest(
            table_id="live_tbl", source_table_ref={"table_id": "live_tbl", "original_filename": "synthetic"},
            columns=[
                ProposalColumn("c_emp", "EmployeeNumber", {"number": 3}, {"has_leading_zero_values": True}, ["001", "002"]),
                ProposalColumn("c_mail", "Mail", {"text": 3}, {"email_ratio": 1.0}, ["a@x.com", "b@x.com"]),
                ProposalColumn("c_start", "Start", {"date": 3}, {"looks_date_like": True}, ["2021-03-15"]),
            ],
        )
        # Use the lean model-facing schema view — the same bounded context the mapping graph sends
        # (keeps the request within tight TPM budgets on constrained tiers).
        resp, meta = await adapter.propose_mappings(schema_public=schema.public_dict(for_model=True), request=req)
        assert meta.adapter_kind == "groq"
        assert len(resp.proposals) >= 1
        for p in resp.proposals:
            if p.proposed_target_field is not None:
                assert p.proposed_target_field in schema.field_names
        print(f"\n[LIVE] model={meta.model_id} attempts={meta.attempts} "
              f"proposals={[(p.source_header, p.proposed_target_field) for p in resp.proposals]}")
    finally:
        await client.close()


@pytest.mark.live
async def test_live_groq_enum_value_map_is_validated():
    """The Tier-2 value-map path: the model proposes source_value -> target enum, and the result is
    re-validated deterministically against the target enum (no invented values). One bounded call."""
    import asyncio

    from app.config import get_settings
    from app.enum_inference import validate_model_value_map
    from app.llm.base import TransformProposalRequest
    from app.llm.groq_adapter import GroqAdapter
    from app.schema_loader import get_target_schema
    from groq import AsyncGroq

    settings = get_settings()
    schema = get_target_schema()
    gender = schema.get("gender")
    client = AsyncGroq(api_key=settings.groq_api_key, timeout=settings.llm_timeout_seconds, max_retries=0)
    adapter = GroqAdapter(
        client=client, model_id=settings.groq_model, max_attempts=settings.llm_max_attempts,
        operation_deadline_seconds=settings.llm_operation_deadline_seconds,
        semaphore=asyncio.Semaphore(settings.llm_max_concurrency),
        target_field_names=list(schema.field_names))
    try:
        # "Woman"/"Man" are not declared aliases, so they exercise the MODEL semantic path.
        req = TransformProposalRequest(
            profile_id="c_gender", source_header="Gender", target_field="gender",
            target_label=gender.label, target_description=gender.description,
            allowed_values=list(gender.enum_values or ()), source_values=["Woman", "Man"],
            already_mapped={}, table_ref={"table_id": "live_tbl"})
        resp, meta = await adapter.propose_transforms(request=req)
        proposals = [{"source_value": it.source_value, "target_value": it.target_value,
                      "ambiguous": it.ambiguous} for it in resp.mappings]
        validated = validate_model_value_map(gender, proposals, ["Woman", "Man"])
        # The model can never introduce an out-of-enum value; every accepted target is valid.
        assert all(v in (gender.enum_values or ()) for v in validated.accepted.values())
        print(f"\n[LIVE-TRANSFORM] model={meta.model_id} accepted={validated.accepted} "
              f"rejected={validated.rejected}")
    finally:
        await client.close()
