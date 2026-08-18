"""The planner's runtime knobs (MUS-26a): defaults, overrides, rejections that
name the setting at ``manage.py check`` time, and Django-free imports."""

import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from unittest import mock

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from project import settings as project_settings
from project.app import checks
from project.app.services.llm import retry, runtime


class PlannerRuntimeDefaultsTests(SimpleTestCase):
    """The defaults, and the two places they are written down."""

    def test_defaults_apply_when_nothing_is_configured(self):
        # No env vars in the test environment, so settings.py's defaults apply.
        policy = runtime.get_retry_policy()
        timeouts = runtime.get_timeouts()

        self.assertEqual(runtime.get_max_in_flight(), 8)
        self.assertEqual(policy.max_attempts, 4)
        self.assertEqual(policy.initial_backoff_s, 0.5)
        self.assertEqual(policy.max_backoff_s, 30.0)
        self.assertEqual(policy.multiplier, 2.0)
        self.assertEqual(timeouts.request_s, 60.0)
        self.assertEqual(timeouts.per_lead_s, 150.0)

    def test_settings_defaults_match_the_module_level_fallbacks(self):
        """The literals in settings.py and the fallbacks in runtime.py must not
        drift (settings cannot import app code, so both copies exist)."""
        self.assertEqual(settings.OUTREACH_MAX_IN_FLIGHT, runtime.DEFAULT_MAX_IN_FLIGHT)
        self.assertEqual(settings.OUTREACH_MAX_ATTEMPTS, retry.DEFAULT_MAX_ATTEMPTS)
        self.assertEqual(settings.OUTREACH_INITIAL_BACKOFF_S, retry.DEFAULT_INITIAL_BACKOFF_S)
        self.assertEqual(settings.OUTREACH_MAX_BACKOFF_S, retry.DEFAULT_MAX_BACKOFF_S)
        self.assertEqual(settings.OUTREACH_BACKOFF_MULTIPLIER, retry.DEFAULT_MULTIPLIER)
        self.assertEqual(settings.OUTREACH_REQUEST_TIMEOUT_S, runtime.DEFAULT_REQUEST_TIMEOUT_S)
        self.assertEqual(settings.OUTREACH_PER_LEAD_TIMEOUT_S, runtime.DEFAULT_PER_LEAD_TIMEOUT_S)

    def test_a_missing_setting_falls_back_instead_of_exploding(self):
        # A settings module predating MUS-26 must still boot. `self.settings()`
        # restores the settings object on exit, so deleting inside it is safe.
        with self.settings():
            del settings.OUTREACH_MAX_IN_FLIGHT
            del settings.OUTREACH_PER_LEAD_TIMEOUT_S
            del settings.OUTREACH_BACKOFF_MULTIPLIER

            self.assertEqual(runtime.get_max_in_flight(), runtime.DEFAULT_MAX_IN_FLIGHT)
            self.assertEqual(runtime.get_timeouts().per_lead_s, runtime.DEFAULT_PER_LEAD_TIMEOUT_S)
            self.assertEqual(runtime.get_retry_policy().multiplier, retry.DEFAULT_MULTIPLIER)


class PlannerRuntimeOverrideTests(SimpleTestCase):
    """An override has to reach the dataclass, not just the settings object."""

    @override_settings(
        OUTREACH_MAX_ATTEMPTS=7,
        OUTREACH_INITIAL_BACKOFF_S=0.125,
        OUTREACH_MAX_BACKOFF_S=5.0,
        OUTREACH_BACKOFF_MULTIPLIER=3.0,
    )
    def test_retry_policy_reads_every_setting(self):
        policy = runtime.get_retry_policy()

        self.assertEqual(policy.max_attempts, 7)
        self.assertEqual(policy.initial_backoff_s, 0.125)
        self.assertEqual(policy.max_backoff_s, 5.0)
        self.assertEqual(policy.multiplier, 3.0)

    @override_settings(OUTREACH_REQUEST_TIMEOUT_S=2.5, OUTREACH_PER_LEAD_TIMEOUT_S=9.0)
    def test_timeouts_read_every_setting(self):
        timeouts = runtime.get_timeouts()

        self.assertEqual(timeouts.request_s, 2.5)
        self.assertEqual(timeouts.per_lead_s, 9.0)

    @override_settings(OUTREACH_MAX_IN_FLIGHT=32)
    def test_max_in_flight_reads_its_setting(self):
        self.assertEqual(runtime.get_max_in_flight(), 32)

    @override_settings(OUTREACH_MAX_ATTEMPTS=1)
    def test_one_attempt_is_a_legal_configuration(self):
        # The way to switch retries off; distinct from the rejected 0.
        self.assertEqual(runtime.get_retry_policy().max_attempts, 1)

    @override_settings(OUTREACH_MAX_BACKOFF_S=30)
    def test_integers_are_accepted_where_a_float_is_expected(self):
        # A custom settings module may well write a bare int.
        policy = runtime.get_retry_policy()
        self.assertIsInstance(policy.max_backoff_s, float)
        self.assertEqual(policy.max_backoff_s, 30.0)


class PlannerRuntimeValidationTests(SimpleTestCase):
    """Every rejection names the environment variable that caused it."""

    def assertRejects(self, setting, value, *, accessor):
        with override_settings(**{setting: value}):
            with self.assertRaises(ImproperlyConfigured) as caught:
                accessor()
        message = str(caught.exception)
        self.assertIn(setting, message)
        self.assertIn(repr(value), message)

    def test_max_in_flight_below_one_is_rejected(self):
        self.assertRejects("OUTREACH_MAX_IN_FLIGHT", 0, accessor=runtime.get_max_in_flight)

    def test_negative_max_in_flight_is_rejected(self):
        self.assertRejects("OUTREACH_MAX_IN_FLIGHT", -4, accessor=runtime.get_max_in_flight)

    def test_max_attempts_below_one_is_rejected(self):
        self.assertRejects("OUTREACH_MAX_ATTEMPTS", 0, accessor=runtime.get_retry_policy)

    def test_negative_initial_backoff_is_rejected(self):
        self.assertRejects("OUTREACH_INITIAL_BACKOFF_S", -0.5, accessor=runtime.get_retry_policy)

    def test_negative_max_backoff_is_rejected(self):
        self.assertRejects("OUTREACH_MAX_BACKOFF_S", -1.0, accessor=runtime.get_retry_policy)

    def test_multiplier_below_one_is_rejected(self):
        # A multiplier under 1 makes the backoff *shrink* -- a retry storm.
        self.assertRejects("OUTREACH_BACKOFF_MULTIPLIER", 0.5, accessor=runtime.get_retry_policy)

    def test_zero_request_timeout_is_rejected(self):
        # Zero is not "no deadline" -- asyncio.timeout(0) expires immediately.
        self.assertRejects("OUTREACH_REQUEST_TIMEOUT_S", 0.0, accessor=runtime.get_timeouts)

    def test_zero_per_lead_timeout_is_rejected(self):
        self.assertRejects("OUTREACH_PER_LEAD_TIMEOUT_S", 0.0, accessor=runtime.get_timeouts)

    def test_negative_per_lead_timeout_is_rejected(self):
        self.assertRejects("OUTREACH_PER_LEAD_TIMEOUT_S", -30.0, accessor=runtime.get_timeouts)

    def test_a_non_numeric_value_is_rejected(self):
        self.assertRejects("OUTREACH_MAX_IN_FLIGHT", "eight", accessor=runtime.get_max_in_flight)

    def test_a_non_numeric_float_setting_is_rejected(self):
        self.assertRejects("OUTREACH_REQUEST_TIMEOUT_S", "sixty", accessor=runtime.get_timeouts)

    def test_a_boolean_is_not_silently_read_as_one(self):
        # bool is an int subclass in Python: True would otherwise configure a
        # semaphore of 1 and quietly serialize the entire run.
        self.assertRejects("OUTREACH_MAX_IN_FLIGHT", True, accessor=runtime.get_max_in_flight)

    def test_an_absurd_pool_size_is_rejected(self):
        # `>= 1` bounds one end of the range; the ceiling bounds the other.
        self.assertRejects("OUTREACH_MAX_IN_FLIGHT", 80000, accessor=runtime.get_max_in_flight)

    @override_settings(OUTREACH_MAX_IN_FLIGHT=runtime.MAX_IN_FLIGHT_CEILING)
    def test_the_ceiling_itself_is_allowed(self):
        self.assertEqual(runtime.get_max_in_flight(), runtime.MAX_IN_FLIGHT_CEILING)

    def test_a_per_lead_budget_shorter_than_one_attempt_is_rejected(self):
        # Two plausible numbers that together fail every lead.
        with override_settings(OUTREACH_REQUEST_TIMEOUT_S=600.0):
            self.assertRejects("OUTREACH_PER_LEAD_TIMEOUT_S", 5.0, accessor=runtime.get_timeouts)

    @override_settings(OUTREACH_REQUEST_TIMEOUT_S=30.0, OUTREACH_PER_LEAD_TIMEOUT_S=30.0)
    def test_equal_deadlines_are_allowed(self):
        # "at least", not "greater than": one attempt, no retry budget.
        timeouts = runtime.get_timeouts()
        self.assertEqual(timeouts.request_s, timeouts.per_lead_s)


class TimeoutsDataclassTests(SimpleTestCase):
    """Timeouts validates itself, independently of the settings accessor."""

    def test_defaults(self):
        timeouts = runtime.Timeouts()
        self.assertEqual(timeouts.request_s, runtime.DEFAULT_REQUEST_TIMEOUT_S)
        self.assertEqual(timeouts.per_lead_s, runtime.DEFAULT_PER_LEAD_TIMEOUT_S)

    def test_is_frozen(self):
        timeouts = runtime.Timeouts()
        with self.assertRaises(FrozenInstanceError):
            timeouts.request_s = 1.0  # type: ignore[misc]

    def test_rejects_a_non_positive_request_timeout(self):
        with self.assertRaises(ValueError):
            runtime.Timeouts(request_s=0.0)

    def test_rejects_a_non_positive_per_lead_timeout(self):
        with self.assertRaises(ValueError):
            runtime.Timeouts(per_lead_s=-1.0)

    def test_rejects_a_per_lead_budget_shorter_than_one_attempt(self):
        with self.assertRaises(ValueError):
            runtime.Timeouts(request_s=600.0, per_lead_s=5.0)


class PlannerRuntimeAggregateTests(SimpleTestCase):
    """One run, one resolution."""

    @override_settings(
        OUTREACH_MAX_IN_FLIGHT=5,
        OUTREACH_MAX_ATTEMPTS=2,
        OUTREACH_REQUEST_TIMEOUT_S=10.0,
        OUTREACH_PER_LEAD_TIMEOUT_S=20.0,
    )
    def test_it_carries_all_three(self):
        resolved = runtime.get_planner_runtime()

        self.assertEqual(resolved.max_in_flight, 5)
        self.assertEqual(resolved.retry.max_attempts, 2)
        self.assertEqual(resolved.timeouts.request_s, 10.0)
        self.assertEqual(resolved.timeouts.per_lead_s, 20.0)

    def test_is_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            runtime.get_planner_runtime().max_in_flight = 1  # type: ignore[misc]


class BootTimeCheckTests(SimpleTestCase):
    """A bad knob must fail `manage.py check`, not a user's planner run."""

    def test_a_healthy_configuration_reports_nothing(self):
        self.assertEqual(checks.planner_runtime_check(None), [])

    @override_settings(OUTREACH_BACKOFF_MULTIPLIER=0.5)
    def test_a_bad_value_becomes_a_check_error_naming_the_setting(self):
        errors = checks.planner_runtime_check(None)

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].id, "app.E002")
        # The check reuses the accessor, so the message is the accessor's.
        self.assertIn("OUTREACH_BACKOFF_MULTIPLIER", errors[0].msg)
        self.assertIn("0.5", errors[0].msg)

    @override_settings(OUTREACH_MAX_IN_FLIGHT=0)
    def test_it_catches_the_pool_size_too(self):
        errors = checks.planner_runtime_check(None)
        self.assertIn("OUTREACH_MAX_IN_FLIGHT", errors[0].msg)


class EnvParsingTests(SimpleTestCase):
    """`settings.py`'s environment read must not be the one place an operator
    gets an unhelpful error."""

    def test_a_blank_value_reads_as_unset(self):
        # `.env.example` ships these pre-filled, so blanking a line out is the
        # obvious way to take the default; it is also what `${VAR:-}` produces.
        with mock.patch.dict(os.environ, {"OUTREACH_MAX_IN_FLIGHT": "   "}):
            self.assertEqual(project_settings._env_int("OUTREACH_MAX_IN_FLIGHT", 8), 8)

    def test_a_set_value_wins_over_the_default(self):
        with mock.patch.dict(os.environ, {"OUTREACH_MAX_BACKOFF_S": "12.5"}):
            self.assertEqual(project_settings._env_float("OUTREACH_MAX_BACKOFF_S", 30.0), 12.5)

    def test_a_non_numeric_value_names_the_variable(self):
        with mock.patch.dict(os.environ, {"OUTREACH_MAX_IN_FLIGHT": "eight"}):
            with self.assertRaises(ImproperlyConfigured) as caught:
                project_settings._env_int("OUTREACH_MAX_IN_FLIGHT", 8)

        message = str(caught.exception)
        self.assertIn("OUTREACH_MAX_IN_FLIGHT", message)
        self.assertIn("whole number", message)
        self.assertIn("'eight'", message)

    def test_a_non_numeric_float_names_the_variable(self):
        with mock.patch.dict(os.environ, {"OUTREACH_MAX_BACKOFF_S": "soon"}):
            with self.assertRaises(ImproperlyConfigured) as caught:
                project_settings._env_float("OUTREACH_MAX_BACKOFF_S", 30.0)
        self.assertIn("OUTREACH_MAX_BACKOFF_S", str(caught.exception))


class DjangoFreeImportTests(SimpleTestCase):
    """Every module under ``services/llm/`` must import with no Django settings
    configured -- ``evals/run_rules_eval.py`` and the retry unit tests need it."""

    def test_the_llm_package_imports_without_django_configured(self):
        env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import project.app.services.llm.runtime, "
                "project.app.services.llm.retry, "
                "project.app.services.outreach",
            ],
            cwd=settings.BASE_DIR,
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
