"""FastAPI application entrypoint.

Lifespan owns the long-lived :class:`AppContext`: the reused AsyncGroq client, the
file-backed LangGraph checkpointer, the SQLite database, and the compiled graph.
All are created on startup and closed on shutdown.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.routes import router
from .config import get_settings
from .runtime import AppContext


@asynccontextmanager
async def lifespan(app: FastAPI):
    ctx = await AppContext.create(get_settings())
    app.state.ctx = ctx
    try:
        yield
    finally:
        await ctx.aclose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Darwinbox Employee Migration — Milestone 1 (mapping)",
        version="0.1.0",
        lifespan=lifespan,
    )
    # Localhost-only single-user prototype; allow the Vite dev server origins.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173", "http://127.0.0.1:5173",
            "http://localhost:4173", "http://127.0.0.1:4173",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


app = create_app()
