"""Application runtime context (M3A single-node).

Owns: the hardened SQLite Database, the LocalBlobStore, the model adapter (or None when
unconfigured — the fake adapter is never substituted at runtime), the LangGraph
AsyncSqliteSaver + compiled mapping/preparation graphs, the TargetEmployeeGateway, and the
bounded local WorkerPool. Upload persists blobs + metadata + work items and returns; the
worker pool does all parsing/mapping/preparation/reconciliation. Human-decision endpoints
persist a decision and enqueue the continuation atomically (see routes), never running the
graph in the request.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import aiosqlite
import httpx
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .blobstore import LocalBlobStore
from .config import Settings, get_settings
from .db import Database
from .graph import GraphDeps, build_graphs
from .llm.base import ModelAdapter
from .llm.fake_adapter import FakeModelAdapter
from .schema_loader import TargetSchema, get_target_schema, load_tenant_seeds
from .target_gateway import TargetEmployeeGateway
from .worker import WorkerPool


@dataclass
class AppContext:
    settings: Settings
    db: Database
    schema: object
    blobstore: LocalBlobStore
    adapter: ModelAdapter | None
    adapter_kind: str
    model_id: str
    provider: str
    gateway: TargetEmployeeGateway
    checkpointer: AsyncSqliteSaver
    _conn: aiosqlite.Connection
    mapping_graph: object
    preparation_graph: object
    workers: WorkerPool | None = None
    _target_client: httpx.AsyncClient | None = None
    _target_app: object | None = None          # mock target ASGI app (test/demo admin access)
    _started: bool = field(default=False)

    @classmethod
    async def create(cls, settings: Settings | None = None, *,
                     adapter_override: "ModelAdapter | None" = None, use_override: bool = False,
                     gateway: TargetEmployeeGateway | None = None,
                     start_workers: bool | None = None) -> "AppContext":
        settings = settings or get_settings()
        settings.ensure_dirs()
        # Optional engineering observability: turns LangGraph run tracing on when LangSmith is
        # configured, and is a harmless no-op otherwise. Must run before the graphs are built.
        from .observability import build_tracer, configure_tracing
        configure_tracing(settings)
        tracer = build_tracer(settings)          # LangSmith model-call tracer, or a no-op when off
        schema = get_target_schema()
        db = Database(settings.app_db_path, busy_timeout_ms=settings.sqlite_busy_timeout_ms)
        blobstore = LocalBlobStore(settings.blob_store_root)
        seed_tenants(db, settings)

        if use_override:
            adapter = adapter_override
            adapter_kind = adapter.kind if adapter is not None else "groq"
            model_id = adapter.model_id if adapter is not None else settings.groq_model
            provider = settings.llm_provider.lower()
        else:
            adapter, adapter_kind, model_id, provider = _build_adapter(settings, schema)

        conn = await aiosqlite.connect(str(settings.checkpoint_db_path))
        checkpointer = AsyncSqliteSaver(conn)
        await checkpointer.setup()
        mapping_graph, preparation_graph = build_graphs(
            GraphDeps(db=db, adapter=adapter, schema=schema, model_id=model_id, tracer=tracer),
            checkpointer)

        target_client: httpx.AsyncClient | None = None
        if gateway is not None:
            gw = gateway
        elif settings.target_inprocess:
            # Single-node: run the mock target in-process behind httpx's ASGI transport, so the
            # app reconciles over the same HTTP + gateway seam with no separate port. Swap to a
            # real remote target by setting target_inprocess=False (see config).
            from mock_target.service import create_app as create_target_app
            target_app = create_target_app(db_path=str(settings.target_db_path))
            target_client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=target_app), base_url="http://target")
            gw = TargetEmployeeGateway(client=target_client,
                                       batch_size=settings.target_lookup_batch_size)
        else:
            gw = TargetEmployeeGateway(base_url=settings.target_base_url,
                                       batch_size=settings.target_lookup_batch_size)
        ctx = cls(settings=settings, db=db, schema=schema, blobstore=blobstore, adapter=adapter,
                  adapter_kind=adapter_kind, model_id=model_id, provider=provider, gateway=gw,
                  checkpointer=checkpointer, _conn=conn, mapping_graph=mapping_graph,
                  preparation_graph=preparation_graph, _target_client=target_client,
                  _target_app=target_app if settings.target_inprocess else None)
        ctx.workers = WorkerPool(ctx)
        should_start = settings.start_workers if start_workers is None else start_workers
        if should_start:
            await ctx.workers.start()
            ctx._started = True
        return ctx

    async def aclose(self) -> None:
        if self.workers is not None and self._started:
            await self.workers.stop()
        if self.adapter is not None:
            try:
                await self.adapter.aclose()
            except Exception:  # pragma: no cover
                pass
        try:
            await self.gateway.aclose()
        except Exception:  # pragma: no cover
            pass
        if self._target_client is not None:
            try:
                await self._target_client.aclose()
            except Exception:  # pragma: no cover
                pass
        try:
            await self._conn.close()
        except Exception:  # pragma: no cover
            pass
        self.db.close()

    # --- effective schema (core + collections + a tenant's custom-field definitions) --------
    def effective_schema(self, tenant_id: str | None) -> TargetSchema:
        tid = tenant_id or self.settings.default_tenant_id
        return self.schema.with_custom_definitions(self.db.get_custom_field_definitions(tid), tid)

    def effective_schema_for_job(self, job_id: str) -> TargetSchema:
        return self.effective_schema(self.db.job_tenant(job_id))

    # --- introspection (used by tests) -----------------------------------
    @property
    def provider_status(self) -> dict:
        return self.settings.provider_status()

    @property
    def adapter_error(self) -> str | None:
        if self.provider_status["configured"]:
            return None
        return ("Groq is not configured. Deterministic processing is available; columns that require "
                "model interpretation cannot proceed until GROQ_API_KEY is set in backend/.env.")

    async def get_state(self, thread_id: str):
        return await self.mapping_graph.aget_state({"configurable": {"thread_id": thread_id}})

    async def get_prep_state(self, thread_id: str):
        return await self.preparation_graph.aget_state({"configurable": {"thread_id": thread_id}})


def _build_adapter(settings: Settings, schema) -> tuple[ModelAdapter | None, str, str, str]:
    provider = settings.llm_provider.lower()
    if provider == "fake":
        a = FakeModelAdapter()
        return a, a.kind, a.model_id, "fake"
    if provider == "groq":
        if settings.groq_key is None:
            return None, "groq", settings.groq_model, "groq"
        from groq import AsyncGroq

        from .llm.groq_adapter import GroqAdapter
        client = AsyncGroq(api_key=settings.groq_key, timeout=settings.llm_timeout_seconds,
                           max_retries=0)
        adapter = GroqAdapter(client=client, model_id=settings.groq_model,
                              max_attempts=settings.llm_max_attempts,
                              operation_deadline_seconds=settings.llm_operation_deadline_seconds,
                              semaphore=asyncio.Semaphore(settings.llm_max_concurrency),
                              target_field_names=list(schema.field_names))

        async def _close():
            await client.close()
        adapter.aclose = _close  # type: ignore[method-assign]
        return adapter, "groq", settings.groq_model, "groq"
    return None, provider, settings.groq_model, provider


def seed_tenants(db: Database, settings: Settings) -> None:
    """Idempotently load tenant custom-field SEEDS (schemas/tenants/*.yaml|json) and ensure the
    default tenant exists. INSERT OR IGNORE on (tenant, key): a restart never duplicates a definition
    and never overwrites one created later through the proposal workflow. The internal default tenant
    is always ensured; the demo/synthetic file seeds are skipped when ``seed_demo_tenants`` is off
    (e.g. a clean production-style instance that should show no built-in organizations)."""
    db.upsert_tenant(settings.default_tenant_id, settings.default_tenant_id)
    if not settings.seed_demo_tenants:
        return
    for seed in load_tenant_seeds(settings.tenant_seeds_dir):
        db.upsert_tenant(seed["tenant_id"], seed.get("name"))
        for cf in seed["custom_fields"]:
            if not cf.get("key"):
                continue
            db.add_custom_field_definition(
                tenant_id=seed["tenant_id"], key=str(cf["key"]), label=str(cf.get("label") or cf["key"]),
                type=str(cf.get("type") or "string"), required=bool(cf.get("required")),
                options=[str(o) for o in cf.get("options", [])] if cf.get("options") else None,
                multi_value=bool(cf.get("multi_value")), description=cf.get("description"),
                aliases=[str(a) for a in cf.get("aliases", [])], origin="seed", created_by="system")
