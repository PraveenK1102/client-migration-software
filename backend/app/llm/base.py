"""Model-adapter interface and typed errors.

Business logic and tests depend on :class:`ModelAdapter`, never on Groq SDK
objects directly. Only two implementations exist: the real Groq adapter and an
explicitly test/demo-only fake adapter. Every adapter reports its ``kind`` so the
application can label a run and never present a fake as a live model call.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field

from .schema import ProposalResponse
from .transform_schema import ValueMapResponse


@dataclass
class ProposalColumn:
    source_column_id: str
    header: str
    observed_types: dict[str, int]
    format_indicators: dict[str, object]
    samples: list[str]


@dataclass
class TransformProposalRequest:
    """Everything the model may see to propose a value map for ONE already-mapped enum column.

    Bounded and PII-safe: only the low-cardinality source values needing interpretation, the target
    enum labels/descriptions, and any deterministic mappings already known (for context)."""

    profile_id: str
    source_header: str
    target_field: str
    target_label: str
    target_description: str
    allowed_values: list[str]
    source_values: list[str]                 # unmapped low-cardinality values only
    already_mapped: dict[str, str] = field(default_factory=dict)
    table_ref: dict = field(default_factory=dict)
    # M3I: set on the ONE bounded semantic-clarification pass that revisits values the first proposal
    # merely DECLINED (not ambiguous, not taxonomy). The adapter adds a clarification instruction that
    # a lexical/semantic equivalent is NOT ambiguous just because the wording differs. Safety is
    # unchanged: the same fixed enum + strict schema, and the output is re-validated deterministically.
    clarify: bool = False


@dataclass
class ProposalRequest:
    """Everything the model is allowed to see for one source table."""

    table_id: str
    source_table_ref: dict          # {original_filename, sheet_name, table_id}
    columns: list[ProposalColumn]


@dataclass
class ProposalCallMeta:
    adapter_kind: str               # "groq" | "fake"
    model_id: str
    attempts: int
    notes: list[str] = field(default_factory=list)
    # M3D observability: measured per successful call. Token counts are populated only when the
    # provider returns usage (Groq does); the fake adapter leaves them None and latency ~0.
    latency_ms: float = 0.0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    status: str = "ok"              # "ok" | "error:<category>" — filled by the caller on failure


# --- Error taxonomy ------------------------------------------------------

class ModelError(Exception):
    """Base for all adapter errors. Carries an operator-facing, actionable message."""


class ModelConfigError(ModelError):
    """Non-retryable: bad credentials, permission denied, unknown model, invalid request/schema.

    Retrying cannot help; the operator must fix configuration.
    """


class RecoverableModelError(ModelError):
    """Transient failures exhausted (timeouts, 429s, 5xx) or the operation deadline was hit.

    The operation can be retried later once the upstream recovers.
    """


class ModelResponseInvalid(ModelError):
    """The model returned invalid/incomplete/truncated structured output within the attempt budget."""


class ModelAdapter(abc.ABC):
    @property
    @abc.abstractmethod
    def kind(self) -> str:
        """'groq' for the real model, 'fake' for the test/demo adapter."""

    @property
    @abc.abstractmethod
    def model_id(self) -> str:
        ...

    @abc.abstractmethod
    async def propose_mappings(
        self, *, schema_public: dict, request: ProposalRequest
    ) -> tuple[ProposalResponse, ProposalCallMeta]:
        """Return validated proposals for one source table (one call per table)."""

    async def propose_transforms(
        self, *, request: TransformProposalRequest
    ) -> tuple[ValueMapResponse, ProposalCallMeta]:
        """Propose a value map for one already-mapped low-cardinality enum column (M3C Tier 2).

        Optional capability: the default declines (empty mapping) so an adapter that does not
        implement it never blocks the deterministic pipeline. The output is always re-validated
        deterministically against the target enum before anything is applied.
        """
        meta = ProposalCallMeta(adapter_kind=self.kind, model_id=self.model_id, attempts=0,
                                notes=["propose_transforms not implemented"])
        return ValueMapResponse(mappings=[]), meta

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None
