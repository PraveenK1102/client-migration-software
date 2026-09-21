"""Structured-output schema for model VALUE-MAP transform proposals (M3C Tier 2).

Kept separate from the mapping-proposal schema. The model is asked ONLY to interpret the semantics
of a bounded set of low-cardinality source values against an ALREADY-ESTABLISHED target enum, one
source value at a time. It cannot emit code, invent a target value outside the enum, or see whole
employee records — and everything it returns is re-validated deterministically
(:func:`app.enum_inference.validate_model_value_map`) before any transform is applied.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ValueMapItem(BaseModel):
    source_value: str
    target_value: str | None          # null => model declines / no confident target
    relation: str                     # lexical_semantic_match | taxonomy_translation | no_match | ...
    ambiguous: bool
    evidence: list[str] = Field(default_factory=list)


class ValueMapResponse(BaseModel):
    mappings: list[ValueMapItem]


def build_value_map_json_schema(allowed_values: list[str]) -> dict:
    """Groq strict-mode JSON schema for a value-map response.

    ``allowed_values`` constrains ``target_value`` to the target enum (the policy re-checks anyway).
    """
    enum = list(allowed_values)
    return {
        "name": "value_map_proposals",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["mappings"],
            "properties": {
                "mappings": {
                    "type": "array",
                    "description": "One entry per provided source value.",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["source_value", "target_value", "relation", "ambiguous", "evidence"],
                        "properties": {
                            "source_value": {"type": "string",
                                             "description": "Echo the provided source value exactly."},
                            "target_value": {"type": ["string", "null"], "enum": enum + [None],
                                             "description": "Target enum label, or null if none fits / ambiguous."},
                            "relation": {"type": "string",
                                         "description": "How the values relate: lexical_semantic_match, "
                                                        "taxonomy_translation, or no_match."},
                            "ambiguous": {"type": "boolean",
                                          "description": "True when the source value plausibly maps to more than one label."},
                            "evidence": {"type": "array", "items": {"type": "string"},
                                         "description": "Short factual observations. Not chain-of-thought."},
                        },
                    },
                }
            },
        },
    }
