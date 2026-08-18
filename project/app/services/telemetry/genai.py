"""GenAI spans and metrics for the provider call (MUS-25, 25-b).

One CLIENT span per HTTP attempt (via :func:`provider_call_scope`); attributes
dual-emitted under ``gen_ai.*`` and OpenInference names for Phoenix; and no
provider-authored text — prompts, completions, error messages — ever reaches
the trace backend (see :data:`semconv.FORBIDDEN_CONTENT_KEYS`).
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

# Raw stop reasons are provider text off the wire; anything not enum-shaped is
# dropped rather than truncated.
_SAFE_FINISH_REASON = re.compile(r"\A[A-Za-z0-9_.-]{1,32}\Z")

# The run span's name template is `invoke_agent {gen_ai.agent.name}`, NOT a
# bare `invoke_agent`.
AGENT_NAME = "outreach_planner"
LEAD_SPAN_NAME = "plan_lead"

# Must match OutreachAction.trace_run_id's max_length (a uuid4 string is 36).
RUN_ID_MAX_LENGTH = 36

# Our configured provider name -> the spec's `gen_ai.provider.name` enum
# member. Names outside the map simply omit the attribute; the enum is closed.
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
    """The two GenAI histograms, cached per ``MeterProvider`` identity —
    tests install a real provider after import, and instruments bound to the
    replaced no-op one would record into nothing forever.
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
    """The ``error.type`` value for an exception: the bare class name, or its
    ``failure_kind`` override (``LLMUnexpectedError``) — never the message,
    which is provider-authored text of unbounded cardinality.
    """
    override = getattr(exc, "failure_kind", None)
    if isinstance(override, str) and override:
        return override
    return type(exc).__qualname__


def fault_domain(exc: BaseException) -> str:
    """Whose problem this failure is: ``provider``, ``configuration``,
    ``contract`` or ``unknown``. Read off the exception class, where
    ``llm/errors.py`` declares it; undeclared means ``unknown``, not assumed.
    """
    declared = getattr(exc, "fault_domain", None)
    return declared if isinstance(declared, str) and declared else "unknown"


@dataclass(frozen=True, slots=True)
class ProviderCall:
    """Everything known about a provider call *before* it is made; built once
    per lead and shared by every attempt, so request-side attributes agree.
    """

    provider: str
    model: str
    max_tokens: int | None = None
    base_url: str | None = None

    @classmethod
    def from_client(cls, client: Any, max_tokens: int | None = None) -> "ProviderCall":
        """Read a call description off a resolved ``LLMClient``.

        Adapters that publish no ``base_url`` (Anthropic) simply omit
        ``server.address`` — absent beats wrong.
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
        """Attribute set shared by both histograms; kept small — every distinct
        combination is a separate time series. ``outreach.llm.provider`` is the
        join key for providers outside the spec's enum.
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
    """Set ``key`` only when ``value`` is present — absent and zero (or a
    placeholder) mean different things.
    """
    if value is not None:
        target[key] = value


def _server_attributes(base_url: str | None) -> dict[str, Any]:
    """``server.address`` / ``server.port`` parsed out of a base URL — host
    and *explicit* port only, 443 is never synthesised.
    """
    if not base_url:
        return {}
    parsed = urlsplit(base_url)
    attributes: dict[str, Any] = {}
    _set_if(attributes, sc.SERVER_ADDRESS, parsed.hostname)
    try:
        port = parsed.port
    except ValueError:
        # Malformed authority; telemetry must never raise out of a provider call.
        port = None
    _set_if(attributes, sc.SERVER_PORT, port)
    return attributes


def _set_request_attributes(span: "Span", call: ProviderCall, attempt: int) -> None:
    """Everything knowable before the provider answers."""
    attributes: dict[str, Any] = {
        sc.GEN_AI_OPERATION_NAME: sc.OPERATION_CHAT,
        # Always emitted: the LLMConfiguration join must not depend on the
        # gen_ai.provider.name enum having a member for this provider.
        sc.LLM_PROVIDER_CONFIGURED: call.provider,
        # 1-based: "attempt 0" reads as "no attempt".
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

    Normalized reason preferred (cross-provider charts); the raw provider value
    is a fallback and, being wire text, is shape-checked first.
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
        # Only when BOTH are known.
        attributes[sc.LLM_TOKEN_COUNT_TOTAL] = result.input_tokens + result.output_tokens
    span.set_attributes(attributes)
    # Explicitly OK, not Unset: the application asserts the outcome here, and
    # Phoenix renders on status.
    span.set_status(Status(StatusCode.OK))


def _record_failure(span: "Span", exc: BaseException) -> None:
    """Mark the span failed."""
    kind = error_type(exc)
    retry_after = getattr(exc, "retry_after", None)
    attributes: dict[str, Any] = {sc.ERROR_TYPE: kind}
    # Narrowed before it is set: `retry_after` comes off an arbitrary exception.
    if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool):
        attributes[sc.LLM_RETRY_AFTER_S] = retry_after
    span.set_attributes(attributes)
    # Description is the class name, never str(exc) -- provider error prose
    # stays out of the trace backend.
    span.set_status(Status(StatusCode.ERROR, kind))


def _record_metrics(
    call: ProviderCall,
    *,
    duration_s: float,
    result: "LLMResult | None" = None,
    exc: BaseException | None = None,
) -> None:
    """Record the two GenAI instruments for one attempt.

    Different denominators on purpose: duration is per HTTP attempt, token
    usage per logical call — and only when the provider actually reported it,
    never a synthesised zero.
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
    """Run a recording step, swallowing anything it raises — telemetry must
    never fail an answered provider call or replace its ``LLMError``.
    """
    try:
        record()
    except Exception:
        logger.debug("GenAI telemetry: %s failed", what, exc_info=True)


# ---------------------------------------------------------------------------
# run / lead ambient state
# ---------------------------------------------------------------------------
#
# ContextVars, not a threaded-through argument: `asyncio.gather`/`to_thread`
# copy the context per task, so the lead-scoped counter stays per-task under
# concurrency. HAZARD: `loop.run_in_executor` and pre-existing executors do NOT
# copy it — carry the context across explicitly there
# (`contextvars.copy_context().run(...)`) or counts silently become zero.

_current_run: ContextVar["RunRecorder | None"] = ContextVar("outreach_run", default=None)
_current_lead: ContextVar["LeadSpan | None"] = ContextVar("outreach_lead", default=None)


def sha256_of(text: str | None) -> str | None:
    """Hex digest of ``text``, or ``None`` when there is nothing to digest —
    the only thing a span ever learns about prompt or completion content.
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

    ``skipped`` is a decision, ``not_generated`` a failure — collapsed, a
    provider outage would look like a quiet week.
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


@dataclass(eq=False)
class RunRecorder:
    """Totals accumulated over one planner run, written to the run span at the end.

    Also owns the run's lead spans, so an escaping exception cannot leave one
    open forever (an unended span is never exported).
    """

    run_id: str
    span: "Span"
    tracer: Any = None
    input_tokens: int = 0
    output_tokens: int = 0
    saw_usage: bool = False
    finished: bool = False
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
        lead = _start_lead_span(
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
        """End any lead span the planner did not finish, each guarded
        separately so one failure cannot take the rest or mask the planner's
        real exception.
        """
        with self._lock:
            leads = list(self.leads)
        for lead in leads:
            try:
                lead.abandon(exc)
            except Exception:
                logger.warning("Could not end a lead span", exc_info=True)

    def add_usage(self, input_tokens: int | None, output_tokens: int | None) -> None:
        """Fold one provider call's usage into the run total."""
        if input_tokens is None and output_tokens is None:
            return
        with self._lock:
            self.input_tokens += input_tokens or 0
            self.output_tokens += output_tokens or 0
            # So a run with no reported usage emits none, not a confident zero.
            self.saw_usage = True

    def finish(self, *, lead_count: int, needs_human_count: int) -> None:
        """Write the run's totals. Idempotent, like :meth:`LeadSpan.finish`."""
        with self._lock:
            if self.finished:
                return
            self.finished = True
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
    """The whole planner run: ``invoke_agent outreach_planner``, kind INTERNAL
    (the agent runs in-process).

    Yields the recorder the planner reports totals to. ``run_id`` (generated
    unless supplied) is the value written to ``OutreachAction.trace_run_id``.
    """
    active = tracer if tracer is not None else get_tracer()
    resolved_run_id = run_id or str(uuid.uuid4())
    if len(resolved_run_id) > RUN_ID_MAX_LENGTH:
        # Refused here rather than dying on the phase-5 write (`value too long`).
        raise ValueError(
            f"run_id must be at most {RUN_ID_MAX_LENGTH} characters to fit "
            f"OutreachAction.trace_run_id; got {len(resolved_run_id)}."
        )
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

    Started once and ended once, covering both planner phases, with
    :meth:`active` making it current per phase. Carries no ``gen_ai.*``
    attributes — the conventions define nothing for it.
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
        """Make this the current span for a phase, without ending it
        (``end_on_exit=False``: phase done, not lead done).
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
        }
        # Absent, not zero, when no attempt was made -- same rule as usage.
        _set_if(attributes, sc.LLM_ATTEMPTS, attempts or None)
        _set_if(attributes, sc.OUTPUT_REF, output_ref)
        _set_if(attributes, sc.OUTPUT_SHA256, output_sha256)
        if failure is not None:
            attributes[sc.FAILURE_KIND] = error_type(failure)
            attributes[sc.FAILURE_DOMAIN] = fault_domain(failure)
        self._span.set_attributes(attributes)
        if failure is not None:
            # ERROR on the lead, not the run: one dead call must not sink the run.
            self._span.set_status(Status(StatusCode.ERROR, error_type(failure)))
        self._span.end()

    def abandon(self, exc: BaseException | None = None) -> None:
        """End the span without a verdict — otherwise the lead would vanish
        from the trace (unended spans are never exported). No-op after
        :meth:`finish`.
        """
        with self._lock:
            if self._ended:
                return
            self._ended = True
        self._span.set_attribute(sc.VERIFY_OUTCOME, sc.VERIFY_NOT_GENERATED)
        if exc is not None:
            self._span.set_attribute(sc.FAILURE_KIND, error_type(exc))
            self._span.set_attribute(sc.FAILURE_DOMAIN, fault_domain(exc))
            self._span.set_status(Status(StatusCode.ERROR, error_type(exc)))
        self._span.end()


def _start_lead_span(
    *,
    run_id: str,
    lead_id: str,
    action_type: str,
    priority: int,
    prompt: str | None = None,
    tracer: Any = None,
) -> LeadSpan:
    """Open a lead's span; the caller owns ending it via :meth:`LeadSpan.finish`.
    ``prompt`` is hashed, never recorded.
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
    """Close a lead span from the facts the planner already has; everything
    derived (outcome, output ref, digest) is derived here, not at the call site.
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
        # Emitted for every lead -- a row is written for each; only the digest
        # is a claim about content, so only it is gated on there being any.
        output_ref=output_ref(run_id, lead_id),
        output_sha256=sha256_of(output_text),
        failure=failure,
    )


def output_ref(run_id: str, lead_id: str) -> str:
    """Reference to the row this lead's work will produce.

    A promise, not a fact: emitted before the run's single atomic write, so a
    rolled-back run leaves green lead spans and no row — check the run span's
    ``error.type`` in that case.
    """
    return f"outreach_action:{run_id}:{lead_id}"


@contextmanager
def tool_span(
    name: str, *, args_sha256: str | None = None
) -> Iterator[Callable[[str | None], None]]:
    """One agent tool execution: ``execute_tool {name}``, kind INTERNAL (MUS-29).

    Yields a setter for the result's sha256. The span carries content hashes
    only, never payloads; the same hashes land on the ``AgentStep`` row.
    """
    active = get_tracer()
    attributes: dict[str, Any] = {
        sc.GEN_AI_OPERATION_NAME: sc.OPERATION_EXECUTE_TOOL,
        sc.GEN_AI_TOOL_NAME: name,
        sc.OPENINFERENCE_SPAN_KIND: sc.OPENINFERENCE_KIND_TOOL,
    }
    if args_sha256:
        attributes[sc.INPUT_SHA256] = args_sha256
    with active.start_as_current_span(
        f"{sc.OPERATION_EXECUTE_TOOL} {name}",
        kind=SpanKind.INTERNAL,
        record_exception=False,
        set_status_on_exception=False,
        attributes=attributes,
    ) as span:

        def set_result_sha256(result_sha256: str | None) -> None:
            if result_sha256:
                span.set_attribute(sc.OUTPUT_SHA256, result_sha256)

        try:
            yield set_result_sha256
        except BaseException as exc:
            span.set_attribute(sc.ERROR_TYPE, error_type(exc))
            span.set_status(Status(StatusCode.ERROR, error_type(exc)))
            raise


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

    Takes a 0-based attempt index and returns a context manager: entering opens
    the CLIENT span and yields the ``record_result`` hook, leaving closes the
    span either way — including on cancellation, which is why the seam is a
    scope rather than a pair of callbacks. ``tracer`` is injectable for tests;
    production passes nothing.
    """

    @contextmanager
    def scope(attempt: int) -> Iterator[Callable[["LLMResult"], None]]:
        # Resolved per attempt: a tracer captured when the factory was built
        # would stay a no-op if the provider is installed in between.
        active = tracer if tracer is not None else get_tracer()
        recorded: list[LLMResult] = []
        started = time.perf_counter()
        with active.start_as_current_span(
            call.span_name,
            kind=SpanKind.CLIENT,
            # Both off so the provider's error message never reaches the span.
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
                # BaseException, not Exception: `CancelledError` must still end
                # the span, or cancelled leads vanish from the trace.
                # Bound to a local because `except ... as exc` unbinds `exc` at
                # the end of the block, and the closures below outlive the name.
                failure = exc
                # The adapter's own latency beats our wall clock; absent only
                # for a caller-constructed error.
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
                # Empty when the caller opted out of reporting a result; the
                # span still closes green, just without response attributes.
                result = recorded[-1] if recorded else None
                # Duration is recorded either way, so the latency instrument
                # does not under-count.
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
    "fault_domain",
    "finish_lead",
    "instruments",
    "output_ref",
    "provider_call_scope",
    "run_span",
    "sha256_of",
    "tool_span",
    "verify_outcome",
]
