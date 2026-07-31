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

import hashlib
import logging
import re
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from opentelemetry import metrics as metrics_api
from opentelemetry import trace as trace_api
from opentelemetry.metrics import Histogram
from opentelemetry.trace import SpanKind, Status, StatusCode

from . import semconv as sc
from .setup import INSTRUMENTATION_NAME, INSTRUMENTATION_VERSION, get_meter, get_tracer

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from opentelemetry.trace import Span

    from ..llm.base import LLMResult

logger = logging.getLogger(__name__)

# A provider's raw stop reason is text off the wire, and this module's whole
# stance is that provider-authored text does not reach the trace backend. It is
# an enum-ish token in every SDK we ship against, so anything that is not one is
# dropped rather than truncated -- a mangled 200-character "reason" on a span is
# no more useful than an absent one, and it would be unbounded cardinality if it
# ever reached a metric.
_SAFE_FINISH_REASON = re.compile(r"\A[A-Za-z0-9_.-]{1,32}\Z")

# The agent name the run span is named after. The span-name template is
# `invoke_agent {gen_ai.agent.name}` -- NOT a bare `invoke_agent`, which is the
# easiest detail in the whole convention to get wrong because the bare form
# looks complete.
AGENT_NAME = "outreach_planner"
LEAD_SPAN_NAME = "plan_lead"

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

        ``gen_ai.operation.name`` and ``gen_ai.provider.name`` are both Required
        on **both** instruments. For a provider outside the spec's enum — a stub
        or a self-hosted endpoint — both instruments therefore emit without a
        Required attribute, and ``outreach.llm.provider`` is the join key
        instead. That is the right trade: an invented enum member would be
        accepted by every validator and wrong in every dashboard.

        Kept deliberately small — every distinct combination is a separate time
        series, so a per-lead or per-attempt attribute here would be a
        cardinality bomb.
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

    The raw fallback is the one attribute in this module carrying text off the
    provider's wire, so it is shape-checked before it is emitted (see
    :data:`_SAFE_FINISH_REASON`). Normalized reasons come from our own closed
    vocabulary and need no such check.
    """
    if result.finish_reason:
        return [result.finish_reason]
    raw = result.raw_finish_reason
    if raw and _SAFE_FINISH_REASON.match(raw):
        return [raw]
    return None


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
    # Explicitly OK, not left Unset. The spec says instrumentation SHOULD NOT
    # set Ok unless the application asserts the outcome -- and here it does: a
    # provider call that returned a readable LLMResult succeeded, full stop.
    # Phoenix also renders on status, and "two red spans and a green one" is the
    # acceptance evidence for the retry behaviour, which Unset would not show.
    span.set_status(Status(StatusCode.OK))


def _record_failure(span: "Span", exc: BaseException) -> None:
    """Mark the span failed."""
    kind = error_type(exc)
    retry_after = getattr(exc, "retry_after", None)
    attributes: dict[str, Any] = {sc.ERROR_TYPE: kind}
    # Narrowed before it is set, not after: `retry_after` is read off an
    # arbitrary exception, and a non-numeric one would otherwise reach
    # set_attribute to be logged and dropped by the SDK.
    if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool):
        attributes[sc.LLM_RETRY_AFTER_S] = retry_after
    span.set_attributes(attributes)
    # Description is the class name, never str(exc) -- see the module docstring
    # on why a provider's error prose does not go to the trace backend.
    span.set_status(Status(StatusCode.ERROR, kind))


def _record_metrics(
    call: ProviderCall,
    *,
    duration_s: float,
    result: "LLMResult | None" = None,
    exc: BaseException | None = None,
) -> None:
    """Record the two GenAI instruments for one attempt.

    **The two have different denominators, and it matters.**
    ``gen_ai.client.operation.duration`` is recorded once per **HTTP attempt**,
    so a throttled call that succeeded on its third try contributes three
    observations, not one. ``gen_ai.client.token.usage`` is recorded once per
    **logical call**, because only the attempt that succeeded has any usage to
    report.

    That is a deliberate departure from reading the spec's "per operation"
    literally, and it is the only reading that makes a rate-limited run legible:
    aggregating an attempt and its two failed predecessors into one number would
    hide exactly the thing the histogram exists to show. ``error.type`` is what
    separates the two populations — filter it out and the remaining series is
    successful-attempt latency.

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


def _safely(what: str, record: Callable[[], None]) -> None:
    """Run a recording step, swallowing anything it raises.

    **Telemetry must never be the thing that fails a provider call.** These
    steps run inside the scope's ``__exit__``, after ``acall_with_retry`` has
    already evaluated ``return result`` — so an exception here would replace a
    successful return with one the retry helper does not catch (it is not an
    ``LLMError``), and the planner would report a hard failure for a call the
    provider actually answered. On the failure path it would be worse: it would
    *replace* the ``LLMError`` and silently disable the retry.

    The SDK is forgiving (bad attribute types and duplicate instrument
    registrations are logged, not raised), so this should never fire. It is
    three lines against a failure mode whose name is "observability took down
    the planner", and it will run eight-wide once MUS-26 lands.
    """
    try:
        record()
    except Exception:
        logger.debug("GenAI telemetry: %s failed", what, exc_info=True)


# ---------------------------------------------------------------------------
# run / lead ambient state
# ---------------------------------------------------------------------------
#
# The run span wants totals (tokens over the whole run) and the lead span wants
# a count (HTTP attempts for that lead), and neither is known where the span is
# opened -- only the provider-call scope has those numbers, several frames down.
#
# ContextVars rather than an extra argument threaded through the planner, for
# one reason that decides it: `asyncio.gather` copies the current context into
# every task it creates, so the lead-scoped counter is automatically per-task
# once MUS-26 makes phase 3 concurrent, with no change here and no change at the
# call site. Passing a recorder down by hand would work today and would have to
# be re-plumbed then.
#
# Note what is copied is the *binding*, not the object: mutating the recorder a
# task inherited is visible to the run that created it, which is exactly the
# aggregation behaviour wanted. The lock is there because `asyncio.to_thread`
# and a threaded server can both put two real threads on one recorder.

_current_run: ContextVar["RunRecorder | None"] = ContextVar("outreach_run", default=None)
_current_lead: ContextVar["LeadSpan | None"] = ContextVar("outreach_lead", default=None)


def sha256_of(text: str | None) -> str | None:
    """Hex digest of ``text``, or ``None`` when there is nothing to digest.

    This is the *only* thing a span ever learns about prompt or completion
    content. It answers "is this the same text the row holds?" and "did two runs
    generate identical copy?" without the trace backend ever holding a lead's
    HubSpot notes or an outreach email.
    """
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify_outcome(
    *,
    generated: bool,
    skipped: bool,
    shape_problem_count: int,
    violation_count: int,
) -> str:
    """Classify a lead's output-gate result into one of six states.

    The distinction that earns its keep is ``skipped`` versus ``not_generated``:
    the first is a decision (no automated pattern matched this lead, so no copy
    was ever requested) and the second is a failure (copy was requested and the
    provider call did not produce any). Collapsing them would make a provider
    outage look like a quiet week.
    """
    if skipped:
        return sc.VERIFY_SKIPPED
    if not generated:
        return sc.VERIFY_NOT_GENERATED
    if shape_problem_count and violation_count:
        return sc.VERIFY_BOTH_FAILED
    if shape_problem_count:
        return sc.VERIFY_SHAPE_FAILED
    if violation_count:
        return sc.VERIFY_GROUNDING_FAILED
    return sc.VERIFY_PASS


@dataclass
class RunRecorder:
    """Totals accumulated over one planner run, written to the run span at the end.

    Also owns the run's lead spans. A lead span outlives a single ``with``
    block by design (see :class:`LeadSpan`), which means an exception between
    the two phases could otherwise leave one open forever — a span that never
    ends is never exported, so the symptom is a lead silently missing from the
    trace rather than an error. Having the run own them makes that
    structurally impossible instead of a thing to remember.
    """

    run_id: str
    span: "Span"
    tracer: Any = None
    input_tokens: int = 0
    output_tokens: int = 0
    saw_usage: bool = False
    leads: list["LeadSpan"] = field(default_factory=list, repr=False)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def start_lead(
        self,
        *,
        lead_id: str,
        action_type: str,
        priority: int,
        prompt: str | None = None,
    ) -> "LeadSpan":
        """Open a lead span belonging to this run."""
        lead = start_lead_span(
            run_id=self.run_id,
            lead_id=lead_id,
            action_type=action_type,
            priority=priority,
            prompt=prompt,
            tracer=self.tracer,
        )
        with self._lock:
            self.leads.append(lead)
        return lead

    def _close_orphans(self, exc: BaseException | None) -> None:
        """End any lead span the planner did not finish."""
        with self._lock:
            leads = list(self.leads)
        for lead in leads:
            lead.abandon(exc)

    def add_usage(self, input_tokens: int | None, output_tokens: int | None) -> None:
        """Fold one provider call's usage into the run total."""
        if input_tokens is None and output_tokens is None:
            return
        with self._lock:
            self.input_tokens += input_tokens or 0
            self.output_tokens += output_tokens or 0
            # Tracked separately so a run in which no provider reported usage
            # emits no usage attributes at all, rather than a confident zero.
            self.saw_usage = True

    def finish(self, *, lead_count: int, needs_human_count: int) -> None:
        attributes: dict[str, Any] = {
            sc.LEAD_COUNT: lead_count,
            sc.NEEDS_HUMAN_COUNT: needs_human_count,
        }
        if self.saw_usage:
            attributes[sc.GEN_AI_USAGE_INPUT_TOKENS] = self.input_tokens
            attributes[sc.GEN_AI_USAGE_OUTPUT_TOKENS] = self.output_tokens
            attributes[sc.LLM_TOKEN_COUNT_PROMPT] = self.input_tokens
            attributes[sc.LLM_TOKEN_COUNT_COMPLETION] = self.output_tokens
            attributes[sc.LLM_TOKEN_COUNT_TOTAL] = self.input_tokens + self.output_tokens
        self.span.set_attributes(attributes)


@contextmanager
def run_span(
    *,
    verify_level: str,
    max_in_flight: int,
    run_id: str | None = None,
    tracer: Any = None,
) -> Iterator[RunRecorder]:
    """The whole planner run: ``invoke_agent outreach_planner``, kind INTERNAL.

    INTERNAL rather than CLIENT because ours is the *internal* variant of the
    operation — the agent runs in this process, like the framework examples in
    the spec. A CLIENT span would claim we called an agent over the network.

    Yields the recorder the planner reports totals to. ``run_id`` is generated
    here unless supplied, and is the same value written to
    ``OutreachAction.trace_run_id``, which is what lets a row found in the
    database be traced back to the run that produced it.
    """
    active = tracer if tracer is not None else get_tracer()
    resolved_run_id = run_id or str(uuid.uuid4())
    with active.start_as_current_span(
        f"{sc.OPERATION_INVOKE_AGENT} {AGENT_NAME}",
        kind=SpanKind.INTERNAL,
        record_exception=False,
        set_status_on_exception=False,
        attributes={
            sc.GEN_AI_OPERATION_NAME: sc.OPERATION_INVOKE_AGENT,
            sc.GEN_AI_AGENT_NAME: AGENT_NAME,
            sc.RUN_ID: resolved_run_id,
            sc.VERIFY_LEVEL: verify_level,
            sc.CONCURRENCY_MAX_IN_FLIGHT: max_in_flight,
            sc.OPENINFERENCE_SPAN_KIND: sc.OPENINFERENCE_KIND_AGENT,
        },
    ) as span:
        recorder = RunRecorder(run_id=resolved_run_id, span=span, tracer=tracer)
        token = _current_run.set(recorder)
        try:
            yield recorder
        except BaseException as exc:
            span.set_attribute(sc.ERROR_TYPE, error_type(exc))
            span.set_status(Status(StatusCode.ERROR, error_type(exc)))
            recorder._close_orphans(exc)
            raise
        else:
            recorder._close_orphans(None)
        finally:
            _current_run.reset(token)


class LeadSpan:
    """One lead's span, which deliberately outlives a single ``with`` block.

    A lead's work is split across two planner phases — the provider call, then
    the two output gates — and the span has to cover both, because "how long did
    this lead take" is the question it exists to answer. So it is started once
    and ended once, with :meth:`active` making it the current span for each
    phase in between.

    The alternative, holding every lead span open until the run ends, is the
    thing to avoid: at concurrency 8 over 200 leads the lead processed first
    would report a 45-second duration, and the per-lead latency signal — the
    entire point — would be gone.

    ``plan_lead`` carries no ``gen_ai.*`` attributes. It is neither a model call
    nor an agent invocation, the conventions define nothing for it, and
    inventing keys in a namespace someone else owns is how you collide with a
    future release that means something different by them.
    """

    __slots__ = ("_span", "_attempts", "_lock", "_ended")

    def __init__(self, span: "Span") -> None:
        self._span = span
        self._attempts = 0
        self._lock = Lock()
        self._ended = False

    def note_attempt(self) -> None:
        """Count one HTTP attempt made for this lead."""
        with self._lock:
            self._attempts += 1

    @contextmanager
    def active(self) -> Iterator["LeadSpan"]:
        """Make this the current span for a phase, without ending it.

        ``end_on_exit=False`` is the whole point: leaving this block means "this
        phase is done", not "this lead is done".
        """
        with trace_api.use_span(
            self._span,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        ):
            token = _current_lead.set(self)
            try:
                yield self
            finally:
                _current_lead.reset(token)

    def finish(
        self,
        *,
        needs_human: bool,
        outcome: str,
        violation_count: int = 0,
        shape_problem_count: int = 0,
        output_ref: str | None = None,
        output_sha256: str | None = None,
        failure: BaseException | None = None,
    ) -> None:
        """Record the verdict and end the span. Safe to call twice."""
        with self._lock:
            if self._ended:
                return
            self._ended = True
            attempts = self._attempts

        attributes: dict[str, Any] = {
            sc.NEEDS_HUMAN: needs_human,
            sc.VERIFY_OUTCOME: outcome,
            sc.VERIFY_VIOLATION_COUNT: violation_count,
            sc.SHAPE_PROBLEM_COUNT: shape_problem_count,
            sc.LLM_ATTEMPTS: attempts,
        }
        _set_if(attributes, sc.OUTPUT_REF, output_ref)
        _set_if(attributes, sc.OUTPUT_SHA256, output_sha256)
        if failure is not None:
            attributes[sc.FAILURE_KIND] = error_type(failure)
        self._span.set_attributes(attributes)
        if failure is not None:
            # ERROR, not OK: this lead did not produce usable copy. The run as a
            # whole may still be a success -- one dead API call must not sink it
            # -- which is exactly why the status lives on the lead span.
            self._span.set_status(Status(StatusCode.ERROR, error_type(failure)))
        self._span.end()

    def abandon(self, exc: BaseException | None = None) -> None:
        """End the span without a verdict, because the run did not reach one.

        A no-op once :meth:`finish` has run. An unended span is never exported,
        so without this a lead caught by an escaping exception would simply
        vanish from the trace — the worst possible failure mode for the thing
        you are looking at the trace to explain.
        """
        with self._lock:
            if self._ended:
                return
            self._ended = True
        self._span.set_attribute(sc.VERIFY_OUTCOME, sc.VERIFY_NOT_GENERATED)
        if exc is not None:
            self._span.set_attribute(sc.FAILURE_KIND, error_type(exc))
            self._span.set_status(Status(StatusCode.ERROR, error_type(exc)))
        self._span.end()


def start_lead_span(
    *,
    run_id: str,
    lead_id: str,
    action_type: str,
    priority: int,
    prompt: str | None = None,
    tracer: Any = None,
) -> LeadSpan:
    """Open a lead's span. The caller owns ending it via :meth:`LeadSpan.finish`.

    ``prompt`` is hashed, never recorded. The prompt embeds the lead's HubSpot
    notes; ``outreach.input.ref`` plus ``outreach.input.sha256`` say *which*
    record was read and *that* this is the text, which is everything a trace
    needs and nothing a trace backend should hold.
    """
    active = tracer if tracer is not None else get_tracer()
    attributes: dict[str, Any] = {
        sc.LEAD_ID: lead_id,
        sc.ACTION_TYPE: action_type,
        sc.ACTION_PRIORITY: priority,
        sc.RUN_ID: run_id,
        sc.INPUT_REF: f"lead:{lead_id}",
        sc.OPENINFERENCE_SPAN_KIND: sc.OPENINFERENCE_KIND_CHAIN,
    }
    _set_if(attributes, sc.INPUT_SHA256, sha256_of(prompt))
    span = active.start_span(LEAD_SPAN_NAME, kind=SpanKind.INTERNAL, attributes=attributes)
    return LeadSpan(span)


def finish_lead(
    lead: LeadSpan,
    *,
    run_id: str,
    lead_id: str,
    skipped: bool,
    generated: bool,
    needs_human: bool,
    shape_problem_count: int = 0,
    violation_count: int = 0,
    output_text: str = "",
    failure: BaseException | None = None,
) -> None:
    """Close a lead span from the facts the planner already has.

    Everything derived — the six-state outcome, the output reference, the
    digest — is derived *here*, so the planner's call site is a list of facts
    about the lead and contains no telemetry decisions of its own. That is what
    keeps the wiring in ``outreach.py`` to statements a rebase can move without
    thinking about.
    """
    lead.finish(
        needs_human=needs_human,
        outcome=verify_outcome(
            generated=generated,
            skipped=skipped,
            shape_problem_count=shape_problem_count,
            violation_count=violation_count,
        ),
        violation_count=violation_count,
        shape_problem_count=shape_problem_count,
        output_ref=output_ref(run_id, lead_id) if generated else None,
        output_sha256=sha256_of(output_text),
        failure=failure,
    )


def output_ref(run_id: str, lead_id: str) -> str:
    """Reference to the row this lead's work will produce.

    Resolvable via ``OutreachAction.objects.get(trace_run_id=..., lead_id=...)``
    and — the point — knowable *before* the row exists, so the lead span can
    close on time instead of waiting for the run's single write.
    """
    return f"outreach_action:{run_id}:{lead_id}"


def _add_run_usage(result: "LLMResult") -> None:
    """Fold one call's usage into the enclosing run, if there is one."""
    run = _current_run.get()
    if run is not None:
        run.add_usage(result.input_tokens, result.output_tokens)


AttemptScope = Callable[[int], AbstractContextManager[Callable[["LLMResult"], None]]]


def provider_call_scope(
    call: ProviderCall, *, tracer: "trace_api.Tracer | None" = None
) -> AttemptScope:
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
            lead = _current_lead.get()
            if lead is not None:
                lead.note_attempt()
            try:
                yield recorded.append
            except BaseException as exc:
                # BaseException, not Exception, and the reason is asyncio:
                # `CancelledError` derives from BaseException, and cancelling a
                # sibling task mid-flight is a normal event once MUS-26 runs
                # eight leads concurrently. Narrowing this to Exception would
                # leave those spans open -- and an unended span is never
                # exported, so cancelled leads would vanish from the trace
                # rather than show as cancelled.
                #
                # The failure's own latency, measured by the adapter, beats our
                # wall clock: it excludes our parsing and error mapping. It is
                # absent only for a caller-constructed error.
                # Bound to a local because `except ... as exc` unbinds `exc` at
                # the end of the block, and the closures below outlive the name.
                failure = exc
                latency = getattr(failure, "latency_s", None)
                duration = (
                    latency if isinstance(latency, (int, float)) else time.perf_counter() - started
                )
                _safely("failure attributes", lambda: _record_failure(span, failure))
                _safely(
                    "failure metrics",
                    lambda: _record_metrics(call, duration_s=duration, exc=failure),
                )
                raise
            else:
                # `recorded` is empty when the caller opted out of reporting a
                # result (nullcontext semantics). The span still closes green;
                # it simply has no response-side attributes.
                result = recorded[-1] if recorded else None
                # Duration is recorded whatever happened -- the attempt took as
                # long as it took, and a caller declining to report a result is
                # not a reason for the Required latency instrument to under-count.
                duration = (
                    result.latency_s
                    if result is not None and result.latency_s is not None
                    else time.perf_counter() - started
                )
                if result is not None:
                    _safely("success attributes", lambda: _record_success(span, call, result))
                _safely(
                    "success metrics",
                    lambda: _record_metrics(call, duration_s=duration, result=result),
                )
                if result is not None:
                    # Folded into the run's totals through the ambient recorder
                    # -- guarded like the rest, so a telemetry fault here cannot
                    # fail a call the provider answered.
                    _safely("run usage", lambda: _add_run_usage(result))

    return scope


def _reset_instruments_for_tests() -> None:
    """Drop the cached instruments. Test-support only."""
    global _instruments, _instruments_provider
    with _instruments_lock:
        _instruments = None
        _instruments_provider = None


__all__ = [
    "AGENT_NAME",
    "INSTRUMENTATION_NAME",
    "INSTRUMENTATION_VERSION",
    "LEAD_SPAN_NAME",
    "PROVIDER_NAMES",
    "LeadSpan",
    "ProviderCall",
    "RunRecorder",
    "error_type",
    "finish_lead",
    "instruments",
    "output_ref",
    "provider_call_scope",
    "run_span",
    "sha256_of",
    "start_lead_span",
    "verify_outcome",
]
