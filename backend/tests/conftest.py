"""Shared pytest fixtures.

All automated tests run OFFLINE with the fake adapter (LLM_PROVIDER=fake) or with a
mocked Groq client. None require a live API key or incur API charges. The live smoke
test (test_smoke_groq_live.py) is separately marked and skipped by default.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
SAMPLE = REPO / "sample-data"


@pytest.fixture
def temp_settings(tmp_path, monkeypatch):
    """Fresh Settings pointing at an isolated temp data dir, provider=fake."""
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AUTO_CONTINUE", "false")   # stage isolation for tests that poll individual stages
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    # The offline suite must NEVER reach LangSmith (no network, no key echoed): force tracing off and
    # drop any key inherited from backend/.env. The live LangSmith smoke test opts in explicitly.
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
    from app import config
    config.get_settings.cache_clear()
    settings = config.get_settings()
    settings.ensure_dirs()
    yield settings
    config.get_settings.cache_clear()


@pytest.fixture
def sample_files() -> dict[str, bytes]:
    return {
        "legacy_hr.csv": (SAMPLE / "legacy_hr.csv").read_bytes(),
        "employee_export.xlsx": (SAMPLE / "employee_export.xlsx").read_bytes(),
    }


@pytest.fixture
async def ctx(temp_settings):
    """A live AppContext with the fake adapter."""
    from app.runtime import AppContext
    c = await AppContext.create(temp_settings)
    try:
        yield c
    finally:
        await c.aclose()


def ingest_sample_job(ctx, files: dict[str, bytes]) -> str:
    """Ingest files into a new job WITHOUT starting the graph. Returns job_id."""
    import uuid

    from app.ingest import parse_file

    parsed = []
    for name, data in files.items():
        ext = Path(name).suffix.lower()
        fid = f"file_{uuid.uuid4().hex[:12]}"
        pf = parse_file(filename=name, data=data, stored_name=f"{fid}{ext}",
                        max_bytes=ctx.settings.max_upload_bytes,
                        max_rows=ctx.settings.max_rows_per_table, file_id=fid)
        parsed.append((pf, data))
    job_id = ctx.db.create_job(schema_version=ctx.schema.version, provider=ctx.provider,
                               model_id=ctx.model_id, adapter_kind=ctx.adapter_kind)
    job_dir = ctx.settings.uploads_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    for pf, data in parsed:
        (job_dir / pf.stored_name).write_bytes(data)
        ctx.db.add_source_file(job_id, file_id=pf.file_id, original_filename=pf.original_filename,
                               stored_name=pf.stored_name, content_type=pf.content_type,
                               size_bytes=pf.size_bytes)
        for t in pf.tables:
            ctx.db.add_source_table(job_id, t)
        ctx.db.add_source_rows(job_id, pf.records)
        ctx.db.add_parsing_issues(job_id, pf.issues)
    ctx.db.set_job_stage(job_id, status="processing", stage="ingested")
    return job_id
