"""Explicitly TEST / OFFLINE-ONLY fake adapter.

TEST / OFFLINE ONLY. The production runtime and the live demo NEVER silently substitute this adapter
for Groq: the real adapter is selected whenever ``LLM_PROVIDER=groq`` and a key is present, and if Groq
is unconfigured the pipeline runs deterministically and marks provider-dependent columns as blocked
rather than faking a model call (see ``runtime._build_adapter``). This adapter is used only when
``LLM_PROVIDER=fake`` is set explicitly — by the automated test suite and offline validation.

Produces deterministic proposals from simple header heuristics so tests and offline
demos are reproducible WITHOUT a network call, API key, or charges. It always
reports ``kind == "fake"`` so the application labels the run and never presents it
as a live model result.

Note it deliberately returns an *overconfident* proposal for generic/ambiguous
date columns (proposed target set, is_ambiguous=False, high confidence). This lets
tests prove that the deterministic policy — not the model's self-reported
confidence — is what routes the genuinely ambiguous date-role case to review.
"""
from __future__ import annotations

from ..text_util import tokens as _tokens
from .base import ModelAdapter, ProposalCallMeta, ProposalRequest, TransformProposalRequest
from .schema import ProposalItem, ProposalResponse
from .transform_schema import ValueMapItem, ValueMapResponse

_HIRE_KW = ("hire", "hired", "hiring", "join", "joined", "joining", "doj", "onboard")
_CONTRACT_KW = ("contract", "agreement")
_GENERIC_DATE_KW = ("start", "date", "begin", "effective", "from")


def _norm(s: str) -> list[str]:
    return list(_tokens(s))


class FakeModelAdapter(ModelAdapter):
    def __init__(self, model_id: str = "fake/deterministic-1") -> None:
        self._model_id = model_id

    @property
    def kind(self) -> str:
        return "fake"

    @property
    def model_id(self) -> str:
        return self._model_id

    def _classify(self, header: str, indicators: dict) -> ProposalItem:
        toks = set(_norm(header))
        h = header.lower()

        def item(target, *, ambiguous=False, reason=None, alts=None, conf=0.9, ev=None):
            return ProposalItem(
                source_column_id="",  # filled by caller
                source_header=header,
                proposed_target_field=target,
                alternative_target_fields=alts or [],
                is_ambiguous=ambiguous,
                ambiguity_reason=reason,
                evidence=ev or [f"header '{header}'"],
                confidence=conf,
            )

        if {"email", "mail", "e"} & toks or "mail" in h:
            return item("work_email", ev=[f"header '{header}' contains a mail token"])
        if "name" in toks:
            return item("full_name", ev=[f"header '{header}' contains 'name'"])
        if {"id", "emp", "employee", "no", "number", "code", "staff", "payroll"} & toks:
            return item("employee_id", ev=[f"header '{header}' looks like an identifier"])
        if {"dept", "department", "team", "division", "group", "function"} & toks:
            return item("department", ev=[f"header '{header}' looks like a department"])
        if any(k in h for k in _CONTRACT_KW):
            return item("contract_start_date", ev=[f"header '{header}' mentions a contract"])
        if any(k in h for k in _HIRE_KW):
            return item("hire_date", ev=[f"header '{header}' mentions hiring/joining"])
        # Generic/underspecified date column: overconfident hire_date on purpose.
        if any(k in h for k in _GENERIC_DATE_KW) or indicators.get("looks_date_like"):
            return item(
                "hire_date",
                conf=0.96,
                ev=[f"header '{header}' holds date-like values"],
            )
        return item(None, conf=0.2, ev=[f"header '{header}' did not match a target concept"])

    async def propose_mappings(
        self, *, schema_public: dict, request: ProposalRequest
    ) -> tuple[ProposalResponse, ProposalCallMeta]:
        items: list[ProposalItem] = []
        for col in request.columns:
            it = self._classify(col.header, col.format_indicators)
            it.source_column_id = col.source_column_id
            items.append(it)
        meta = ProposalCallMeta(
            adapter_kind=self.kind, model_id=self._model_id, attempts=1, notes=["fake adapter"]
        )
        return ProposalResponse(proposals=items), meta

    async def propose_transforms(
        self, *, request: TransformProposalRequest
    ) -> tuple[ValueMapResponse, ProposalCallMeta]:
        """Deterministic value-map proposal for offline/CI use.

        Confidently maps only a clear LEXICAL/semantic match — a source value that CONTAINS a target
        label as a word (e.g. "Voluntarily Terminated" -> terminated, "Active" -> active). Anything
        else is declined (target_value=null) so a genuine business-taxonomy translation (e.g.
        "Marketing"/"IT"/"HR" -> a department) escalates to a human instead of being guessed.
        """
        mappings: list[ValueMapItem] = []
        allowed = list(request.allowed_values)
        for sv in request.source_values:
            sv_toks = set(_tokens(sv))
            match = None
            for label in allowed:
                lt = set(_tokens(label))
                if lt and (lt <= sv_toks or _tokens(sv) == _tokens(label)):
                    match = label
                    break
            if match is not None:
                mappings.append(ValueMapItem(source_value=sv, target_value=match,
                                             relation="lexical_semantic_match", ambiguous=False,
                                             evidence=[f"source value '{sv}' contains the label '{match}'"]))
            else:
                mappings.append(ValueMapItem(source_value=sv, target_value=None, relation="no_match",
                                             ambiguous=False,
                                             evidence=[f"'{sv}' has no lexical match to an allowed label"]))
        meta = ProposalCallMeta(adapter_kind=self.kind, model_id=self._model_id, attempts=1,
                                notes=["fake adapter transform"])
        return ValueMapResponse(mappings=mappings), meta
