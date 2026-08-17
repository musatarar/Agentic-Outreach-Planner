"""Tests for the OpenTelemetry bootstrap (MUS-25, 25-a): instrumented code
must not know whether telemetry is switched on."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase
from opentelemetry import trace

from project.app.services import telemetry
from project.app.services.telemetry import setup

from .tests_telemetry_support import RecordingTestCase, spans_named

# Every env var that can switch telemetry on, cleared together — a leaked
# metrics endpoint would have the suite construct a real network exporter.
_TRACING_ENV = {
    name: ""
    for name in (
        *setup.ENDPOINT_ENV_VARS,
        *setup.METRICS_ENDPOINT_ENV_VARS,
        setup.CONSOLE_METRICS_ENV,
    )
}


class _ConfigureTestCase(SimpleTestCase):
    """Base for ``configure`` tests: patches both halves of the provider
    accessor (``configure`` reads the provider back to verify its install) and
    restores the idempotence flag."""

    def setUp(self):
        super().setUp()
        self.registered = None

        def _set(provider):
            self.registered = provider

        self.set_provider = self.enterContext(
            mock.patch.object(trace, "set_tracer_provider", side_effect=_set)
        )
        self.enterContext(
            mock.patch.object(trace, "get_tracer_provider", side_effect=lambda: self.registered)
        )
        self.build_processor = self.enterContext(
            mock.patch.object(setup, "_build_otlp_span_processor", autospec=True)
        )
        # Neutral argv: the real one is `manage.py test`, which
        # `configure_from_env` refuses outright.
        self.enterContext(mock.patch.object(sys, "argv", ["manage.py", "migrate"]))
        setup._reset_for_tests()
        self.addCleanup(setup._reset_for_tests)


class NoEndpointTests(_ConfigureTestCase):
    """With no OTLP endpoint configured, nothing is installed and nothing breaks."""

    def test_configure_from_env_installs_nothing(self):
        with mock.patch.dict(os.environ, _TRACING_ENV, clear=False):
            self.assertFalse(telemetry.configure_from_env())
        self.assertFalse(setup.is_installed())
        self.set_provider.assert_not_called()
        self.build_processor.assert_not_called()

    def test_a_whitespace_only_endpoint_counts_as_unset(self):
        with mock.patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": "   "}, clear=False):
            self.assertIsNone(telemetry.otlp_endpoint())
            self.assertFalse(telemetry.configure_from_env())

    def test_either_endpoint_variable_switches_tracing_on(self):
        for name in setup.ENDPOINT_ENV_VARS:
            with self.subTest(variable=name):
                env = dict(_TRACING_ENV, **{name: "http://phoenix:6006"})
                with mock.patch.dict(os.environ, env, clear=False):
                    self.assertEqual(telemetry.otlp_endpoint(), "http://phoenix:6006")

    def test_a_no_op_provider_still_supports_the_whole_span_protocol(self):
        """A no-op tracer supports the whole span protocol, so instrumented code never branches."""
        tracer = trace.get_tracer(__name__, tracer_provider=trace.NoOpTracerProvider())
        with tracer.start_as_current_span("chat some-model") as span:
            self.assertIsInstance(span, trace.NonRecordingSpan)
            self.assertFalse(span.is_recording())
            span.set_attribute("gen_ai.request.model", "some-model")
            span.set_attribute("gen_ai.response.finish_reasons", ["stop"])
            span.set_status(trace.Status(trace.StatusCode.ERROR))
            span.record_exception(RuntimeError("boom"))


class PristineProcessTests(SimpleTestCase):
    """With no endpoint, ``get_tracer()`` is a no-op — only assertable in a
    subprocess, because this suite installs a process-global provider."""

    def test_with_no_endpoint_get_tracer_is_a_no_op(self):
        script = textwrap.dedent(
            """
            import sys
            import django
            django.setup()

            from opentelemetry import trace
            from project.app.services import telemetry
            from project.app.services.telemetry import setup

            assert not setup.is_installed(), "a provider was installed with no endpoint"

            with telemetry.get_tracer().start_as_current_span("plan_lead") as span:
                assert isinstance(span, trace.NonRecordingSpan), type(span)
                assert not span.is_recording()
                span.set_attribute("outreach.lead.id", "lead_007")

            # And nothing from the SDK was even imported on the way here.
            sdk = [m for m in sys.modules if m.startswith("opentelemetry.sdk")]
            assert not sdk, sdk
            print("ok")
            """
        )
        env = dict(os.environ, DJANGO_SETTINGS_MODULE="project.settings")
        env.setdefault("DJANGO_SECRET_KEY", "subprocess-test-key")
        for name in setup.ENDPOINT_ENV_VARS:
            env.pop(name, None)

        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)


class ConfigureTests(_ConfigureTestCase):
    def test_endpoint_set_installs_the_otlp_exporter(self):
        env = dict(_TRACING_ENV, OTEL_EXPORTER_OTLP_ENDPOINT="http://phoenix:6006")
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertTrue(telemetry.configure_from_env())
        self.assertTrue(setup.is_installed())
        self.build_processor.assert_called_once_with()
        self.set_provider.assert_called_once()

    def test_a_second_configure_installs_nothing(self):
        """A second ``configure`` is a no-op — ``AppConfig.ready()`` can fire more than once."""
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
        # The provider is a real SDK object, so this exercises the production injection seam.
        self.assertIn(processor, provider._active_span_processor._span_processors)

    def test_a_refused_registration_is_reported_as_a_refusal(self):
        """``set_tracer_provider`` silently keeps an incumbent; ``configure`` must report that as a refusal."""
        incumbent = object()
        with mock.patch.object(trace, "get_tracer_provider", return_value=incumbent):
            self.assertFalse(telemetry.configure(mock.Mock()))
        self.assertFalse(setup.is_installed())

    def test_the_discarded_provider_is_shut_down(self):
        """A refused provider is shut down so its processor's thread and buffer don't leak."""
        incumbent = object()
        processor = mock.Mock()
        with mock.patch.object(trace, "get_tracer_provider", return_value=incumbent):
            telemetry.configure(processor)
        processor.shutdown.assert_called_once()

    def test_the_provider_does_not_register_its_own_atexit_handler(self):
        """The SDK's own atexit handler stays off; shutdown is registered once, by us."""
        telemetry.configure(mock.Mock())
        provider = self.set_provider.call_args.args[0]
        self.assertIsNone(provider._atexit_handler)


class ConfigurationFailureTests(_ConfigureTestCase):
    """A mistyped OTEL_* variable costs telemetry, never the application."""

    def setUp(self):
        super().setUp()
        self.env = dict(_TRACING_ENV, OTEL_EXPORTER_OTLP_ENDPOINT="http://phoenix:6006")

    def test_a_broken_exporter_configuration_does_not_stop_the_app(self):
        """A broken exporter configuration (e.g. ``OTEL_EXPORTER_OTLP_TIMEOUT=10s``) must not stop boot."""
        self.build_processor.side_effect = ValueError("could not convert string to float: '10s'")
        with mock.patch.dict(os.environ, self.env, clear=False):
            self.assertFalse(telemetry.configure_from_env())
        self.assertFalse(setup.is_installed())

    def test_configure_itself_still_raises(self):
        """The rescue lives on the boot path only; ``configure()`` stays strict."""
        self.build_processor.side_effect = ValueError("boom")
        with self.assertRaises(ValueError):
            telemetry.configure()


class ResourceTests(_ConfigureTestCase):
    def _installed_resource(self):
        telemetry.configure(mock.Mock())
        return self.set_provider.call_args.args[0].resource

    def test_service_name_defaults_when_the_environment_is_silent(self):
        with mock.patch.dict(os.environ, {"OTEL_SERVICE_NAME": ""}, clear=False):
            resource = self._installed_resource()
        self.assertEqual(resource.attributes["service.name"], telemetry.DEFAULT_SERVICE_NAME)

    def test_otel_service_name_wins_over_the_default(self):
        """The operator's ``OTEL_SERVICE_NAME`` wins over our default."""
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
        """``RUN_MAIN`` is unset for gunicorn and management commands — they must still install."""
        for argv in (
            ["gunicorn", "project.wsgi"],
            ["manage.py", "shell"],
            ["manage.py", "migrate"],
        ):
            with self.subTest(argv=argv):
                setup._reset_for_tests()
                self.assertTrue(self._configure(argv))


class TestRunnerTests(_ConfigureTestCase):
    """``manage.py test`` must never install a live exporter, even with an
    OTLP endpoint exported in the developer's shell."""

    def _configure(self, argv):
        env = dict(_TRACING_ENV, OTEL_EXPORTER_OTLP_ENDPOINT="http://phoenix:6006")
        with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, env, clear=False):
            return telemetry.configure_from_env()

    def test_the_test_runner_does_not_install(self):
        self.assertFalse(self._configure(["manage.py", "test", "project.app"]))
        self.set_provider.assert_not_called()
        self.build_processor.assert_not_called()

    def test_an_app_label_containing_test_does_not_switch_telemetry_off(self):
        """The test-runner guard matches at the command position, not anywhere in argv."""
        self.assertTrue(self._configure(["gunicorn", "--config", "test_settings.py"]))


class RecordedSpanTests(RecordingTestCase):
    """End to end through the real API: a provider is installed, spans arrive."""

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
