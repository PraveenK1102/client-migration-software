"""Groq model adapter.

Uses the official Groq SDK ``AsyncGroq`` client with Chat Completions and
``response_format = {"type": "json_schema", strict: true}`` (non-streaming, no tool
calling / browsing / code execution / Compound).

Retry policy (single controller — SDK auto-retries are disabled with
``max_retries=0`` where the client is constructed):
- At most ``max_attempts`` total API attempts per proposal.
- Transient failures (connection, timeout, 429, retryable 5xx) → bounded
  exponential backoff with jitter, honouring ``Retry-After``.
- Non-retryable failures (auth, permission, unknown model, invalid request/schema)
  are raised immediately as :class:`ModelConfigError`.
- At most ONE corrective retry for invalid/incomplete/truncated structured output,
  inside the same total attempt budget. Truncated output is never accepted.
- If a required wait exceeds the overall operation deadline, a
  :class:`RecoverableModelError` is raised rather than retrying early.
The adapter never fabricates a mapping and never falls back to a fake model.
"""
from __future__ import annotations

import asyncio
import json
import random
import time

from groq import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)

from .base import (
    ModelAdapter,
    ModelConfigError,
    ModelResponseInvalid,
    ProposalCallMeta,
    ProposalRequest,
    RecoverableModelError,
)
from .base import TransformProposalRequest
from .prompt import (
    SYSTEM_PROMPT,
    TRANSFORM_SYSTEM_PROMPT,
    build_transform_user_prompt,
    build_user_prompt,
)
from .schema import ProposalResponse, build_response_json_schema
from .transform_schema import ValueMapResponse, build_value_map_json_schema

_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 20.0
# A TPM (tokens-per-minute) 413 clears when the per-minute window rolls over; wait a meaningful
# fraction of a minute between retries rather than the sub-second base backoff.
_TPM_BACKOFF_FLOOR = 20.0


def _safe_msg(exc: Exception) -> str:
    msg = str(exc)
    return (msg[:300] + "…") if len(msg) > 300 else msg


def _usage(completion) -> dict:
    """Extract bounded, non-PII token usage from a Groq completion (absent -> all None)."""
    u = getattr(completion, "usage", None)
    return {
        "prompt_tokens": getattr(u, "prompt_tokens", None) if u else None,
        "completion_tokens": getattr(u, "completion_tokens", None) if u else None,
        "total_tokens": getattr(u, "total_tokens", None) if u else None,
    }


def _is_rate_413(exc: Exception) -> bool:
    """A Groq 413 that is a per-minute TOKEN-RATE condition (TPM), not a permanently oversized request.
    Groq returns 413 (not 429) for TPM overage; such a request succeeds once the minute resets, so it
    is treated as a recoverable rate condition (bounded backoff) rather than a config error."""
    if getattr(exc, "status_code", None) != 413:
        return False
    msg = str(exc).lower()
    return "tokens per minute" in msg or "tpm" in msg or "rate limit" in msg or "request too large" in msg


def _retry_after_seconds(exc: Exception) -> float | None:
    """Extract a Retry-After header value (seconds) if the SDK error carries a response."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    val = headers.get("retry-after") or headers.get("Retry-After")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


class GroqAdapter(ModelAdapter):
    def __init__(
        self,
        *,
        client,
        model_id: str,
        max_attempts: int,
        operation_deadline_seconds: float,
        semaphore: asyncio.Semaphore,
        target_field_names: list[str],
    ) -> None:
        self._client = client
        self._model_id = model_id
        self._max_attempts = max(1, max_attempts)
        self._deadline = operation_deadline_seconds
        self._sem = semaphore
        self._default_target_names = list(target_field_names)
        self._response_schema = build_response_json_schema(target_field_names)

    def _schema_for(self, schema_public: dict) -> dict:
        """Constrain the structured output to the EFFECTIVE contract handed in for this call (core +
        collection paths + the tenant's custom fields); falls back to the constructor's list."""
        paths = (schema_public or {}).get("target_paths")
        if isinstance(paths, list) and paths and paths != self._default_target_names:
            return build_response_json_schema([str(p) for p in paths])
        return self._response_schema

    @property
    def kind(self) -> str:
        return "groq"

    @property
    def model_id(self) -> str:
        return self._model_id

    def _backoff_delay(self, attempt: int, exc: Exception) -> float:
        retry_after = _retry_after_seconds(exc)
        if retry_after is not None:
            return retry_after
        raw = min(_BACKOFF_BASE * (2 ** (attempt - 1)), _BACKOFF_CAP)
        return raw + random.uniform(0.0, raw * 0.5)  # full-ish jitter

    async def propose_mappings(
        self, *, schema_public: dict, request: ProposalRequest
    ) -> tuple[ProposalResponse, ProposalCallMeta]:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(schema_public, request)},
        ]
        deadline_at = time.monotonic() + self._deadline
        started = time.monotonic()
        response_schema = self._schema_for(schema_public)
        notes: list[str] = []
        attempts = 0
        corrective_used = False

        while attempts < self._max_attempts:
            attempts += 1
            if time.monotonic() >= deadline_at:
                raise RecoverableModelError(
                    f"Operation deadline ({self._deadline:.0f}s) reached before attempt {attempts}."
                )
            try:
                async with self._sem:
                    completion = await self._client.chat.completions.create(
                        model=self._model_id,
                        messages=messages,
                        response_format={"type": "json_schema", "json_schema": response_schema},
                        temperature=0,
                        stream=False,
                    )
            except (AuthenticationError, PermissionDeniedError) as e:
                raise ModelConfigError(
                    f"Groq auth/permission error ({type(e).__name__}): {_safe_msg(e)}. "
                    f"Check GROQ_API_KEY and account access. Not retried."
                ) from e
            except NotFoundError as e:
                raise ModelConfigError(
                    f"Groq model '{self._model_id}' not found/accessible: {_safe_msg(e)}. "
                    f"Check GROQ_MODEL. Not retried."
                ) from e
            except (BadRequestError, UnprocessableEntityError) as e:
                raise ModelConfigError(
                    f"Groq rejected the request (invalid request/schema, {type(e).__name__}): "
                    f"{_safe_msg(e)}. Not retried."
                ) from e
            except (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError) as e:
                if attempts >= self._max_attempts:
                    raise RecoverableModelError(
                        f"Groq transient failure after {attempts} attempt(s) "
                        f"({type(e).__name__}): {_safe_msg(e)}."
                    ) from e
                delay = self._backoff_delay(attempts, e)
                if time.monotonic() + delay > deadline_at:
                    raise RecoverableModelError(
                        f"Required wait {delay:.1f}s ({type(e).__name__}) exceeds the operation "
                        f"deadline; reporting recoverable error instead of retrying early."
                    ) from e
                notes.append(f"transient {type(e).__name__}; backoff {delay:.2f}s")
                await asyncio.sleep(delay)
                continue
            except APIStatusError as e:
                status = getattr(e, "status_code", None) or 0
                tpm = _is_rate_413(e)
                if (tpm or 500 <= status < 600) and attempts < self._max_attempts:
                    delay = self._backoff_delay(attempts, e)
                    if tpm:
                        delay = max(delay, _TPM_BACKOFF_FLOOR)   # a TPM window resets by the next minute
                    if time.monotonic() + delay > deadline_at:
                        raise RecoverableModelError(
                            f"{'TPM 413' if tpm else f'Server error {status}'}; wait {delay:.1f}s "
                            f"exceeds deadline. Recoverable."
                        ) from e
                    notes.append(f"{'tpm 413' if tpm else f'server {status}'}; backoff {delay:.2f}s")
                    await asyncio.sleep(delay)
                    continue
                if tpm:
                    raise RecoverableModelError(
                        f"Groq TPM limit (413) after {attempts} attempt(s): {_safe_msg(e)}."
                    ) from e
                raise ModelConfigError(
                    f"Groq returned non-retryable status {status}: {_safe_msg(e)}."
                ) from e

            # --- Got a completion: validate structured output ---------------
            choice = completion.choices[0]
            finish = getattr(choice, "finish_reason", None)
            content = getattr(choice.message, "content", None)

            invalid_reason: str | None = None
            resp: ProposalResponse | None = None
            if finish == "length":
                invalid_reason = "response truncated (finish_reason=length); not accepted"
            elif not content:
                invalid_reason = "empty response content"
            else:
                try:
                    resp = ProposalResponse.model_validate(json.loads(content))
                except (json.JSONDecodeError, ValueError) as e:
                    invalid_reason = f"schema/JSON validation failed: {_safe_msg(e)}"

            if resp is not None:
                usage = _usage(completion)
                return resp, ProposalCallMeta(
                    adapter_kind=self.kind,
                    model_id=self._model_id,
                    attempts=attempts,
                    notes=notes,
                    latency_ms=(time.monotonic() - started) * 1000.0,
                    status="ok",
                    **usage,
                )

            # Invalid/incomplete/truncated output.
            if corrective_used or attempts >= self._max_attempts:
                raise ModelResponseInvalid(
                    f"Invalid structured output after {attempts} attempt(s): {invalid_reason}."
                )
            corrective_used = True
            notes.append(f"corrective retry ({invalid_reason})")
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Your previous reply was not valid for the required schema "
                        f"({invalid_reason}). Reply again with ONLY a complete JSON object "
                        "matching the schema, one proposal per source column."
                    ),
                }
            )

        # Safety net (should be unreachable given the checks above).
        raise RecoverableModelError("Attempt budget exhausted without a valid response.")

    async def propose_transforms(
        self, *, request: TransformProposalRequest
    ) -> tuple[ValueMapResponse, ProposalCallMeta]:
        response_schema = build_value_map_json_schema(request.allowed_values)
        messages = [
            {"role": "system", "content": TRANSFORM_SYSTEM_PROMPT},
            {"role": "user", "content": build_transform_user_prompt(request)},
        ]
        deadline_at = time.monotonic() + self._deadline
        started = time.monotonic()
        notes: list[str] = []
        attempts = 0
        corrective_used = False

        while attempts < self._max_attempts:
            attempts += 1
            if time.monotonic() >= deadline_at:
                raise RecoverableModelError(
                    f"Operation deadline ({self._deadline:.0f}s) reached before attempt {attempts}.")
            try:
                async with self._sem:
                    completion = await self._client.chat.completions.create(
                        model=self._model_id, messages=messages,
                        response_format={"type": "json_schema", "json_schema": response_schema},
                        temperature=0, stream=False)
            except (AuthenticationError, PermissionDeniedError) as e:
                raise ModelConfigError(
                    f"Groq auth/permission error ({type(e).__name__}): {_safe_msg(e)}. Not retried.") from e
            except NotFoundError as e:
                raise ModelConfigError(
                    f"Groq model '{self._model_id}' not found/accessible: {_safe_msg(e)}. Not retried.") from e
            except (BadRequestError, UnprocessableEntityError) as e:
                raise ModelConfigError(
                    f"Groq rejected the request ({type(e).__name__}): {_safe_msg(e)}. Not retried.") from e
            except (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError) as e:
                if attempts >= self._max_attempts:
                    raise RecoverableModelError(
                        f"Groq transient failure after {attempts} attempt(s) ({type(e).__name__}): "
                        f"{_safe_msg(e)}.") from e
                delay = self._backoff_delay(attempts, e)
                if time.monotonic() + delay > deadline_at:
                    raise RecoverableModelError(
                        f"Required wait {delay:.1f}s ({type(e).__name__}) exceeds the operation deadline.") from e
                notes.append(f"transient {type(e).__name__}; backoff {delay:.2f}s")
                await asyncio.sleep(delay)
                continue
            except APIStatusError as e:
                status = getattr(e, "status_code", None) or 0
                tpm = _is_rate_413(e)
                if (tpm or 500 <= status < 600) and attempts < self._max_attempts:
                    delay = self._backoff_delay(attempts, e)
                    if tpm:
                        delay = max(delay, _TPM_BACKOFF_FLOOR)
                    if time.monotonic() + delay > deadline_at:
                        raise RecoverableModelError(
                            f"{'TPM 413' if tpm else f'Server error {status}'}; wait {delay:.1f}s "
                            f"exceeds deadline.") from e
                    notes.append(f"{'tpm 413' if tpm else f'server {status}'}; backoff {delay:.2f}s")
                    await asyncio.sleep(delay)
                    continue
                if tpm:
                    raise RecoverableModelError(
                        f"Groq TPM limit (413) after {attempts} attempt(s): {_safe_msg(e)}.") from e
                raise ModelConfigError(
                    f"Groq returned non-retryable status {status}: {_safe_msg(e)}.") from e

            choice = completion.choices[0]
            finish = getattr(choice, "finish_reason", None)
            content = getattr(choice.message, "content", None)
            invalid_reason: str | None = None
            resp: ValueMapResponse | None = None
            if finish == "length":
                invalid_reason = "response truncated (finish_reason=length); not accepted"
            elif not content:
                invalid_reason = "empty response content"
            else:
                try:
                    resp = ValueMapResponse.model_validate(json.loads(content))
                except (json.JSONDecodeError, ValueError) as e:
                    invalid_reason = f"schema/JSON validation failed: {_safe_msg(e)}"
            if resp is not None:
                return resp, ProposalCallMeta(adapter_kind=self.kind, model_id=self._model_id,
                                              attempts=attempts, notes=notes, status="ok",
                                              latency_ms=(time.monotonic() - started) * 1000.0,
                                              **_usage(completion))
            if corrective_used or attempts >= self._max_attempts:
                raise ModelResponseInvalid(
                    f"Invalid structured output after {attempts} attempt(s): {invalid_reason}.")
            corrective_used = True
            notes.append(f"corrective retry ({invalid_reason})")
            messages.append({"role": "user", "content": (
                "Your previous reply was not valid for the required schema "
                f"({invalid_reason}). Reply again with ONLY a complete JSON object matching the schema, "
                "one entry per source value.")})

        raise RecoverableModelError("Attempt budget exhausted without a valid response.")
