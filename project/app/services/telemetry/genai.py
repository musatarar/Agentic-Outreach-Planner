"""GenAI spans and metrics for the provider call (MUS-25, 25-b).

**Where this hooks in, and why there.** :func:`provider_call_scope` produces an
``attempt_scope`` for :func:`project.app.services.llm.retry.acall_with_retry` —
one context manager per *HTTP attempt*. Not inside the adapters, which would
mean two copies (Anthropic SDK, httpx) that can drift; and not around the retry
helper as a whole, which would collapse a rate-limited call into one long span
and hide the very thing worth seeing.

One span per attempt is what makes a throttled run legible. Three attempts under
one lead render as three CLIENT spans, the first two red, the third green — the
retry policy visible as a picture rather than as a log line. It is also the only
placement where the *duration* is honest: the retry helper deliberately leaves
its backoff sleep outside the scope, so an attempt's span covers the provider
call and nothing else.

**This module knows nothing about the LLM layer at runtime.** ``LLMResult`` is
imported only under ``TYPE_CHECKING``; everything read off a result or an
exception is read by attribute. That is the mirror image of ``llm/retry.py``
importing nothing telemetry-shaped, and it keeps the dependency edge in exactly
one direction: the planner wires the two together, neither knows the other.

**Two namespaces, every span.** Self-hosted Phoenix speaks OpenInference, not
the OTel GenAI conventions (Arize-ai/phoenix#10622), so a span carrying only
``gen_ai.*`` renders as an unlabelled grey bar with empty model and token panes.
Every attribute that has an OpenInference equivalent is therefore emitted twice.
The two carriers that are **never** emitted are ``llm.input_messages`` and
``llm.output_messages`` — OpenInference's prompt and completion fields, which
would put a lead's HubSpot notes and the generated email into the trace backend
and defeat the whole content-reference policy. See
:data:`project.app.services.telemetry.semconv.FORBIDDEN_CONTENT_KEYS`.

**Provider error messages are not recorded either.** The span is opened with
``record_exception=False`` and the status description is the exception's class
name. A provider's error string is text we did not write, produced in response
to a prompt built from lead data, and some providers echo request content back
in a 4xx body. The full message still reaches the reviewer via
``further_action`` and the application log, which are not the trace backend.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from opentelemetry.metrics import Histogram
from opentelemetry.trace import SpanKind, Status, StatusCode

from . import semconv as sc
from .setup import INSTRUMENTATION_NAME, INSTRUMENTATION_VERSION, get_meter, get_tracer

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from opentelemetry.trace import Span

    from ..llm.base import LLMResult

# Our configured provider name -> the spec's `gen_ai.provider.name` enum member.
# Total over the four providers this app ships. A name that is not a key here
# simply gets no `gen_ai.provider.name`: the attribute is an enum, and inventing
# a member for a stub or self-hosted provider would make the span invalid in a
# way no validator would catch but every dashboard would.
PROVIDER_NAMES = {
    "claude": sc.PROVIDER_ANTHROPIC,
    "chatgpt": sc.PROVIDER_OPENAI,
    "groq": sc.PROVIDER_GROQ,
    "deepseek": sc.PROVIDER_DEEPSEEK,
}

_instruments_lock = Lock()
_instruments: "_Instruments | None" = None
_instruments_provider: object = None


@dataclass(frozen=True, slots=True)
class _Instruments:
    duration: Histogram
    tokens: Histogram


def instruments() -> _Instruments:
    """The two GenAI histograms, created once per ``MeterProvider``.

    Cached because ``create_histogram`` is not free and this is called on every
    attempt; keyed on the provider identity because tests install a real
    ``MeterProvider`` *after* this module is imported, and instruments bound to
    the no-op provider it replaced would record into nothing forever.
    """
    global _instruments, _instruments_provider

    from opentelemetry import metrics as metrics_api

    provider = metrics_api.get_meter_provider()
    with _instruments_lock:
        if _instruments is None or _instruments_provider is not provider:
            meter = get_meter()
            _instruments = _Instruments(
                duration=meter.create_histogram(
                    name=sc.METRIC_OPERATION_DURATION,
                    unit=sc.METRIC_OPERATION_DURATION_UNIT,
                    description="Duration of a GenAI client operation.",
                ),
                tokens=meter.create_histogram(
                    name=sc.METRIC_TOKEN_USAGE,
                    unit=sc.METRIC_TOKEN_USAGE_UNIT,
                    description="Number of input and output tokens used.",
                ),
            )
            _instruments_provider = provider
        return _instruments


def error_type(exc: BaseException) -> str:
    """The ``error.type`` value for an exception.

    The spec allows either a fully-qualified class name or a low-cardinality
    string. The bare class name is chosen: ``LLMRateLimitError`` is the same
    cardinality as its dotted path and is the label a human reads on a chart.
    Crucially it is *not* the exception's message — that is provider-authored
    text of unbounded cardinality, and putting it here would both blow up a
    metric's attribute set and route provider prose into the trace backend.
    """
    return type(exc).__qualname__


@dataclass(frozen=True, slots=True)
class ProviderCall:
    """Everything known about a provider call *before* it is made.

    Built once per lead and shared by every attempt for that lead, so the
    request-side attributes cannot disagree between a failed attempt and the
    retry that succeeded.
    """

    provider: str
    model: str
    max_tokens: int | None = None
    base_url: str | None = None

    @classmethod
    def from_client(cls, client: Any, max_tokens: int | None = None) -> "ProviderCall":
        """Read a call description off a resolved ``LLMClient``.

        ``base_url`` is read only when the adapter publishes one — the
        OpenAI-compatible adapters do, ``ClaudeClient`` keeps the Anthropic SDK's
        own client private. Rather than hardcode ``api.anthropic.com`` (which
        ``ANTHROPIC_BASE_URL`` can override, making the attribute a confident
        lie), the Anthropic branch simply omits ``server.address``. It is a
        Recommended attribute, and an absent one is strictly better than a wrong
        one.
        """
        return cls(
            provider=getattr(client, "provider_name", "") or "",
            model=getattr(client, "model", "") or "",
            max_tokens=max_tokens
            if max_tokens is not None
            else getattr(client, "default_max_tokens", None),
            base_url=getattr(client, "base_url", None),
        )

    @property
    def gen_ai_provider(self) -> str | None:
        """The spec enum member for this provider, or ``None`` if it has none."""
        return PROVIDER_NAMES.get(self.provider)

    @property
    def span_name(self) -> str:
        """``chat {gen_ai.request.model}``, per the spec's naming template."""
        return f"{sc.OPERATION_CHAT} {self.model}".strip()

    def metric_attributes(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Attribute set shared by both histograms.

        ``gen_ai.operation.name`` is Required on both; ``gen_ai.provider.name``
        is Required on token.usage. Kept deliberately small — every distinct
        combination is a separate time series, so a per-lead or per-attempt
        attribute here would be a cardinality bomb.
        """
        attributes: dict[str, Any] = {
            sc.GEN_AI_OPERATION_NAME: sc.OPERATION_CHAT,
            sc.LLM_PROVIDER_CONFIGURED: self.provider,
        }
        _set_if(attributes, sc.GEN_AI_PROVIDER_NAME, self.gen_ai_provider)
        _set_if(attributes, sc.GEN_AI_REQUEST_MODEL, self.model or None)
        for key, value in (extra or {}).items():
            _set_if(attributes, key, value)
        return attributes


def _set_if(target: dict[str, Any], key: str, value: Any) -> None:
    """Set ``key`` only when ``value`` is present.

    ``None`` is not a legal attribute value and, more to the point, an absent
    attribute and one set to a placeholder mean different things: a provider
    that reported no token usage must not be indistinguishable from one that
    reported zero.
    """
    if value is not None:
        target[key] = value


def _server_attributes(base_url: str | None) -> dict[str, Any]:
    """``server.address`` / ``server.port`` parsed out of a base URL.

    Only the host and an *explicit* port are taken. Synthesising 443 for an
    https URL that did not name one would make ``server.port`` a fact about our
    parser rather than about the request.
    """
    if not base_url:
        return {}
    parsed = urlsplit(base_url)
    attributes: dict[str, Any] = {}
    _set_if(attributes, sc.SERVER_ADDRESS, parsed.hostname)
    try:
        port = parsed.port
    except ValueError:
        # A malformed authority ("host:notaport"). Telemetry must never be the
        # thing that raises out of a provider call.
        port = None
    _set_if(attributes, sc.SERVER_PORT, port)
    return attributes


def _set_request_attributes(span: "Span", call: ProviderCall, attempt: int) -> None:
    """Everything knowable before the provider answers."""
    attributes: dict[str, Any] = {
        sc.GEN_AI_OPERATION_NAME: sc.OPERATION_CHAT,
        # Always emitted, even when gen_ai.provider.name is too: the spec's enum
        # has no member for a stub or self-hosted provider, and the join back to
        # an LLMConfiguration row must not depend on enum membership.
        sc.LLM_PROVIDER_CONFIGURED: call.provider,
        # 1-based. "attempt 0" reads as "no attempt" to everyone who has not
        # read the retry helper's signature.
        sc.LLM_ATTEMPT: attempt + 1,
        sc.OPENINFERENCE_SPAN_KIND: sc.OPENINFERENCE_KIND_LLM,
    }
    _set_if(attributes, sc.GEN_AI_PROVIDER_NAME, call.gen_ai_provider)
    _set_if(attributes, sc.GEN_AI_REQUEST_MODEL, call.model or None)
    _set_if(attributes, sc.GEN_AI_REQUEST_MAX_TOKENS, call.max_tokens)
    _set_if(attributes, sc.LLM_MODEL_NAME, call.model or None)
    _set_if(attributes, sc.LLM_PROVIDER, call.gen_ai_provider or call.provider or None)
    attributes.update(_server_attributes(call.base_url))
    span.set_attributes(attributes)


def _finish_reasons(result: "LLMResult") -> list[str] | None:
    """``gen_ai.response.finish_reasons`` — an ARRAY of strings, always.

    The plural is the spec's: a response with several choices has several
    reasons. We only ever request one completion, so the array has one element,
    but emitting a bare string here would be a technically-invalid span that
    still looks right in every UI, which is the worst kind of wrong.

    The normalized reason is preferred over the provider's raw one so a chart
    aggregating across providers is not split between ``end_turn`` and ``stop``;
    the raw value is used when normalization declined to guess, because a
    provider's own word beats nothing.
    """
    reason = result.finish_reason or result.raw_finish_reason
    return [reason] if reason else None


def _record_success(span: "Span", call: ProviderCall, result: "LLMResult") -> None:
    attributes: dict[str, Any] = {}
    _set_if(attributes, sc.GEN_AI_RESPONSE_MODEL, result.response_model)
    _set_if(attributes, sc.GEN_AI_RESPONSE_FINISH_REASONS, _finish_reasons(result))
    _set_if(attributes, sc.GEN_AI_USAGE_INPUT_TOKENS, result.input_tokens)
    _set_if(attributes, sc.GEN_AI_USAGE_OUTPUT_TOKENS, result.output_tokens)

    # OpenInference twins, so Phoenix's token pane is populated.
    _set_if(attributes, sc.LLM_TOKEN_COUNT_PROMPT, result.input_tokens)
    _set_if(attributes, sc.LLM_TOKEN_COUNT_COMPLETION, result.output_tokens)
    if result.input_tokens is not None and result.output_tokens is not None:
        # Only when BOTH are known. A "total" that is silently just the prompt
        # count is worse than no total at all.
        attributes[sc.LLM_TOKEN_COUNT_TOTAL] = result.input_tokens + result.output_tokens
    span.set_attributes(attributes)
    span.set_status(Status(StatusCode.OK))


def _record_failure(span: "Span", exc: BaseException) -> float | None:
    """Mark the span failed. Returns the provider's ``Retry-After``, if any."""
    kind = error_type(exc)
    retry_after = getattr(exc, "retry_after", None)
    span.set_attribute(sc.ERROR_TYPE, kind)
    _set_if_span(span, sc.LLM_RETRY_AFTER_S, retry_after)
    # Description is the class name, never str(exc) -- see the module docstring
    # on why a provider's error prose does not go to the trace backend.
    span.set_status(Status(StatusCode.ERROR, kind))
    return retry_after if isinstance(retry_after, (int, float)) else None


def _set_if_span(span: "Span", key: str, value: Any) -> None:
    if value is not None:
        span.set_attribute(key, value)


def _record_metrics(
    call: ProviderCall,
    *,
    duration_s: float,
    result: "LLMResult | None" = None,
    exc: BaseException | None = None,
) -> None:
    """One duration point per attempt; two token points per successful call.

    Token usage is recorded **only** when the provider reported it. Recording a
    zero for a provider that simply omitted ``usage`` would poison the histogram
    with observations of "this call cost nothing", which is a claim we cannot
    make and which no later query could distinguish from the real thing.
    """
    kit = instruments()
    kit.duration.record(
        duration_s,
        call.metric_attributes({sc.ERROR_TYPE: error_type(exc) if exc is not None else None}),
    )

    if result is None:
        return
    for token_type, count in (
        (sc.TOKEN_TYPE_INPUT, result.input_tokens),
        (sc.TOKEN_TYPE_OUTPUT, result.output_tokens),
    ):
        if count is None:
            continue
        kit.tokens.record(count, call.metric_attributes({sc.GEN_AI_TOKEN_TYPE: token_type}))


AttemptScope = Callable[[int], Any]


def provider_call_scope(call: ProviderCall, *, tracer: Any = None) -> AttemptScope:
    """Build an ``attempt_scope`` for ``acall_with_retry``.

    Usage — the whole integration, at the one call site that retries::

        result = await acall_with_retry(
            lambda: client.agenerate(prompt, max_tokens=n),
            attempt_scope=provider_call_scope(ProviderCall.from_client(client, n)),
        )

    The returned callable takes a 0-based attempt index and yields a context
    manager. Entering it opens the CLIENT span; the value it yields is the
    retry helper's ``record_result`` hook, called with the ``LLMResult`` while
    the span is still open. Leaving it closes the span either way — including on
    a cancellation, which is why the retry helper's seam is a scope and not a
    pair of callbacks.

    ``tracer`` is injectable so a test can drive the same code against a
    ``NoOpTracerProvider`` and prove the no-telemetry path really is the same
    path. Production passes nothing.
    """

    @contextmanager
    def scope(attempt: int) -> Iterator[Callable[["LLMResult"], None]]:
        # Resolved per attempt, not once when the factory is built: the provider
        # may be installed between the two, and a tracer captured too early
        # would be a no-op for the life of the process.
        active = tracer if tracer is not None else get_tracer()
        recorded: list[LLMResult] = []
        started = time.perf_counter()
        with active.start_as_current_span(
            call.span_name,
            kind=SpanKind.CLIENT,
            # Both off on purpose. record_exception=True would attach the
            # provider's error message as a span event; set_status_on_exception
            # would give the status a description built from the same string.
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            _set_request_attributes(span, call, attempt)
            try:
                yield recorded.append
            except BaseException as exc:
                _record_failure(span, exc)
                # The failure's own latency, measured by the adapter, beats our
                # wall clock: it excludes our parsing and error mapping. It is
                # absent only for a caller-constructed error.
                latency = getattr(exc, "latency_s", None)
                _record_metrics(
                    call,
                    duration_s=latency
                    if isinstance(latency, (int, float))
                    else time.perf_counter() - started,
                    exc=exc,
                )
                raise
            else:
                # `recorded` is empty when the caller opted out of reporting a
                # result (nullcontext semantics). The span still closes green;
                # it simply has no response-side attributes.
                result = recorded[-1] if recorded else None
                if result is not None:
                    _record_success(span, call, result)
                    _record_metrics(
                        call,
                        duration_s=result.latency_s
                        if result.latency_s is not None
                        else time.perf_counter() - started,
                        result=result,
                    )

    return scope


def _reset_instruments_for_tests() -> None:
    """Drop the cached instruments. Test-support only."""
    global _instruments, _instruments_provider
    with _instruments_lock:
        _instruments = None
        _instruments_provider = None


__all__ = [
    "INSTRUMENTATION_NAME",
    "INSTRUMENTATION_VERSION",
    "PROVIDER_NAMES",
    "ProviderCall",
    "error_type",
    "instruments",
    "provider_call_scope",
]
