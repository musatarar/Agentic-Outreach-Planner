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

# Every environment variable that can switch telemetry on, cleared together. A
# test that clears only some of them passes for the wrong reason on a machine
# where the others are exported -- and the metrics ones matter most, because a
# leaked OTEL_EXPORTER_OTLP_METRICS_ENDPOINT would have the suite construct a
# real network exporter.
_TRACING_ENV = {
    name: ""
    for name in (
        *setup.ENDPOINT_ENV_VARS,
        *setup.METRICS_ENDPOINT_ENV_VARS,
        setup.CONSOLE_METRICS_ENV,
    )
}


class _ConfigureTestCase(SimpleTestCase):
    """Base for tests that call ``configure``/``configure_from_env``.

    Patches out the global provider install and always restores the idempotence
    flag, so these tests can run in any order relative to the ones that want
    real recorded spans.

    Both halves of the API's provider accessor are patched, not just the setter.
    ``configure`` verifies its install by reading the provider back — a real
    behaviour, because ``set_tracer_provider`` silently refuses a second
    registration — so a stub setter with a live getter would make every install
    here look refused.
    """

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
        # A neutral argv. The real one is `manage.py test ...`, which
        # `configure_from_env` refuses outright -- so without this every test
        # below would pass or fail for that reason instead of its own. Classes
        # that care about argv (autoreload, test runner) patch it themselves.
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

    def test_a_no_op_provider_still_supports_the_whole_span_protocol(self):
        """The library guarantee the no-branching design rests on.

        A tracer from the API's no-op provider supports every span call --
        ``set_attribute`` and ``set_status`` are empty method bodies rather than
        errors -- which is why the planner can open spans and set attributes
        unconditionally. Asserting it here means a future OTel upgrade that
        changed it would be caught by name.
        """
        tracer = trace.get_tracer(__name__, tracer_provider=trace.NoOpTracerProvider())
        with tracer.start_as_current_span("chat some-model") as span:
            self.assertIsInstance(span, trace.NonRecordingSpan)
            self.assertFalse(span.is_recording())
            span.set_attribute("gen_ai.request.model", "some-model")
            span.set_attribute("gen_ai.response.finish_reasons", ["stop"])
            span.set_status(trace.Status(trace.StatusCode.ERROR))
            span.record_exception(RuntimeError("boom"))


class PristineProcessTests(SimpleTestCase):
    """The claim that can only be tested in a process nothing has touched.

    ``get_tracer()`` reads a *process-global* provider, and this suite installs
    an in-memory one. So "with no endpoint, our own ``get_tracer()`` yields a
    ``NonRecordingSpan``" is not assertable in-process at all — asserting it
    against a hand-built ``NoOpTracerProvider`` would be testing OpenTelemetry,
    not us. A subprocess is the only honest way to make the claim, so it is
    worth the second or so it costs.
    """

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

    def test_a_refused_registration_is_reported_as_a_refusal(self):
        """``set_tracer_provider`` does not raise when one is already
        registered -- it logs and keeps the old one. Believing our own flag over
        that would let ``is_installed()`` say ``True`` while ``get_tracer()``
        resolved against something else entirely, and would leave an
        unreachable provider holding an ``atexit`` handler."""
        incumbent = object()
        with mock.patch.object(trace, "get_tracer_provider", return_value=incumbent):
            self.assertFalse(telemetry.configure(mock.Mock()))
        self.assertFalse(setup.is_installed())

    def test_the_discarded_provider_is_shut_down(self):
        """It owns a span processor. Dropping the reference without shutting it
        down leaks whatever thread and buffer that processor started."""
        incumbent = object()
        processor = mock.Mock()
        with mock.patch.object(trace, "get_tracer_provider", return_value=incumbent):
            telemetry.configure(processor)
        processor.shutdown.assert_called_once()

    def test_the_provider_does_not_register_its_own_atexit_handler(self):
        """One handler, registered here beside its reasoning -- not two, one of
        which is the SDK's and invisible from this file."""
        telemetry.configure(mock.Mock())
        provider = self.set_provider.call_args.args[0]
        self.assertIsNone(provider._atexit_handler)


class ConfigurationFailureTests(_ConfigureTestCase):
    """A mistyped OTEL_* variable costs telemetry, never the application."""

    def setUp(self):
        super().setUp()
        self.env = dict(_TRACING_ENV, OTEL_EXPORTER_OTLP_ENDPOINT="http://phoenix:6006")

    def test_a_broken_exporter_configuration_does_not_stop_the_app(self):
        """``OTEL_EXPORTER_OTLP_TIMEOUT=10s`` is a natural thing to write and is
        parsed as a float by the exporter's constructor. Unhandled, it takes
        down ``manage.py check``, ``migrate`` and ``runserver`` alike -- and the
        Docker entrypoint runs all three, so the container never boots."""
        self.build_processor.side_effect = ValueError("could not convert string to float: '10s'")
        with mock.patch.dict(os.environ, self.env, clear=False):
            self.assertFalse(telemetry.configure_from_env())
        self.assertFalse(setup.is_installed())

    def test_configure_itself_still_raises(self):
        """The rescue lives on the boot path only. ``configure()`` stays strict
        so a test that breaks the exporter sees the breakage."""
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
        ``manage.py migrate`` and every management command, so keying on it
        alone would disable telemetry everywhere except ``runserver``."""
        for argv in (
            ["gunicorn", "project.wsgi"],
            ["manage.py", "shell"],
            ["manage.py", "migrate"],
        ):
            with self.subTest(argv=argv):
                setup._reset_for_tests()
                self.assertTrue(self._configure(argv))


class TestRunnerTests(_ConfigureTestCase):
    """``manage.py test`` must never install a live exporter.

    ``ready()`` runs for the test runner like every other entrypoint. A
    developer with ``OTEL_EXPORTER_OTLP_ENDPOINT`` exported -- exactly what
    ``docker compose up`` teaches them to set -- would otherwise have the suite
    ship every test span, lead ids and content digests included, to whatever
    backend their shell happens to point at. It would also break the suite,
    because the tests install their own in-memory provider and the API refuses
    the second registration.
    """

    def _configure(self, argv):
        env = dict(_TRACING_ENV, OTEL_EXPORTER_OTLP_ENDPOINT="http://phoenix:6006")
        with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, env, clear=False):
            return telemetry.configure_from_env()

    def test_the_test_runner_does_not_install(self):
        self.assertFalse(self._configure(["manage.py", "test", "project.app"]))
        self.set_provider.assert_not_called()
        self.build_processor.assert_not_called()

    def test_an_app_label_containing_test_does_not_switch_telemetry_off(self):
        """Matched at the command position, not anywhere in argv -- otherwise
        `gunicorn --config test.py` would silently lose tracing in production."""
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
