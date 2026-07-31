"""Tests for the OpenTelemetry bootstrap (MUS-25, 25-a).

The claim under test is the one the rest of the instrumentation is built on:
**instrumented code does not know whether telemetry is switched on.** So the
important tests here are not "does it export" but "what happens when it
doesn't" — no endpoint must produce a ``NonRecordingSpan`` and no installed
provider, along the same statements the demo runs.

Two conventions keep these tests from fighting the OTel API's process-global
provider:

* every test that exercises :func:`configure`'s decision logic patches
  ``trace.set_tracer_provider``, so it asserts on the decision without mutating
  the process;
* every one of them restores the idempotence flag on the way out, so
  ``tests_telemetry_support`` can still install the shared in-memory provider
  whatever order the suite runs in.
"""

import os
import sys
from unittest import mock

from django.test import SimpleTestCase
from opentelemetry import trace

from project.app.services import telemetry
from project.app.services.telemetry import setup

from .tests_telemetry_support import install_in_memory_tracing, reset_spans, spans_named

# Every environment variable that can switch tracing on, cleared together. A
# test that clears only one of them passes for the wrong reason on a machine
# where the other is exported.
_TRACING_ENV = {name: "" for name in setup.ENDPOINT_ENV_VARS}


class _ConfigureTestCase(SimpleTestCase):
    """Base for tests that call ``configure``/``configure_from_env``.

    Patches out the global provider install and always restores the idempotence
    flag, so these tests can run in any order relative to the ones that want
    real recorded spans.
    """

    def setUp(self):
        super().setUp()
        self.set_provider = self.enterContext(
            mock.patch.object(trace, "set_tracer_provider", autospec=True)
        )
        self.build_processor = self.enterContext(
            mock.patch.object(setup, "_build_otlp_span_processor", autospec=True)
        )
        setup._reset_for_tests()
        self.addCleanup(setup._reset_for_tests)


class NoEndpointTests(_ConfigureTestCase):
    """With no OTLP endpoint configured, nothing is installed and nothing breaks."""

    def test_configure_from_env_installs_nothing(self):
        with mock.patch.dict(os.environ, _TRACING_ENV, clear=False):
            self.assertFalse(telemetry.configure_from_env())
        self.assertFalse(setup.is_installed())
        self.set_provider.assert_not_called()
        # The exporter is not merely unused -- it is never constructed, so an
        # unreachable collector host cannot cost startup a DNS timeout.
        self.build_processor.assert_not_called()

    def test_a_whitespace_only_endpoint_counts_as_unset(self):
        # `OTEL_EXPORTER_OTLP_ENDPOINT=` in a .env file is how an operator says
        # "off"; reading it as a hostname would be a startup crash on a blank line.
        with mock.patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": "   "}, clear=False):
            self.assertIsNone(telemetry.otlp_endpoint())
            self.assertFalse(telemetry.configure_from_env())

    def test_either_endpoint_variable_switches_tracing_on(self):
        for name in setup.ENDPOINT_ENV_VARS:
            with self.subTest(variable=name):
                env = dict(_TRACING_ENV, **{name: "http://phoenix:6006"})
                with mock.patch.dict(os.environ, env, clear=False):
                    self.assertEqual(telemetry.otlp_endpoint(), "http://phoenix:6006")

    def test_no_provider_yields_a_non_recording_span(self):
        """The guarantee the whole no-branching design rests on.

        A tracer from the API's no-op provider still supports the full span
        protocol -- ``set_attribute`` and ``set_status`` are empty method bodies
        rather than errors -- which is why the planner can open spans and set
        attributes unconditionally.
        """
        tracer = trace.get_tracer(__name__, tracer_provider=trace.NoOpTracerProvider())
        with tracer.start_as_current_span("chat some-model") as span:
            self.assertIsInstance(span, trace.NonRecordingSpan)
            self.assertFalse(span.is_recording())
            span.set_attribute("gen_ai.request.model", "some-model")
            span.set_attribute("gen_ai.response.finish_reasons", ["stop"])
            span.set_status(trace.Status(trace.StatusCode.ERROR))
            span.record_exception(RuntimeError("boom"))


class ConfigureTests(_ConfigureTestCase):
    def test_endpoint_set_installs_the_otlp_exporter(self):
        env = dict(_TRACING_ENV, OTEL_EXPORTER_OTLP_ENDPOINT="http://phoenix:6006")
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertTrue(telemetry.configure_from_env())
        self.assertTrue(setup.is_installed())
        self.build_processor.assert_called_once_with()
        self.set_provider.assert_called_once()

    def test_a_second_configure_installs_nothing(self):
        """``AppConfig.ready()`` can fire more than once per process.

        Two live ``BatchSpanProcessor``s would each hold an exporter and each
        register an ``atexit`` shutdown, so the second call has to be a no-op
        rather than a second install.
        """
        first = telemetry.configure(mock.Mock())
        second = telemetry.configure(mock.Mock())
        self.assertTrue(first)
        self.assertFalse(second)
        self.set_provider.assert_called_once()

    def test_an_injected_processor_is_the_one_installed(self):
        processor = mock.Mock()
        telemetry.configure(processor)
        self.build_processor.assert_not_called()
        provider = self.set_provider.call_args.args[0]
        # add_span_processor is real here (the provider is a real SDK object);
        # asserting the processor is wired means the injection seam the tests
        # below depend on is genuinely the production one.
        self.assertIn(processor, provider._active_span_processor._span_processors)


class ResourceTests(_ConfigureTestCase):
    def _installed_resource(self):
        telemetry.configure(mock.Mock())
        return self.set_provider.call_args.args[0].resource

    def test_service_name_defaults_when_the_environment_is_silent(self):
        with mock.patch.dict(os.environ, {"OTEL_SERVICE_NAME": ""}, clear=False):
            resource = self._installed_resource()
        self.assertEqual(resource.attributes["service.name"], telemetry.DEFAULT_SERVICE_NAME)

    def test_otel_service_name_wins_over_the_default(self):
        """Passing ``service.name`` unconditionally would silently override the
        operator's ``OTEL_SERVICE_NAME``, which the SDK reads for itself."""
        with mock.patch.dict(os.environ, {"OTEL_SERVICE_NAME": "outreach-staging"}, clear=False):
            resource = self._installed_resource()
        self.assertEqual(resource.attributes["service.name"], "outreach-staging")


class AutoreloadTests(_ConfigureTestCase):
    """``runserver``'s watcher process must not hold a second exporter."""

    def setUp(self):
        super().setUp()
        self.env = dict(_TRACING_ENV, OTEL_EXPORTER_OTLP_ENDPOINT="http://phoenix:6006")

    def _configure(self, argv, run_main=None):
        env = dict(self.env, RUN_MAIN=run_main or "")
        with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, env, clear=False):
            return telemetry.configure_from_env()

    def test_the_watcher_process_does_not_install(self):
        self.assertFalse(self._configure(["manage.py", "runserver"]))
        self.set_provider.assert_not_called()

    def test_the_reloaded_child_installs(self):
        self.assertTrue(self._configure(["manage.py", "runserver"], run_main="true"))

    def test_noreload_installs_in_the_only_process_there_is(self):
        self.assertTrue(self._configure(["manage.py", "runserver", "--noreload"]))

    def test_other_entrypoints_install_without_run_main(self):
        """The trap this guard has to avoid: ``RUN_MAIN`` is unset for gunicorn,
        ``manage.py test`` and every management command, so keying on it alone
        would disable telemetry everywhere except ``runserver``."""
        for argv in (["manage.py", "test"], ["gunicorn", "project.wsgi"], ["manage.py", "shell"]):
            with self.subTest(argv=argv):
                setup._reset_for_tests()
                self.assertTrue(self._configure(argv))


class RecordedSpanTests(SimpleTestCase):
    """End to end through the real API: a provider is installed, spans arrive."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.exporter = install_in_memory_tracing()

    def setUp(self):
        super().setUp()
        reset_spans()

    def test_a_span_opened_through_get_tracer_is_exported(self):
        with telemetry.get_tracer().start_as_current_span("plan_lead") as span:
            self.assertTrue(span.is_recording())
            span.set_attribute("outreach.lead.id", "lead_007")

        (recorded,) = spans_named("plan_lead")
        self.assertEqual(recorded.attributes["outreach.lead.id"], "lead_007")
        self.assertEqual(recorded.instrumentation_scope.name, setup.INSTRUMENTATION_NAME)
        self.assertEqual(recorded.instrumentation_scope.version, setup.INSTRUMENTATION_VERSION)

    def test_nesting_produces_a_parent_child_relationship(self):
        tracer = telemetry.get_tracer()
        with tracer.start_as_current_span("invoke_agent outreach_planner") as parent:
            parent_id = parent.get_span_context().span_id
            with tracer.start_as_current_span("plan_lead"):
                pass

        (child,) = spans_named("plan_lead")
        self.assertIsNotNone(child.parent)
        self.assertEqual(child.parent.span_id, parent_id)
