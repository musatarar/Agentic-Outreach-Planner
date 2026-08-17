"""Typed provider errors for the LLM layer (MUS-43).

Every provider failure used to surface as whatever exception the SDK or
``httpx`` happened to raise, which meant nothing downstream could answer the
only question that matters at the call site: **is this worth retrying?** A 429
from Groq's free tier (wait a second and it disappears) and a malformed
``base_url`` (will fail forever) both arrived as an opaque ``Exception`` and
both ended up stringified into a lead's ``further_action``, indistinguishable
from real BD work.

This module gives that failure a type. Adapters catch their provider's native
exceptions and re-raise one of the six classes below; callers branch on
:attr:`LLMError.retryable` instead of on a vendor's class hierarchy.

**The** ``RuntimeError`` **base is load-bearing, not incidental.**
``openai_compatible`` has always raised ``RuntimeError`` for a missing API key
and the adapter tests assert exactly that. Basing the taxonomy on
``RuntimeError`` lets those raises become ``LLMAuthError`` without touching a
single caller or test — the taxonomy is strictly additive to the existing
contract, so adopting it can't break code that hasn't been updated yet.

Mapping lives in two pure functions, :func:`map_anthropic_error` and
:func:`map_httpx_error`. They take an exception and return an ``LLMError``:
no network, no client object, no I/O — which is what makes the mapping tables
exhaustively unit-testable rather than tested through a mocked transport.
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
# 408 Request Timeout and 425 Too Early are the two 4xx codes that are NOT
# "you sent something wrong": 408 is a gateway/proxy giving up on a request it
# never finished reading, 425 is TLS early-data replay protection. Both clear on
# their own, so they must not fall into the non-retryable 4xx bucket below.
_STATUS_REQUEST_TIMEOUT = 408
_STATUS_TOO_EARLY = 425


# Whose problem is it? Answers the first question anybody asks of a spike in
# failures, and the three answers lead to three different pages.
#
#   provider      -- their side, expected to clear on its own. Wait.
#   configuration -- ours, will not clear. Fix the key, the model name, the URL.
#   contract      -- they answered, and we could not read it. Neither party is
#                    down; the wire format is not what one of us thinks it is.
#
# Deliberately coarser than the exception classes, and deliberately NOT the same
# axis as `retryable`. A timeout and a 500 are both retryable and both the
# provider's; an auth failure and a 400 are both non-retryable but only one of
# them is a credential problem. Collapsing the two axes would lose exactly the
# distinction an on-call reader needs.
FAULT_PROVIDER = "provider"
FAULT_CONFIGURATION = "configuration"
FAULT_CONTRACT = "contract"
FAULT_UNKNOWN = "unknown"


class LLMError(RuntimeError):
    """Base class for every provider failure the LLM layer surfaces.

    Subclasses ``RuntimeError`` so the pre-taxonomy contract (bare
    ``RuntimeError`` for a missing key) still holds — see the module docstring.

    ``retryable`` is a *class* attribute, not an instance one: retryability is a
    property of the failure category, so a caller can reason about it from the
    type alone and a subclass can never be constructed into disagreeing with
    its own semantics. ``fault_domain`` is a class attribute for the same
    reason, and the two together are the whole per-class table a caller needs —
    there is no second copy of it anywhere for this one to drift from.
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
        # Seconds the provider asked us to wait. None means "no guidance" — the
        # retry helper then falls back to its own backoff schedule.
        self.retry_after = retry_after
        # The original SDK/httpx exception, kept for debugging and for span
        # attributes. Deliberately not chained via `raise ... from` here; the
        # adapters do that at the raise site where the traceback is meaningful.
        self.cause = cause
        # How long the failed call took, in seconds. Filled in by the adapter
        # via with_latency() rather than by the mapper: only the adapter knows
        # when the clock started, and the mappers must stay pure.
        self.latency_s: float | None = None

    def with_latency(self, latency_s: float | None) -> "LLMError":
        """Record how long the failed attempt took and return ``self``.

        A failure has a duration too, and it is the same measurement
        :attr:`LLMResult.latency_s` records on the happy path. Without it, a
        caller wanting to time *every* attempt — which is exactly what a
        provider-call span and an operation-duration histogram want — would have
        to re-time around the adapter, dragging our own parsing and (once the
        retry helper wraps this) the backoff sleeps into the number.

        Returns ``self`` so it composes onto a mapper call in one expression at
        the raise site.
        """
        self.latency_s = latency_s
        return self


class LLMRateLimitError(LLMError):
    """Provider throttled us (HTTP 429). Retry after ``retry_after`` seconds."""

    retryable = True
    fault_domain = FAULT_PROVIDER


class LLMTimeoutError(LLMError):
    """The request did not complete in time (connect, read, write, or pool).

    Distinct from :class:`LLMTransientError` because a timeout tells us nothing
    about whether the provider *received* the request — worth calling out
    separately in traces and in the review-queue message.
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

    Split from the parent because the parent's reasoning does not reach this
    case. "A shape we don't understand is a contract problem, not a blip" is
    right about a missing ``choices`` key or a body that isn't JSON — the wire
    format genuinely disagrees, and the next request will disagree identically.
    It is wrong about a *degenerate sample*: the format was fine, the sampler
    produced nothing usable this time, and the same request very often succeeds
    on the next roll. Observed on groq/openai/gpt-oss-20b, where three of nine
    leads in one run drew an empty completion while the rest of the run, on the
    same model and the same prompt shape, drew perfectly good drafts.

    So this one is **retryable**, and the fault is the provider's rather than a
    contract mismatch neither party can fix by waiting.

    A subclass rather than a sibling, deliberately: every existing
    ``except LLMMalformedResponseError`` still catches it, so making the
    distinction costs no caller a change.
    """

    retryable = True
    fault_domain = FAULT_PROVIDER


class LLMUnexpectedError(LLMError):
    """Something that is not a provider failure, caught where one was expected.

    A ``ZeroDivisionError`` in our own prompt handling is a bug, not a blip, and
    must not be dressed up as one. This class exists so a caller can replace a
    blanket ``except Exception`` with a typed one *without* pretending the
    residual case was classified: everything downstream sees an ``LLMError``
    (uniform handling, ``retryable`` answerable from the type) while
    :attr:`failure_kind` still names the exception that actually occurred.

    Reporting the wrapper's own name instead would make every genuine bug in the
    planner indistinguishable from every other on a dashboard — which is exactly
    the thing the taxonomy was introduced to fix, reintroduced one level up.
    """

    fault_domain = FAULT_UNKNOWN

    def __init__(self, message: str, *, failure_kind: str, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.failure_kind = failure_kind


def wrap_unexpected(exc: BaseException, provider: str | None = None) -> LLMError:
    """Return ``exc`` if it is already typed, else wrap it.

    The residual half of typed error handling. ``except LLMError`` alone would
    let a bug on our side escape and sink a whole run; a bare
    ``except Exception`` alone would silently classify that bug as a provider
    failure. This gives the caller both: one exception type to handle, and no
    lost information about what really happened.

    **The message is the original's, unchanged.** It is not decorated with the
    class name, because this string ends up in ``OutreachAction.further_action``
    — prose a BD reviewer reads while deciding what to do about one lead, where
    "Unexpected RuntimeError while generating copy:" is noise. The
    classification belongs somewhere a machine reads it, and that is
    :attr:`failure_kind`, which the lead span carries as
    ``outreach.failure.kind``. Same information, told to the audience that can
    use it.
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


# Upper bound on an honoured Retry-After. Five minutes is already far longer
# than any real LLM free-tier throttle window; beyond it, waiting is worse for
# the caller than failing and letting the planner report a transient failure.
MAX_RETRY_AFTER_SECONDS = 300.0


def _parse_retry_after(headers: Any) -> float | None:
    """Read a numeric ``Retry-After`` (in seconds) out of response headers.

    RFC 9110 also allows an HTTP-date form (``Retry-After: Wed, 21 Oct 2015
    07:28:00 GMT``). No LLM provider sends it — Anthropic, OpenAI, DeepSeek and
    Groq all emit delta-seconds — so parsing it would be untestable dead code.
    A date-form header simply yields ``None`` and the caller's own backoff
    schedule takes over, which is the correct degradation.

    The value is clamped to :data:`MAX_RETRY_AFTER_SECONDS`. ``retry_after``
    flows into a ``sleep()`` in the retry helper, and ``base_url`` is operator-
    configurable — so a proxy (or a provider having a bad day) answering
    ``Retry-After: 86400000`` must not be able to park a worker for a millennium.
    Clamping in the parser means every consumer inherits the bound instead of
    each one having to remember it.
    """
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after")
    except (AttributeError, TypeError):
        # Defensive: httpx always hands us a Headers mapping, but `headers` is
        # read off the exception with getattr, so a test double or a future SDK
        # could put anything here. A bad header is never worth an exception.
        return None
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    # A negative, NaN or infinite wait is nonsense; treat it as no guidance
    # rather than letting it flow into a sleep() call.
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
        # Unenumerated 4xx (e.g. 405, 451). Client-side, so not retryable —
        # the same request would be rejected identically. 408 and 425, the two
        # exceptions to that rule, are handled above.
        return LLMBadRequestError(message, **kwargs)
    return LLMError(message, **kwargs)


# ---------------------------------------------------------------------------
# Anthropic SDK -> taxonomy
# ---------------------------------------------------------------------------


def map_anthropic_error(exc: BaseException, provider: str | None = None) -> LLMError:
    """Translate an ``anthropic`` SDK exception into an :class:`LLMError`.

    Pure: takes an exception, returns an exception. Verified against
    ``anthropic==0.109.1``.

    Ordering matters in two places and both are easy to get wrong:

    * ``APITimeoutError`` subclasses ``APIConnectionError``, so it must be
      checked first. Reversed, every timeout would silently be classified as a
      generic transient failure and we'd lose the ability to tell "the provider
      never answered" from "the provider answered 503".
    * The specific ``APIStatusError`` subclasses are checked before the base,
      with a status-code fallback for any subclass a future SDK release adds
      (``RequestTooLargeError`` — 413 — arrived exactly that way).
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

    # Timeout BEFORE connection error: APITimeoutError is a subclass of
    # APIConnectionError. Getting this backwards misclassifies every timeout.
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
        # Signalled by SDK middleware, not by the API. Rare, but its whole
        # meaning is "try again" — falling through to the non-retryable base
        # would invert it.
        return build(LLMTransientError)

    if isinstance(exc, anthropic.APIStatusError):
        # A status-carrying error the SDK models with a class we don't name
        # above. Dispatch on the code so a new SDK subclass degrades to the
        # right category instead of to the base LLMError.
        return _from_status_code(
            exc.status_code,
            message,
            provider=provider,
            retry_after=retry_after,
            cause=exc,
        )

    # Residual AnthropicError (and anything else): we know it failed, we don't
    # know enough to call it retryable. Fail closed on the safe side.
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


# How much of an error body to keep. `str(httpx.HTTPStatusError)` is a canned
# sentence naming the status code and linking MDN -- it repeats what the caller
# already has and omits the only field that says *why*, which is the body. So
# the body is appended, bounded here rather than downstream: `outreach.py` caps
# what a reviewer sees, but spans and logs read this message too, and a proxy
# answering a 502 with a full HTML page must not reach any of them whole.
_BODY_EXCERPT_MAX_CHARS = 300


def _response_detail(response: httpx.Response) -> str:
    """The provider's own explanation, bounded, or ``""`` when there isn't one.

    Every OpenAI-compatible provider puts it in ``error.message`` (Groq's
    ``tool_use_failed``, a decommissioned model, a context-length overflow);
    that field is preferred over the raw body because it is the one sentence a
    human wants. Anything else degrades to a truncated excerpt.

    Total by construction. The mappers are pure and must not raise: a streaming
    response has not been read, so ``.text`` raises ``ResponseNotRead`` on it,
    and turning a provider's error into our own traceback is the one outcome
    worse than losing the detail.
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

    Pure, like :func:`map_anthropic_error`. Covers the two failure surfaces the
    OpenAI-compatible adapters have: the HTTP call itself, and reading the
    JSON body it returned.

    ``TimeoutException`` is checked before ``TransportError``: it is a subclass,
    and the same ordering trap as Anthropic's ``APITimeoutError`` applies.
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

    # Timeout BEFORE the general TransportError — httpx.TimeoutException (and
    # its Connect/Read/Write/PoolTimeout subclasses) derive from TransportError.
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

    # Residual httpx error (TooManyRedirects, InvalidURL, ...) or anything else
    # an adapter chose to route here. Unknown retryability, so it falls back to
    # the non-retryable base rather than being retried on a guess.
    #
    # Note httpx.InvalidURL is NOT an httpx.HTTPError — it derives straight from
    # Exception — so a malformed base_url only reaches the taxonomy because the
    # adapter names it explicitly in its except clause.
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
