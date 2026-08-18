"""Tests for the GenAI provider-call span and metrics (MUS-25, 25-b).
Span assertions pin the exact attribute set; metric assertions pin units and buckets."""

import unittest
from unittest import mock

from django.test import SimpleTestCase
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View

from project.app.services.llm import retry
from project.app.services.llm.base import LLMResult
from project.app.services.llm.errors import (
    LLMAuthError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from project.app.services.telemetry import genai, semconv, setup

from .tests_telemetry_support import RecordingTestCase, spans_named

CALL = genai.ProviderCall(
    provider="groq",
    model="openai/gpt-oss-20b",
    max_tokens=500,
    base_url="https://api.groq.com/openai/v1",
)

RESULT = LLMResult(
    text="Subject: hello\n\nBody.",
    provider="groq",
    model="openai/gpt-oss-20b",
    response_model="openai/gpt-oss-20b-0125",
    input_tokens=910,
    output_tokens=140,
    finish_reason="stop",
    raw_finish_reason="end_turn",
    latency_s=0.42,
)


def _immediate_sleep(_seconds):
    """Retry backoff, minus the waiting. Keeps a three-attempt test instant."""

    async def _noop():
        return None

    return _noop()


class ProviderCallTests(SimpleTestCase):
    def test_the_provider_map_is_total_over_the_shipped_providers(self):
        """Every provider in the client registry maps to a legal enum member."""
        from project.app.services.llm import _REGISTRY

        # The bench-only stub is deliberately unmapped: it must emit no
        # `gen_ai.provider.name` rather than an invented enum member.
        self.assertEqual(set(_REGISTRY) - {"stub"}, set(genai.PROVIDER_NAMES))
        legal = {
            semconv.PROVIDER_ANTHROPIC,
            semconv.PROVIDER_OPENAI,
            semconv.PROVIDER_GROQ,
            semconv.PROVIDER_DEEPSEEK,
        }
        self.assertTrue(set(genai.PROVIDER_NAMES.values()) <= legal)

    def test_an_unmapped_provider_gets_no_gen_ai_provider_name(self):
        """An unmapped provider emits no ``gen_ai.provider.name`` rather than an invented one."""
        call = genai.ProviderCall(provider="stub", model="stub-1")
        self.assertIsNone(call.gen_ai_provider)
        self.assertEqual(call.metric_attributes()[semconv.LLM_PROVIDER_CONFIGURED], "stub")
        self.assertNotIn(semconv.GEN_AI_PROVIDER_NAME, call.metric_attributes())

    def test_span_name_follows_the_spec_template(self):
        self.assertEqual(CALL.span_name, "chat openai/gpt-oss-20b")

    def test_from_client_reads_the_adapter(self):
        client = mock.Mock(
            provider_name="groq",
            model="openai/gpt-oss-20b",
            base_url="https://api.groq.com/openai/v1",
            default_max_tokens=500,
        )
        call = genai.ProviderCall.from_client(client, 250)
        self.assertEqual(call.provider, "groq")
        self.assertEqual(call.max_tokens, 250)
        self.assertEqual(call.base_url, "https://api.groq.com/openai/v1")

    def test_from_client_falls_back_to_the_adapter_default_max_tokens(self):
        client = mock.Mock(provider_name="groq", model="m", base_url="https://h/v1")
        client.default_max_tokens = 500
        self.assertEqual(genai.ProviderCall.from_client(client).max_tokens, 500)

    def test_an_adapter_without_a_base_url_omits_the_server_attributes(self):
        """No ``base_url`` means no ``server.*`` attributes — a hardcoded address would be a lie."""

        class Adapter:
            provider_name = "claude"
            model = "claude-sonnet-4-6"
            default_max_tokens = 500

        call = genai.ProviderCall.from_client(Adapter())
        self.assertIsNone(call.base_url)
        self.assertEqual(genai._server_attributes(call.base_url), {})

    def test_server_port_is_only_emitted_when_the_url_names_one(self):
        explicit = genai._server_attributes("http://localhost:11434/v1")
        self.assertEqual(explicit[semconv.SERVER_ADDRESS], "localhost")
        self.assertEqual(explicit[semconv.SERVER_PORT], 11434)

        # No synthesised 443 for an implicit port.
        implicit = genai._server_attributes("https://api.groq.com/openai/v1")
        self.assertEqual(implicit, {semconv.SERVER_ADDRESS: "api.groq.com"})

    def test_a_malformed_authority_does_not_raise(self):
        """Telemetry must never be the thing that breaks a provider call."""
        self.assertNotIn(semconv.SERVER_PORT, genai._server_attributes("http://host:notaport/v1"))


class _SpanTestCase(RecordingTestCase):
    async def _run(self, operation, *, call=CALL, max_attempts=4):
        return await retry.acall_with_retry(
            operation,
            policy=retry.RetryPolicy(max_attempts=max_attempts, initial_backoff_s=0.0),
            sleep=_immediate_sleep,
            attempt_scope=genai.provider_call_scope(call),
        )

    def client_spans(self):
        return spans_named(CALL.span_name)


class SuccessfulCallSpanTests(_SpanTestCase):
    async def test_the_exact_attribute_set_on_a_successful_call(self):
        async def operation():
            return RESULT

        await self._run(operation)

        (span,) = self.client_spans()
        self.assertEqual(span.kind, trace.SpanKind.CLIENT)
        self.assertEqual(span.status.status_code, trace.StatusCode.OK)
        self.assertEqual(
            dict(span.attributes),
            {
                semconv.GEN_AI_OPERATION_NAME: "chat",
                semconv.GEN_AI_PROVIDER_NAME: "groq",
                semconv.GEN_AI_REQUEST_MODEL: "openai/gpt-oss-20b",
                semconv.GEN_AI_REQUEST_MAX_TOKENS: 500,
                semconv.GEN_AI_RESPONSE_MODEL: "openai/gpt-oss-20b-0125",
                semconv.GEN_AI_RESPONSE_FINISH_REASONS: ("stop",),
                semconv.GEN_AI_USAGE_INPUT_TOKENS: 910,
                semconv.GEN_AI_USAGE_OUTPUT_TOKENS: 140,
                semconv.SERVER_ADDRESS: "api.groq.com",
                semconv.LLM_PROVIDER_CONFIGURED: "groq",
                semconv.LLM_ATTEMPT: 1,
                semconv.OPENINFERENCE_SPAN_KIND: "LLM",
                semconv.LLM_MODEL_NAME: "openai/gpt-oss-20b",
                semconv.LLM_PROVIDER: "groq",
                semconv.LLM_TOKEN_COUNT_PROMPT: 910,
                semconv.LLM_TOKEN_COUNT_COMPLETION: 140,
                semconv.LLM_TOKEN_COUNT_TOTAL: 1050,
            },
        )

    async def test_finish_reasons_is_an_array_of_strings(self):
        """The spec wants an array here; a bare string looks right in every UI but is invalid."""

        async def operation():
            return RESULT

        await self._run(operation)
        (span,) = self.client_spans()
        reasons = span.attributes[semconv.GEN_AI_RESPONSE_FINISH_REASONS]
        self.assertIsInstance(reasons, tuple)  # the SDK freezes sequence attributes
        self.assertNotIsInstance(reasons, str)
        self.assertTrue(all(isinstance(r, str) for r in reasons))

    async def test_the_raw_reason_is_used_when_normalization_declined(self):
        """An unmapped-but-legitimate stop reason shows the raw word, not an empty attribute."""
        unmapped = LLMResult(
            text="x",
            provider="groq",
            model="openai/gpt-oss-20b",
            finish_reason=None,
            raw_finish_reason="pause_turn",
        )

        async def operation():
            return unmapped

        await self._run(operation)
        (span,) = self.client_spans()
        self.assertEqual(span.attributes[semconv.GEN_AI_RESPONSE_FINISH_REASONS], ("pause_turn",))

    async def test_absent_usage_leaves_the_token_attributes_off(self):
        """Absent usage means absent attributes — zero would say something false."""
        no_usage = LLMResult(text="x", provider="groq", model="openai/gpt-oss-20b")

        async def operation():
            return no_usage

        await self._run(operation)
        (span,) = self.client_spans()
        for key in (
            semconv.GEN_AI_USAGE_INPUT_TOKENS,
            semconv.GEN_AI_USAGE_OUTPUT_TOKENS,
            semconv.LLM_TOKEN_COUNT_PROMPT,
            semconv.LLM_TOKEN_COUNT_COMPLETION,
            semconv.LLM_TOKEN_COUNT_TOTAL,
        ):
            self.assertNotIn(key, span.attributes)

    async def test_the_total_is_absent_unless_both_counts_are_known(self):
        partial = LLMResult(text="x", provider="groq", model="openai/gpt-oss-20b", input_tokens=910)

        async def operation():
            return partial

        await self._run(operation)
        (span,) = self.client_spans()
        self.assertEqual(span.attributes[semconv.LLM_TOKEN_COUNT_PROMPT], 910)
        self.assertNotIn(semconv.LLM_TOKEN_COUNT_TOTAL, span.attributes)


class RetrySpanTests(_SpanTestCase):
    async def test_three_attempts_produce_three_spans_two_of_them_red(self):
        """One CLIENT span per attempt: the first two red with ``error.type``, the third green."""
        attempts = []

        async def operation():
            attempts.append(len(attempts))
            if len(attempts) < 3:
                raise LLMRateLimitError("throttled", provider="groq", retry_after=2.0)
            return RESULT

        await self._run(operation)

        spans = self.client_spans()
        self.assertEqual(len(spans), 3)
        self.assertEqual(
            [s.status.status_code for s in spans],
            [trace.StatusCode.ERROR, trace.StatusCode.ERROR, trace.StatusCode.OK],
        )
        self.assertEqual([s.attributes[semconv.LLM_ATTEMPT] for s in spans], [1, 2, 3])
        for failed in spans[:2]:
            self.assertEqual(failed.attributes[semconv.ERROR_TYPE], "LLMRateLimitError")
            self.assertEqual(failed.attributes[semconv.LLM_RETRY_AFTER_S], 2.0)
        self.assertNotIn(semconv.ERROR_TYPE, spans[2].attributes)

    async def test_a_non_retryable_failure_produces_exactly_one_red_span(self):
        async def operation():
            raise LLMAuthError("no key", provider="groq")

        with self.assertRaises(LLMAuthError):
            await self._run(operation)

        (span,) = self.client_spans()
        self.assertEqual(span.status.status_code, trace.StatusCode.ERROR)
        self.assertEqual(span.attributes[semconv.ERROR_TYPE], "LLMAuthError")
        self.assertEqual(span.attributes[semconv.LLM_ATTEMPT], 1)

    async def test_retry_after_is_absent_when_the_provider_gave_no_guidance(self):
        async def operation():
            raise LLMTimeoutError("read timeout", provider="groq")

        with self.assertRaises(LLMTimeoutError):
            await self._run(operation, max_attempts=1)

        (span,) = self.client_spans()
        self.assertNotIn(semconv.LLM_RETRY_AFTER_S, span.attributes)

    async def test_the_provider_error_message_never_reaches_the_span(self):
        """Only the exception's class name reaches the span — no message, no exception event."""
        secret = "CANARY-9f3a-lead-notes"

        async def operation():
            raise LLMError(f"upstream said: {secret}", provider="groq")

        with self.assertRaises(LLMError):
            await self._run(operation, max_attempts=1)

        (span,) = self.client_spans()
        self.assertEqual(span.status.description, "LLMError")
        self.assertEqual(span.events, ())
        self.assertNotIn(secret, repr(dict(span.attributes)))
        self.assertNotIn(secret, str(span.status.description))

    async def test_an_unexpected_exception_still_closes_the_span(self):
        """A non-``LLMError`` escapes immediately, but the span still closes red."""

        async def operation():
            raise ValueError("bug on our side")

        with self.assertRaises(ValueError):
            await self._run(operation)

        (span,) = self.client_spans()
        self.assertEqual(span.attributes[semconv.ERROR_TYPE], "ValueError")
        self.assertEqual(span.status.status_code, trace.StatusCode.ERROR)

    async def test_a_cancelled_attempt_still_closes_its_span(self):
        """``CancelledError`` is a ``BaseException``; an unended span is never exported."""
        import asyncio

        async def operation():
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await self._run(operation)

        (span,) = self.client_spans()
        self.assertEqual(span.attributes[semconv.ERROR_TYPE], "CancelledError")
        self.assertEqual(span.status.status_code, trace.StatusCode.ERROR)

    async def test_a_broken_recorder_cannot_fail_a_call_the_provider_answered(self):
        """A recorder exception in ``__exit__`` must not replace a successful return."""

        async def operation():
            return RESULT

        with mock.patch.object(genai, "_record_success", side_effect=RuntimeError("boom")):
            with mock.patch.object(genai, "_record_metrics", side_effect=RuntimeError("boom")):
                result = await self._run(operation)

        self.assertIs(result, RESULT)

    async def test_a_broken_recorder_cannot_swallow_a_provider_failure(self):
        """A recorder exception must not replace the ``LLMError`` in flight."""

        async def operation():
            raise LLMRateLimitError("throttled", provider="groq")

        with mock.patch.object(genai, "_record_failure", side_effect=RuntimeError("boom")):
            with self.assertRaises(LLMRateLimitError):
                await self._run(operation, max_attempts=1)

    async def test_a_provider_authored_finish_reason_is_shape_checked(self):
        """``raw_finish_reason`` comes off the provider's wire: an enum-ish token passes, a sentence does not."""
        smuggled = LLMResult(
            text="x",
            provider="groq",
            model="openai/gpt-oss-20b",
            finish_reason=None,
            raw_finish_reason="stopped because " + "A" * 200,
        )

        async def operation():
            return smuggled

        await self._run(operation)
        (span,) = self.client_spans()
        self.assertNotIn(semconv.GEN_AI_RESPONSE_FINISH_REASONS, span.attributes)


class NoTelemetryPathTests(SimpleTestCase):
    """The same helper against no-op providers: no second code path, nothing raises.

    Both providers are stubbed, not just the tracer — otherwise ``instruments()``
    would still reach the process-global meter.
    """

    def setUp(self):
        super().setUp()
        self.enterContext(
            mock.patch.object(
                genai, "get_meter", lambda: metrics.NoOpMeterProvider().get_meter("t")
            )
        )
        genai._reset_instruments_for_tests()
        self.addCleanup(genai._reset_instruments_for_tests)

    async def test_the_scope_works_against_a_no_op_tracer(self):
        tracer = trace.get_tracer(__name__, tracer_provider=trace.NoOpTracerProvider())
        scope = genai.provider_call_scope(CALL, tracer=tracer)

        with scope(0) as record:
            record(RESULT)

        with self.assertRaises(LLMRateLimitError), scope(1):
            raise LLMRateLimitError("throttled", provider="groq", retry_after=1.0)


class NullContextSemanticsTests(_SpanTestCase):
    async def test_a_scope_whose_caller_reports_no_result_still_closes_green(self):
        """A scope with no reported result closes green, with no response-side attributes."""
        scope = genai.provider_call_scope(CALL)
        with scope(0):
            pass

        (span,) = spans_named(CALL.span_name)
        self.assertNotEqual(span.status.status_code, trace.StatusCode.ERROR)
        self.assertNotIn(semconv.GEN_AI_USAGE_INPUT_TOKENS, span.attributes)


class MetricTests(unittest.IsolatedAsyncioTestCase):
    """Each test builds its own ``MeterProvider`` and patches the meter lookup —
    a fresh reader per test, untouched by global telemetry state."""

    def setUp(self):
        self.reader = InMemoryMetricReader()
        self.provider = MeterProvider(
            metric_readers=[self.reader],
            views=[
                View(
                    instrument_name=semconv.METRIC_OPERATION_DURATION,
                    aggregation=ExplicitBucketHistogramAggregation(
                        setup.OPERATION_DURATION_BUCKETS
                    ),
                ),
                View(
                    instrument_name=semconv.METRIC_TOKEN_USAGE,
                    aggregation=ExplicitBucketHistogramAggregation(setup.TOKEN_USAGE_BUCKETS),
                ),
            ],
        )
        patcher = mock.patch.object(
            genai, "get_meter", lambda: self.provider.get_meter(setup.INSTRUMENTATION_NAME)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        genai._reset_instruments_for_tests()
        self.addCleanup(genai._reset_instruments_for_tests)

    def _metrics_by_name(self):
        data = self.reader.get_metrics_data()
        found = {}
        for resource_metric in data.resource_metrics:
            for scope_metric in resource_metric.scope_metrics:
                for metric in scope_metric.metrics:
                    found[metric.name] = metric
        return found

    async def _call(self, operation, *, max_attempts=4):
        return await retry.acall_with_retry(
            operation,
            policy=retry.RetryPolicy(max_attempts=max_attempts, initial_backoff_s=0.0),
            sleep=_immediate_sleep,
            attempt_scope=genai.provider_call_scope(CALL),
        )

    async def test_instrument_names_units_and_buckets(self):
        async def operation():
            return RESULT

        await self._call(operation)
        found = self._metrics_by_name()

        duration = found[semconv.METRIC_OPERATION_DURATION]
        self.assertEqual(duration.unit, "s")
        self.assertEqual(
            tuple(duration.data.data_points[0].explicit_bounds), setup.OPERATION_DURATION_BUCKETS
        )

        tokens = found[semconv.METRIC_TOKEN_USAGE]
        # Braces are UCUM's annotation form for a dimensionless count.
        self.assertEqual(tokens.unit, "{token}")
        self.assertEqual(
            tuple(tokens.data.data_points[0].explicit_bounds), setup.TOKEN_USAGE_BUCKETS
        )

    async def test_token_usage_records_exactly_twice_per_successful_call(self):
        async def operation():
            return RESULT

        await self._call(operation)
        points = self._metrics_by_name()[semconv.METRIC_TOKEN_USAGE].data.data_points

        self.assertEqual(len(points), 2)
        by_type = {p.attributes[semconv.GEN_AI_TOKEN_TYPE]: p for p in points}
        self.assertEqual(set(by_type), {"input", "output"})
        self.assertEqual(by_type["input"].sum, 910)
        self.assertEqual(by_type["output"].sum, 140)
        for point in points:
            # gen_ai.provider.name is Required on this instrument.
            self.assertEqual(point.attributes[semconv.GEN_AI_PROVIDER_NAME], "groq")
            self.assertEqual(point.attributes[semconv.GEN_AI_OPERATION_NAME], "chat")

    async def test_no_token_points_at_all_when_usage_is_absent(self):
        """Absent usage records no points — a 0 would poison the histogram."""

        async def operation():
            return LLMResult(text="x", provider="groq", model="openai/gpt-oss-20b")

        await self._call(operation)
        self.assertNotIn(semconv.METRIC_TOKEN_USAGE, self._metrics_by_name())

    async def test_duration_records_once_per_attempt_including_failures(self):
        attempts = []

        async def operation():
            attempts.append(1)
            if len(attempts) < 3:
                raise LLMRateLimitError("throttled", provider="groq", retry_after=0.0)
            return RESULT

        await self._call(operation)
        points = self._metrics_by_name()[semconv.METRIC_OPERATION_DURATION].data.data_points

        # Failed attempts share one series; the successful attempt (no error.type) is a second.
        self.assertEqual(sum(p.count for p in points), 3)
        by_error = {p.attributes.get(semconv.ERROR_TYPE): p for p in points}
        self.assertEqual(by_error["LLMRateLimitError"].count, 2)
        self.assertEqual(by_error[None].count, 1)

    async def test_the_successful_duration_is_the_adapters_measurement(self):
        """Duration comes from ``LLMResult.latency_s``, not re-timing around the scope."""

        async def operation():
            return RESULT

        await self._call(operation)
        points = self._metrics_by_name()[semconv.METRIC_OPERATION_DURATION].data.data_points
        (point,) = [p for p in points if semconv.ERROR_TYPE not in p.attributes]
        self.assertAlmostEqual(point.sum, 0.42, places=6)

    async def test_a_failed_attempt_uses_the_latency_the_error_carries(self):
        async def operation():
            raise LLMTimeoutError("timeout", provider="groq").with_latency(60.0)

        with self.assertRaises(LLMTimeoutError):
            await self._call(operation, max_attempts=1)

        points = self._metrics_by_name()[semconv.METRIC_OPERATION_DURATION].data.data_points
        (point,) = points
        self.assertAlmostEqual(point.sum, 60.0, places=6)
        self.assertEqual(point.attributes[semconv.ERROR_TYPE], "LLMTimeoutError")


class MetricsConfigurationTests(SimpleTestCase):
    """Metrics are off unless explicitly switched on, and by their own switch."""

    def setUp(self):
        super().setUp()
        # Both halves of the accessor: `configure_metrics` reads the provider back
        # to verify its install.
        self.registered = None
        self.set_provider = self.enterContext(
            mock.patch.object(
                setup.metrics,
                "set_meter_provider",
                side_effect=lambda provider: setattr(self, "registered", provider),
            )
        )
        self.enterContext(
            mock.patch.object(
                setup.metrics, "get_meter_provider", side_effect=lambda: self.registered
            )
        )
        setup._reset_for_tests()
        self.addCleanup(setup._reset_for_tests)

    def test_the_metrics_endpoint_has_its_own_variable(self):
        with mock.patch.dict(
            "os.environ",
            {"OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "http://otelcol:4318"},
            clear=False,
        ):
            self.assertEqual(setup.otlp_metrics_endpoint(), "http://otelcol:4318")

    def test_a_traces_endpoint_alone_starts_no_metrics_exporter(self):
        """A traces endpoint alone does not switch on the metrics exporter."""
        env = {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://phoenix:6006",
            "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "",
            setup.CONSOLE_METRICS_ENV: "",
        }
        with mock.patch.dict("os.environ", env, clear=False):
            self.assertIsNone(setup.otlp_metrics_endpoint())
            self.assertFalse(setup.configure_metrics())
        self.assertFalse(setup.is_metrics_installed())

    def test_an_injected_reader_installs(self):
        self.assertTrue(setup.configure_metrics(InMemoryMetricReader()))
        self.assertTrue(setup.is_metrics_installed())
        self.assertFalse(setup.configure_metrics(InMemoryMetricReader()))

    def test_a_switch_on_with_no_reader_behind_it_installs_nothing(self):
        """A ``MeterProvider`` with no readers would look installed while collecting nothing."""
        with mock.patch.object(setup, "metrics_enabled", return_value=True):
            with mock.patch.object(setup, "_build_metric_readers", return_value=[]):
                self.assertFalse(setup.configure_metrics())
        self.assertFalse(setup.is_metrics_installed())

    def test_a_refused_registration_is_reported_as_a_refusal(self):
        """``set_meter_provider`` silently keeps an incumbent; that must read as a refusal."""
        reader = InMemoryMetricReader()
        with mock.patch.object(reader, "shutdown", wraps=reader.shutdown) as shutdown:
            with mock.patch.object(setup.metrics, "get_meter_provider", return_value=object()):
                self.assertFalse(setup.configure_metrics(reader))
        self.assertFalse(setup.is_metrics_installed())
        # The discarded provider was shut down (its production reader owns a thread).
        shutdown.assert_called_once()

    def test_the_provider_does_not_register_its_own_atexit_handler(self):
        setup.configure_metrics(InMemoryMetricReader())
        provider = self.set_provider.call_args.args[0]
        self.assertIsNone(provider._atexit_handler)

    def test_the_installed_provider_carries_the_explicit_bucket_views(self):
        setup.configure_metrics(InMemoryMetricReader())
        provider = self.set_provider.call_args.args[0]
        views = provider._sdk_config.views
        by_instrument = {v._instrument_name: v for v in views}
        self.assertEqual(
            by_instrument[semconv.METRIC_OPERATION_DURATION]._aggregation._boundaries,
            setup.OPERATION_DURATION_BUCKETS,
        )
        self.assertEqual(
            by_instrument[semconv.METRIC_TOKEN_USAGE]._aggregation._boundaries,
            setup.TOKEN_USAGE_BUCKETS,
        )

    def test_the_bucket_ladders_are_the_ones_the_conventions_specify(self):
        self.assertEqual(setup.OPERATION_DURATION_BUCKETS[0], 0.01)
        self.assertEqual(setup.OPERATION_DURATION_BUCKETS[-1], 81.92)
        self.assertTrue(
            all(
                round(b / a, 6) == 2.0
                for a, b in zip(
                    setup.OPERATION_DURATION_BUCKETS, setup.OPERATION_DURATION_BUCKETS[1:]
                )
            )
        )
        self.assertEqual(setup.TOKEN_USAGE_BUCKETS[0], 1)
        self.assertEqual(setup.TOKEN_USAGE_BUCKETS[-1], 67108864)
        self.assertTrue(
            all(
                b // a == 4
                for a, b in zip(setup.TOKEN_USAGE_BUCKETS, setup.TOKEN_USAGE_BUCKETS[1:])
            )
        )


class ForbiddenContentKeyTests(SimpleTestCase):
    """No module in the telemetry package may even write down a content-carrier key."""

    def test_no_module_in_the_package_writes_a_content_key(self):
        from pathlib import Path

        package = Path(genai.__file__).parent
        for path in sorted(package.glob("*.py")):
            source = path.read_text()
            for key in semconv.FORBIDDEN_CONTENT_KEYS:
                occurrences = source.count(f'"{key}"')
                if path.name == "semconv.py":
                    # semconv.py names them once each, in FORBIDDEN_CONTENT_KEYS.
                    self.assertLessEqual(occurrences, 1, f"{path.name} / {key}")
                else:
                    self.assertEqual(occurrences, 0, f"{path.name} writes {key}")
