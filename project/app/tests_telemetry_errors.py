"""Typed error handling in the planner, and how it reaches a span (MUS-25, 25-d).

The blanket ``except Exception`` this replaces caught a 429 and a
``ZeroDivisionError`` in our own prompt handling identically. Both became "copy
generation failed", both landed on a dashboard as the same undifferentiated bar,
and the only way to tell them apart was to read the free-text message a human
had to notice first.

Two axes come out of the taxonomy, and the tests below are organised around the
fact that they are *different* axes:

* ``error.type`` / ``outreach.failure.kind`` — what happened, one class name.
* ``outreach.failure.domain`` — whose problem it is. A timeout and a 500 are
  both retryable and both the provider's; an auth failure and a 400 are both
  non-retryable but only one of them is a credential problem. Collapsing the
  two would lose exactly the distinction an on-call reader wants.

The residual case has its own class, :class:`LLMUnexpectedError`, so "we did not
classify this" is itself a classification rather than a silent lie.
"""

from unittest import mock

from django.test import SimpleTestCase, TestCase
from opentelemetry import trace

from project.app.models import Lead, OutreachAction
from project.app.services.llm import errors
from project.app.services.outreach import plan_outreach
from project.app.services.telemetry import genai, semconv

from .tests_telemetry_planner import GOOD_COPY, _PlannerSpanTestCase

# Every class in the taxonomy, with the two facts a span reports about it and
# the retryability the retry helper reads. One table, checked against the
# classes themselves -- so a new error class with no domain declared shows up as
# a failure here rather than as an "unknown" bar on a chart six months later.
TAXONOMY = [
    (errors.LLMRateLimitError, "LLMRateLimitError", errors.FAULT_PROVIDER, True),
    (errors.LLMTimeoutError, "LLMTimeoutError", errors.FAULT_PROVIDER, True),
    (errors.LLMTransientError, "LLMTransientError", errors.FAULT_PROVIDER, True),
    (errors.LLMAuthError, "LLMAuthError", errors.FAULT_CONFIGURATION, False),
    (errors.LLMBadRequestError, "LLMBadRequestError", errors.FAULT_CONFIGURATION, False),
    (errors.LLMMalformedResponseError, "LLMMalformedResponseError", errors.FAULT_CONTRACT, False),
]


class TaxonomyTests(SimpleTestCase):
    def test_every_class_declares_a_kind_a_domain_and_a_retryability(self):
        for cls, kind, domain, retryable in TAXONOMY:
            with self.subTest(cls=cls.__name__):
                exc = cls("boom", provider="groq")
                self.assertEqual(genai.error_type(exc), kind)
                self.assertEqual(genai.fault_domain(exc), domain)
                self.assertEqual(exc.retryable, retryable)

    def test_the_table_covers_every_class_the_module_exports(self):
        """Otherwise a seventh error class could be added and quietly report
        ``unknown`` on every dashboard."""
        exported = {
            getattr(errors, name)
            for name in errors.__all__
            if isinstance(getattr(errors, name), type)
            and issubclass(getattr(errors, name), Exception)
        }
        covered = {cls for cls, _, _, _ in TAXONOMY}
        # The base and the residual wrapper are deliberately outside the table:
        # both mean "unclassified", which is what the default already says.
        self.assertEqual(exported - covered, {errors.LLMError, errors.LLMUnexpectedError})

    def test_the_two_axes_are_genuinely_independent(self):
        """A timeout and a 500 are both retryable and both the provider's; an
        auth failure and a 400 are both non-retryable but only one is a
        credential problem. If retryability determined the domain, one of these
        would be redundant -- and the wrong one would be dropped."""
        by_retryable = {}
        for cls, _, domain, retryable in TAXONOMY:
            by_retryable.setdefault(retryable, set()).add(domain)
        self.assertEqual(by_retryable[False], {errors.FAULT_CONFIGURATION, errors.FAULT_CONTRACT})

    def test_an_unclassified_exception_is_unknown_not_assumed(self):
        self.assertEqual(genai.fault_domain(ValueError("bug")), errors.FAULT_UNKNOWN)
        self.assertEqual(genai.fault_domain(errors.LLMError("residual")), errors.FAULT_UNKNOWN)


class WrapUnexpectedTests(SimpleTestCase):
    def test_an_already_typed_error_passes_through_untouched(self):
        """The whole value of the taxonomy is that the adapter's class survives
        to the span. Re-wrapping would flatten every provider failure into one."""
        original = errors.LLMRateLimitError("throttled", provider="groq")
        self.assertIs(errors.wrap_unexpected(original), original)

    def test_the_wrapper_reports_the_original_class_not_its_own(self):
        """Reporting ``LLMUnexpectedError`` would make every genuine bug
        indistinguishable from every other -- the exact problem the taxonomy was
        introduced to fix, reintroduced one level up."""
        wrapped = errors.wrap_unexpected(ZeroDivisionError("division by zero"))
        self.assertIsInstance(wrapped, errors.LLMError)
        self.assertEqual(genai.error_type(wrapped), "ZeroDivisionError")
        self.assertEqual(genai.fault_domain(wrapped), errors.FAULT_UNKNOWN)

    def test_the_wrapper_is_not_retryable(self):
        """We do not know what it was, so retrying it is a guess -- and a guess
        that costs the full backoff budget before failing the same way."""
        self.assertFalse(errors.wrap_unexpected(ValueError("bug")).retryable)

    def test_the_message_is_the_original_untouched(self):
        """This string reaches ``further_action``, which a BD reviewer reads
        while deciding what to do about one lead. The class name is noise there
        and belongs on the span instead."""
        wrapped = errors.wrap_unexpected(RuntimeError("provider exploded"))
        self.assertEqual(str(wrapped), "provider exploded")

    def test_an_empty_message_falls_back_to_the_class_name(self):
        self.assertEqual(str(errors.wrap_unexpected(ValueError())), "ValueError")

    def test_the_original_is_kept_as_the_cause(self):
        original = KeyError("choices")
        self.assertIs(errors.wrap_unexpected(original).cause, original)


class PlannerErrorSpanTests(_PlannerSpanTestCase):
    """Each class reaches the lead span as itself, with its domain beside it."""

    def _plan_failing_with(self, exc):
        self.make_lead()
        self.plan(side_effect=exc)
        return self.lead_span()

    def test_each_class_reaches_the_span_as_itself(self):
        for cls, kind, domain, _ in TAXONOMY:
            with self.subTest(cls=cls.__name__):
                Lead.objects.all().delete()
                OutreachAction.objects.all().delete()
                self.exporter.clear()

                span = self._plan_failing_with(cls("boom", provider="groq"))
                self.assertEqual(span.attributes[semconv.FAILURE_KIND], kind)
                self.assertEqual(span.attributes[semconv.FAILURE_DOMAIN], domain)
                self.assertEqual(span.status.status_code, trace.StatusCode.ERROR)
                self.assertEqual(
                    span.attributes[semconv.VERIFY_OUTCOME], semconv.VERIFY_NOT_GENERATED
                )

    def test_an_unexpected_exception_fails_closed_to_a_human(self):
        """The residual clause still has to hold the old guarantee: one lead's
        bug must not sink a 200-lead run."""
        span = self._plan_failing_with(ZeroDivisionError("division by zero"))

        self.assertEqual(span.attributes[semconv.FAILURE_KIND], "ZeroDivisionError")
        self.assertEqual(span.attributes[semconv.FAILURE_DOMAIN], errors.FAULT_UNKNOWN)
        row = OutreachAction.objects.get()
        self.assertTrue(row.needs_human)
        self.assertIn("division by zero", row.further_action)

    def test_a_successful_lead_carries_no_failure_attributes(self):
        self.make_lead()
        self.plan(copy=GOOD_COPY)
        span = self.lead_span()
        self.assertNotIn(semconv.FAILURE_KIND, span.attributes)
        self.assertNotIn(semconv.FAILURE_DOMAIN, span.attributes)

    def test_one_lead_failing_does_not_stop_the_others(self):
        """The guarantee the blanket except was there for, kept."""
        self.make_lead(id="lead_ok")
        # Keyed on the contact name because that is what actually appears in
        # the prompt -- the lead id does not.
        self.make_lead(id="lead_bad", contact_name="Doomed Contact")

        async def sometimes(*_args, **kwargs):
            if "Doomed Contact" in kwargs.get("prompt", ""):
                raise errors.LLMTimeoutError("read timeout", provider="groq")
            return GOOD_COPY

        with mock.patch("project.app.services.outreach.agenerate_copy", sometimes):
            plan_outreach()

        self.assertEqual(OutreachAction.objects.count(), 2)
        self.assertEqual(OutreachAction.objects.filter(needs_human=True).count(), 1)


class ClientResolutionErrorTests(TestCase):
    """A bad configuration is one failure per lead, not a dead run."""

    def test_an_unknown_provider_is_wrapped_rather_than_escaping_untyped(self):
        from project.app.services.outreach import _resolve_client

        item = mock.Mock(prompt="a prompt")
        with mock.patch(
            "project.app.services.outreach.get_llm_client", side_effect=ValueError("bogus")
        ):
            client, error = _resolve_client([item])

        self.assertIsNone(client)
        self.assertIsInstance(error, errors.LLMError)
        self.assertEqual(genai.error_type(error), "ValueError")

    def test_a_missing_key_arrives_already_classified(self):
        """``LLMAuthError`` comes out of the adapter's constructor, so it
        reaches the span as a configuration fault rather than as ``unknown``."""
        from project.app.services.outreach import _resolve_client

        item = mock.Mock(prompt="a prompt")
        with mock.patch(
            "project.app.services.outreach.get_llm_client",
            side_effect=errors.LLMAuthError("GROQ_API_KEY is not set", provider="groq"),
        ):
            _client, error = _resolve_client([item])

        self.assertEqual(genai.error_type(error), "LLMAuthError")
        self.assertEqual(genai.fault_domain(error), errors.FAULT_CONFIGURATION)

    def test_a_run_needing_no_copy_never_resolves_a_client(self):
        """Unchanged by the typed handling, and worth pinning: a run of purely
        unmatched leads must still contact no configuration at all."""
        from project.app.services.outreach import _resolve_client

        item = mock.Mock(prompt=None)
        with mock.patch("project.app.services.outreach.get_llm_client") as get_client:
            self.assertEqual(_resolve_client([item]), (None, None))
        get_client.assert_not_called()
