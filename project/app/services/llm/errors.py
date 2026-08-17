"""Typed provider errors for the LLM layer (MUS-43).

Adapters re-raise their SDK/httpx exceptions as one of the classes below;
callers branch on :attr:`LLMError.retryable`. The ``RuntimeError`` base is
load-bearing: pre-taxonomy callers and tests expect ``RuntimeError`` for a
missing key. Mapping lives in the pure functions :func:`map_anthropic_error`
and :func:`map_httpx_error`.
"""

from __future__ import annotations

import json
import math
from typing import Any

import anthropic
import httpx

# HTTP status codes we classify by hand. Everything else falls through to the
# range checks in _from_status_code.
_STATUS_RATE_LIMIT = 429
_STATUS_AUTH = frozenset({401, 403})
_STATUS_BAD_REQUEST = frozenset({400, 404, 409, 413, 422})
# 408 and 425 are the two 4xx codes that clear on their own, so they must not
# fall into the non-retryable 4xx bucket below.
_STATUS_REQUEST_TIMEOUT = 408
_STATUS_TOO_EARLY = 425


# Whose problem is it — deliberately a separate axis from `retryable`:
#   provider      -- their side, expected to clear on its own. Wait.
#   configuration -- ours, will not clear. Fix the key, model name, or URL.
#   contract      -- they answered and we could not read it.
FAULT_PROVIDER = "provider"
FAULT_CONFIGURATION = "configuration"
FAULT_CONTRACT = "contract"
FAULT_UNKNOWN = "unknown"


class LLMError(RuntimeError):
    """Base class for every provider failure the LLM layer surfaces.

    Subclasses ``RuntimeError`` (see module docstring). ``retryable`` and
    ``fault_domain`` are *class* attributes: they are properties of the failure
    category, readable from the type alone.
    """

    retryable: bool = False
    fault_domain: str = FAULT_UNKNOWN

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        status_code: int | None = None,
        retry_after: float | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        # Seconds the provider asked us to wait; None means "no guidance".
        self.retry_after = retry_after
        # Original SDK/httpx exception; chained via `raise ... from` at the
        # adapter's raise site, not here.
        self.cause = cause
        # Filled by the adapter via with_latency(); the mappers stay pure.
        self.latency_s: float | None = None

    def with_latency(self, latency_s: float | None) -> "LLMError":
        """Record how long the failed attempt took and return ``self`` (so it
        composes onto a mapper call at the raise site). Same measurement as
        :attr:`LLMResult.latency_s`."""
        self.latency_s = latency_s
        return self


class LLMRateLimitError(LLMError):
    """Provider throttled us (HTTP 429). Retry after ``retry_after`` seconds."""

    retryable = True
    fault_domain = FAULT_PROVIDER


class LLMTimeoutError(LLMError):
    """The request did not complete in time (connect, read, write, or pool).

    Distinct from :class:`LLMTransientError`: a timeout says nothing about
    whether the provider received the request.
    """

    retryable = True
    fault_domain = FAULT_PROVIDER


class LLMTransientError(LLMError):
    """Provider-side failure that is expected to clear on its own.

    5xx, connection resets, and Anthropic's 529 "overloaded".
    """

    retryable = True
    fault_domain = FAULT_PROVIDER


class LLMAuthError(LLMError):
    """Credentials are missing, invalid, or lack permission (401/403).

    Never retried: hammering an endpoint with a bad key wastes the retry budget
    and, on some providers, earns a longer lockout.
    """

    fault_domain = FAULT_CONFIGURATION


class LLMBadRequestError(LLMError):
    """We sent something the provider rejected (400/404/409/413/422).

    Not retryable by definition — the same request will be rejected the same
    way. Almost always a config or prompt-size bug on our side.
    """

    fault_domain = FAULT_CONFIGURATION


class LLMMalformedResponseError(LLMError):
    """The provider answered, but not with something we can read.

    Unparseable JSON, a response missing the fields the wire format promises,
    or an empty completion. Treated as non-retryable: a provider that returns
    a shape we don't understand is a contract problem, not a blip.
    """

    fault_domain = FAULT_CONTRACT


class LLMEmptyCompletionError(LLMMalformedResponseError):
    """The provider answered with a sample we cannot use: nothing, or a turn it
    marked as a tool call while carrying no tool call we could read.

    Unlike the parent, **retryable**: the wire format was fine and the same
    request often succeeds on the next roll (observed on groq/gpt-oss-20b). A
    subclass so every existing ``except LLMMalformedResponseError`` still
    catches it.
    """

    retryable = True
    fault_domain = FAULT_PROVIDER


class LLMUnexpectedError(LLMError):
    """Something that is not a provider failure, caught where one was expected.

    Lets a caller replace ``except Exception`` with a typed catch while
    :attr:`failure_kind` still names the exception that actually occurred.
    """

    fault_domain = FAULT_UNKNOWN

    def __init__(self, message: str, *, failure_kind: str, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.failure_kind = failure_kind


def wrap_unexpected(exc: BaseException, provider: str | None = None) -> LLMError:
    """Return ``exc`` if it is already typed, else wrap it.

    The message stays the original's, undecorated: it ends up in
    ``OutreachAction.further_action`` for a human reader. The classification
    lives in :attr:`failure_kind` (span attribute ``outreach.failure.kind``).
    """
    if isinstance(exc, LLMError):
        return exc
    kind = type(exc).__qualname__
    return LLMUnexpectedError(
        str(exc) or kind,
        failure_kind=kind,
        provider=provider,
        cause=exc,
    )


# ---------------------------------------------------------------------------
# Retry-After parsing
# ---------------------------------------------------------------------------


# Upper bound on an honoured Retry-After; beyond five minutes, failing beats
# waiting.
MAX_RETRY_AFTER_SECONDS = 300.0


def _parse_retry_after(headers: Any) -> float | None:
    """Read a numeric ``Retry-After`` (in seconds) out of response headers.

    The RFC's HTTP-date form is not parsed (no LLM provider sends it) and
    yields ``None``. The value is clamped to :data:`MAX_RETRY_AFTER_SECONDS`
    here — it flows into a ``sleep()``, so every consumer inherits the bound.
    """
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after")
    except (AttributeError, TypeError):
        # Defensive: `headers` is read off the exception via getattr, so a test
        # double or a future SDK could put anything here.
        return None
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    # Negative, NaN or infinite waits are treated as no guidance.
    if not math.isfinite(value) or value < 0:
        return None
    return min(value, MAX_RETRY_AFTER_SECONDS)


def _response_headers(exc: BaseException) -> Any:
    response = getattr(exc, "response", None)
    return getattr(response, "headers", None)


# ---------------------------------------------------------------------------
# status-code dispatch (shared by both providers)
# ---------------------------------------------------------------------------


def _from_status_code(
    status_code: int | None,
    message: str,
    *,
    provider: str | None,
    retry_after: float | None,
    cause: BaseException | None,
) -> LLMError:
    """Map a bare HTTP status to the taxonomy.

    Shared by the Anthropic and httpx mappers so the two providers can never
    disagree about what a 503 means.
    """
    kwargs: dict[str, Any] = {
        "provider": provider,
        "status_code": status_code,
        "retry_after": retry_after,
        "cause": cause,
    }
    if status_code == _STATUS_RATE_LIMIT:
        return LLMRateLimitError(message, **kwargs)
    if status_code is not None and status_code >= 500:
        return LLMTransientError(message, **kwargs)
    if status_code == _STATUS_REQUEST_TIMEOUT:
        return LLMTimeoutError(message, **kwargs)
    if status_code == _STATUS_TOO_EARLY:
        return LLMTransientError(message, **kwargs)
    if status_code in _STATUS_AUTH:
        return LLMAuthError(message, **kwargs)
    if status_code in _STATUS_BAD_REQUEST:
        return LLMBadRequestError(message, **kwargs)
    if status_code is not None and 400 <= status_code < 500:
        # Unenumerated 4xx: client-side, not retryable (408/425 handled above).
        return LLMBadRequestError(message, **kwargs)
    return LLMError(message, **kwargs)


# ---------------------------------------------------------------------------
# Anthropic SDK -> taxonomy
# ---------------------------------------------------------------------------


def map_anthropic_error(exc: BaseException, provider: str | None = None) -> LLMError:
    """Translate an ``anthropic`` SDK exception into an :class:`LLMError`.

    Pure; verified against ``anthropic==0.109.1``. Ordering matters:
    ``APITimeoutError`` subclasses ``APIConnectionError`` so it is checked
    first, and the specific ``APIStatusError`` subclasses are checked before
    the base, with a status-code fallback for subclasses a future SDK adds.
    """
    retry_after = _parse_retry_after(_response_headers(exc))
    message = str(exc) or exc.__class__.__name__

    def build(cls: type[LLMError], status_code: int | None = None) -> LLMError:
        return cls(
            message,
            provider=provider,
            status_code=status_code if status_code is not None else _status_of(exc),
            retry_after=retry_after,
            cause=exc,
        )

    # APITimeoutError subclasses APIConnectionError -- check it first.
    if isinstance(exc, anthropic.APITimeoutError):
        return build(LLMTimeoutError)
    if isinstance(exc, anthropic.APIConnectionError):
        return build(LLMTransientError)

    if isinstance(exc, anthropic.APIResponseValidationError):
        return build(LLMMalformedResponseError)

    if isinstance(exc, anthropic.RateLimitError):
        return build(LLMRateLimitError)
    if isinstance(exc, (anthropic.InternalServerError, anthropic.OverloadedError)):
        return build(LLMTransientError)
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return build(LLMAuthError)
    if isinstance(
        exc,
        (
            anthropic.BadRequestError,
            anthropic.NotFoundError,
            anthropic.ConflictError,
            anthropic.UnprocessableEntityError,
            anthropic.RequestTooLargeError,
        ),
    ):
        return build(LLMBadRequestError)

    if isinstance(exc, anthropic.RetryableError):
        # SDK-middleware signal whose whole meaning is "try again"; falling
        # through to the non-retryable base would invert it.
        return build(LLMTransientError)

    if isinstance(exc, anthropic.APIStatusError):
        # Unnamed status-carrying subclass: dispatch on the code so it degrades
        # to the right category instead of to the base LLMError.
        return _from_status_code(
            exc.status_code,
            message,
            provider=provider,
            retry_after=retry_after,
            cause=exc,
        )

    # Residual AnthropicError: unknown retryability -- fail closed.
    return build(LLMError)


def _status_of(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


# ---------------------------------------------------------------------------
# httpx -> taxonomy (the OpenAI-compatible adapters)
# ---------------------------------------------------------------------------

# Response-shape failures from data["choices"][0]["message"]["content"]:
#   JSONDecodeError -> body wasn't JSON
#   KeyError        -> a promised key is missing
#   IndexError      -> "choices" came back empty
#   TypeError       -> a key held null / the wrong type (e.g. content: null)
#   AttributeError  -> "content" was JSON but not a string
_MALFORMED_EXCEPTIONS = (json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError)


# How much of an error body to keep. `str(httpx.HTTPStatusError)` omits the
# body -- the only field that says *why* -- so it is appended, bounded here
# because spans and logs read this message too.
_BODY_EXCERPT_MAX_CHARS = 300


def _response_detail(response: httpx.Response) -> str:
    """The provider's own explanation, bounded, or ``""`` when there isn't one.

    Prefers ``error.message``; anything else degrades to a truncated excerpt.
    Total by construction — ``.text`` raises on an unread streaming response,
    and the mappers must never raise.
    """
    try:
        body = response.text
    except Exception:
        return ""
    if not body:
        return ""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        candidate = error.get("message") if isinstance(error, dict) else payload.get("message")
        if isinstance(candidate, str) and candidate.strip():
            body = candidate.strip()
    body = " ".join(body.split())
    if len(body) > _BODY_EXCERPT_MAX_CHARS:
        body = body[:_BODY_EXCERPT_MAX_CHARS].rstrip() + "..."
    return body


def map_httpx_error(exc: BaseException, provider: str | None = None) -> LLMError:
    """Translate an ``httpx`` (or response-parsing) exception into an
    :class:`LLMError`.

    Pure. ``TimeoutException`` is checked before ``TransportError`` — it is a
    subclass, the same ordering trap as Anthropic's ``APITimeoutError``.
    """
    message = str(exc) or exc.__class__.__name__

    if isinstance(exc, httpx.HTTPStatusError):
        retry_after = _parse_retry_after(exc.response.headers)
        detail = _response_detail(exc.response)
        return _from_status_code(
            exc.response.status_code,
            f"{message} {detail}" if detail else message,
            provider=provider,
            retry_after=retry_after,
            cause=exc,
        )

    # TimeoutException derives from TransportError — check it first.
    if isinstance(exc, httpx.TimeoutException):
        return LLMTimeoutError(message, provider=provider, cause=exc)
    if isinstance(exc, httpx.TransportError):
        return LLMTransientError(message, provider=provider, cause=exc)

    if isinstance(exc, _MALFORMED_EXCEPTIONS):
        return LLMMalformedResponseError(
            f"Provider returned an unreadable chat-completion response: {message}",
            provider=provider,
            cause=exc,
        )

    # Residual: unknown retryability, non-retryable base. httpx.InvalidURL is
    # NOT an httpx.HTTPError — it derives from Exception — so a malformed
    # base_url only gets here because the adapter names it explicitly.
    return LLMError(message, provider=provider, cause=exc)


__all__ = [
    "FAULT_CONFIGURATION",
    "FAULT_CONTRACT",
    "FAULT_PROVIDER",
    "FAULT_UNKNOWN",
    "LLMError",
    "LLMRateLimitError",
    "LLMTimeoutError",
    "LLMTransientError",
    "LLMAuthError",
    "LLMBadRequestError",
    "LLMMalformedResponseError",
    "LLMEmptyCompletionError",
    "LLMUnexpectedError",
    "map_anthropic_error",
    "map_httpx_error",
    "wrap_unexpected",
]
