"""Tests for the planner's runtime knobs (MUS-26a).

Three things are worth pinning here and nothing else is:

1. the defaults an unconfigured deployment gets,
2. that an override actually reaches the dataclass (a settings accessor that
   silently ignores its setting is the classic way this goes wrong), and
3. that a bad value is rejected with the *setting name* in the message --
   because the whole reason this accessor exists rather than constructing
   ``RetryPolicy`` inline is to turn "multiplier must be at least 1" into
   something an operator can act on.
"""

from dataclasses import FrozenInstanceError

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from project.app.services.llm import retry, runtime


class PlannerRuntimeDefaultsTests(SimpleTestCase):
    """The defaults, and the two places they are written down."""

    def test_defaults_apply_when_nothing_is_configured(self):
        # No env vars are set in the test environment, so project/settings.py's
        # own defaults are what these accessors see.
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
        """The same numbers live in project/settings.py and in runtime.py.

        Unavoidable: settings cannot import app code at settings-load time, so
        the env-var defaults have to be literals. This test is what stops the
        two copies drifting -- change one and it fails, naming which.
        """
        self.assertEqual(settings.OUTREACH_MAX_IN_FLIGHT, runtime.DEFAULT_MAX_IN_FLIGHT)
        self.assertEqual(settings.OUTREACH_MAX_ATTEMPTS, retry.DEFAULT_MAX_ATTEMPTS)
        self.assertEqual(settings.OUTREACH_INITIAL_BACKOFF_S, retry.DEFAULT_INITIAL_BACKOFF_S)
        self.assertEqual(settings.OUTREACH_MAX_BACKOFF_S, retry.DEFAULT_MAX_BACKOFF_S)
        self.assertEqual(settings.OUTREACH_BACKOFF_MULTIPLIER, retry.DEFAULT_MULTIPLIER)
        self.assertEqual(settings.OUTREACH_REQUEST_TIMEOUT_S, runtime.DEFAULT_REQUEST_TIMEOUT_S)
        self.assertEqual(settings.OUTREACH_PER_LEAD_TIMEOUT_S, runtime.DEFAULT_PER_LEAD_TIMEOUT_S)

    def test_a_missing_setting_falls_back_instead_of_exploding(self):
        # A custom settings module that predates MUS-26 -- or a deployment
        # pointing DJANGO_SETTINGS_MODULE somewhere else -- must still boot.
        # `self.settings()` restores the whole settings object on exit, so
        # deleting inside it is safe.
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
        # "Call it once, surface whatever happens" -- the way to switch retries
        # off. It must not be confused with the rejected 0.
        self.assertEqual(runtime.get_retry_policy().max_attempts, 1)

    @override_settings(OUTREACH_MAX_BACKOFF_S=30)
    def test_integers_are_accepted_where_a_float_is_expected(self):
        # `OUTREACH_MAX_BACKOFF_S=30` in the environment parses to a float via
        # settings.py, but a custom settings module may well write a bare int.
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
        # The offending value too: "must be at least 1" is much less useful
        # without saying what it actually got.
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
        # A multiplier under 1 makes the backoff *shrink* with each attempt,
        # which is a retry storm wearing a backoff's clothes.
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

    def test_a_boolean_is_not_silently_read_as_one(self):
        # bool is an int subclass in Python: True would otherwise configure a
        # semaphore of 1 and quietly serialize the entire run.
        self.assertRejects("OUTREACH_MAX_IN_FLIGHT", True, accessor=runtime.get_max_in_flight)


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
