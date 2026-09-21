"""Explicit shared dependency bundle handed to the graph builders."""
from __future__ import annotations

from dataclasses import dataclass

from ..llm.base import ModelAdapter
from ..observability import ModelTracer
from ..schema_loader import TargetSchema


@dataclass
class GraphDeps:
    db: object
    adapter: ModelAdapter | None          # may be None when the provider is unconfigured
    schema: TargetSchema
    model_id: str = "unconfigured"
    tracer: ModelTracer | None = None     # LangSmith model-call tracer (no-op when tracing off)
