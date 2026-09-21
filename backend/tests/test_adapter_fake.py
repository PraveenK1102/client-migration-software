"""Fake adapter: deterministic output, clearly labelled as NOT a live model."""
from __future__ import annotations

from app.llm.base import ProposalColumn, ProposalRequest
from app.llm.fake_adapter import FakeModelAdapter
from app.schema_loader import get_target_schema


async def test_fake_adapter_is_labelled_and_deterministic():
    adapter = FakeModelAdapter()
    assert adapter.kind == "fake"  # never presented as a live model
    schema = get_target_schema()
    req = ProposalRequest(
        table_id="tbl_1", source_table_ref={"table_id": "tbl_1"},
        columns=[
            ProposalColumn("c1", "EmployeeNumber", {"number": 3}, {}, ["001"]),
            ProposalColumn("c2", "Work Email", {"text": 3}, {"email_ratio": 1.0}, ["a@x.com"]),
            ProposalColumn("c3", "Start", {"date": 3}, {"looks_date_like": True}, ["2021-01-01"]),
        ],
    )
    resp, meta = await adapter.propose_mappings(schema_public=schema.public_dict(), request=req)
    assert meta.adapter_kind == "fake"
    by_id = {p.source_column_id: p for p in resp.proposals}
    assert by_id["c1"].proposed_target_field == "employee_id"
    assert by_id["c2"].proposed_target_field == "work_email"
    # generic 'Start' is proposed overconfidently (the policy, not the model, escalates it)
    assert by_id["c3"].proposed_target_field == "hire_date"
    assert by_id["c3"].is_ambiguous is False
