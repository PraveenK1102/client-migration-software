"""Structured-output schema for model mapping proposals.

Two things live here, kept deliberately SEPARATE from the target employee schema:

1. ``build_response_json_schema`` — the JSON Schema handed to Groq
   (``response_format.type = "json_schema"``, ``strict = true``). It is built to
   satisfy Groq strict mode: every object lists all properties in ``required``,
   ``additionalProperties`` is ``false``, and optional-ness is expressed with
   nullable type unions rather than by omitting the key.

2. ``ProposalResponse`` / ``ProposalItem`` — Pydantic models used to re-validate
   the model's output. Valid JSON is necessary, never sufficient: the
   deterministic policy still runs afterwards.

A *required* output property here does NOT mean a missing employee fact should be
invented. ``proposed_target_field`` is nullable precisely so the model can decline
to map a column, and ``is_ambiguous`` lets it flag genuine ambiguity instead of
being forced to choose.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class ProposalItem(BaseModel):
    source_column_id: str
    source_header: str
    proposed_target_field: str | None          # null => model declines / unresolved
    alternative_target_fields: list[str] = Field(default_factory=list)
    is_ambiguous: bool
    ambiguity_reason: str | None
    evidence: list[str] = Field(default_factory=list)
    confidence: float | None                   # model-reported ONLY; never treated as calibrated

    @field_validator("confidence")
    @classmethod
    def _clamp_conf(cls, v: float | None) -> float | None:
        if v is None:
            return None
        return max(0.0, min(1.0, float(v)))


class ProposalResponse(BaseModel):
    proposals: list[ProposalItem]


def build_response_json_schema(target_field_names: list[str]) -> dict:
    """Build the Groq strict-mode JSON schema for a mapping-proposal response.

    ``target_field_names`` constrains ``proposed_target_field`` / alternatives to
    valid schema fields (the policy re-checks this regardless).
    """
    field_enum = list(target_field_names)
    return {
        "name": "mapping_proposals",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["proposals"],
            "properties": {
                "proposals": {
                    "type": "array",
                    "description": "One proposal per source column provided.",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "source_column_id",
                            "source_header",
                            "proposed_target_field",
                            "alternative_target_fields",
                            "is_ambiguous",
                            "ambiguity_reason",
                            "evidence",
                            "confidence",
                        ],
                        "properties": {
                            "source_column_id": {
                                "type": "string",
                                "description": "Echo the provided source column id exactly.",
                            },
                            "source_header": {
                                "type": "string",
                                "description": "Echo the provided source header exactly.",
                            },
                            "proposed_target_field": {
                                "type": ["string", "null"],
                                "enum": field_enum + [None],
                                "description": "Best single target field, or null if none fits / unresolved.",
                            },
                            "alternative_target_fields": {
                                "type": "array",
                                "description": "Other plausible target fields (competing meanings). Empty if none.",
                                "items": {"type": "string", "enum": field_enum},
                            },
                            "is_ambiguous": {
                                "type": "boolean",
                                "description": "True when the column plausibly maps to more than one target.",
                            },
                            "ambiguity_reason": {
                                "type": ["string", "null"],
                                "description": "Short reason if ambiguous, else null.",
                            },
                            "evidence": {
                                "type": "array",
                                "description": "Short factual observations that support the proposal (header wording, value shapes). Not chain-of-thought.",
                                "items": {"type": "string"},
                            },
                            "confidence": {
                                "type": ["number", "null"],
                                "description": "Self-reported 0..1 confidence. Advisory only.",
                            },
                        },
                    },
                }
            },
        },
    }
