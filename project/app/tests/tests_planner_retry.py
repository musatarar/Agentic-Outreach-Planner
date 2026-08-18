"""Retries, and what the review queue says when they run out (MUS-26c).

Every test drives the real `agenerate_copy`; the stub is a fake `LLMClient` one
layer below, since patching `agenerate_copy` would skip the retry loop itself.
"""

import asyncio
import re

from django.test import SimpleTestCase, TestCase, override_settings

from project.app.models import Lead, OutreachAction
from project.app.services import actions, outreach
from project.app.services.llm import (
    LLMAuthError,
    LLMBadRequestError,
    LLMError,
    LLMMalformedResponseError,
    LLMRateLimitError,
    LLMResult,
    LLMTimeoutError,
    LLMTransientError,
)
from project.app.services.llm.runtime import RetryPolicy, Timeouts
from project.app.services.outreach import plan_outreach

GOOD_COPY = (
    "Subject: A quick idea for your team\n\n"
    "Hi there,\n\n"
    "You have been steadily working through quotes in the portal, and I wanted "
    "to share one small change that usually helps agencies of your size get "
    "more of them over the line. It takes about fifteen minutes to walk "
    "through, and your producers can start using it the same day. I would "
    "rather show you than write it all out here, since the useful part is "
    "seeing it against your own book of business and your own workflow. Would "
    "you have time for a short call this week?\n\n"
    "Best,\nDana"
)

# Backoff switched off, not shortened: `initial_backoff_s=0` makes every jitter
# draw 0, so these tests pin the retry *count* without sleeping. The schedule
# itself is tested in the retry helper's own suite.
NO_SLEEP = {"OUTREACH_INITIAL_BACKOFF_S": 0.0, "OUTREACH_MAX_BACKOFF_S": 0.0}


class _ScriptedClient:
    """A provider that fails on cue and counts how often it was asked."""

    provider_name = "groq"

    def __init__(self, *errors, then=GOOD_COPY):
        # One entry per attempt, consumed in order; `then` is returned after.
        self.script = list(errors)
        self.then = then
        self.attempts = 0

    async def agenerate(self, prompt, max_tokens=None, timeout=None):
        self.attempts += 1
        if self.script:
            raise self.script.pop(0)
        if isinstance(self.then, BaseException):
            raise self.then
        return LLMResult(text=self.then, provider=self.provider_name, model="scripted-model")

    async def aclose(self):
        return None


class _HangingClient:
    """A provider that answers far too late, for the per-lead budget.

    A *bounded* sleep, so deleting the `asyncio.timeout` fails this suite
    rather than hanging CI. Thirty seconds is past every deadline set here.
    """

    provider_name = "groq"
    HANG_S = 30.0

    def __init__(self):
        self.attempts = 0

    async def agenerate(self, prompt, max_tokens=None, timeout=None):
        self.attempts += 1
        await asyncio.sleep(self.HANG_S)

    async def aclose(self):
        return None


def rate_limit(retry_after=None):
    return LLMRateLimitError(
        "Rate limit reached", provider="groq", status_code=429, retry_after=retry_after
    )


def _lead(lead_id="lead_001", **overrides):
    # demo_completed with no signup date -> complete_onboarding, the one
    # classification that is date-independent.
    defaults = dict(
        id=lead_id,
        agency_name="Summit Risk Advisors",
        contact_name="Priya Nair",
        contact_email="priya.nair@summitrisk.com",
        contact_phone="555-0000",
        state="CO",
        num_producers=4,
        years_in_business=12,
        estimated_book_size_usd=5_000_000,
        stage="demo_completed",
        signed_up_date=None,
    )
    defaults.update(overrides)
    return Lead.objects.create(**defaults)


def _unmatched_lead(lead_id="lead_unknown"):
    return Lead.objects.create(
        id=lead_id,
        agency_name="Nowhere Insurance",
        contact_name="Pat Quinn",
        contact_email="pat@nowhere.test",
        contact_phone="555-0001",
        state="NV",
        num_producers=1,
        years_in_business=1,
        estimated_book_size_usd=0,
        stage="",
        signed_up_date=None,
    )


def _with_client(client):
    from unittest.mock import patch

    return patch("project.app.services.outreach.get_llm_client", return_value=client)


@override_settings(COPY_VERIFY_LEVEL="off", **NO_SLEEP)
class RateLimitIsRetriedTests(TestCase):
    """The ticket's acceptance criterion, in one test."""

    def test_rate_limit_is_retried_not_escalated(self):
        """MUS-26's acceptance criterion: rate limits are retried, not reported
        as needing human review. Two 429s then a success, attempt count pinned."""
        _lead()
        client = _ScriptedClient(rate_limit(), rate_limit(), then=GOOD_COPY)

        with _with_client(client):
            planned = plan_outreach()

        self.assertEqual(len(planned), 1)
        action = OutreachAction.objects.get()
        self.assertFalse(action.needs_human)
        self.assertEqual(action.suggested_copy, GOOD_COPY)
        self.assertEqual(action.further_action, "")
        self.assertEqual(client.attempts, 3)  # two refusals, one success

    def test_a_transient_5xx_is_retried_too(self):
        _lead()
        client = _ScriptedClient(
            LLMTransientError("502 Bad Gateway", provider="groq", status_code=502)
        )

        with _with_client(client):
            plan_outreach()

        self.assertFalse(OutreachAction.objects.get().needs_human)
        self.assertEqual(client.attempts, 2)

    def test_a_hostile_retry_after_header_cannot_park_the_run(self):
        # `retry_after` is capped by OUTREACH_MAX_BACKOFF_S, which is 0 here, so
        # a 300-second header costs this test nothing. The cap is the assertion.
        _lead()
        client = _ScriptedClient(rate_limit(retry_after=300.0))

        with _with_client(client):
            plan_outreach()

        self.assertFalse(OutreachAction.objects.get().needs_human)
        self.assertEqual(client.attempts, 2)


@override_settings(COPY_VERIFY_LEVEL="off", **NO_SLEEP)
class ExhaustedRetriesTests(TestCase):
    """When retries genuinely run out, the row says whose problem it is."""

    @override_settings(OUTREACH_MAX_ATTEMPTS=3)
    def test_the_message_names_the_attempts_the_provider_and_the_kind(self):
        _lead()
        client = _ScriptedClient(rate_limit(), rate_limit(), then=rate_limit())

        with _with_client(client):
            plan_outreach()

        action = OutreachAction.objects.get()
        self.assertEqual(client.attempts, 3)
        self.assertTrue(action.needs_human)
        self.assertEqual(action.suggested_copy, "")

        # Asserted against the constant, never a literal. Only the wall-clock
        # seconds are matched loosely: `re.escape` leaves the alphanumeric
        # sentinel intact, so it survives to be swapped for a number pattern.
        sentinel = "ELAPSEDSECONDS"
        expected = outreach.COPY_RETRIES_EXHAUSTED.format(
            attempts=3,
            elapsed=sentinel,
            provider="groq",
            kind=outreach.FAILURE_KINDS[LLMRateLimitError],
            # `_safe_detail` punctuates, hence the trailing full stop.
            detail="Rate limit reached.",
            action_type=actions.COMPLETE_ONBOARDING,
        )
        self.assertRegex(action.further_action, re.escape(expected).replace(sentinel, r"\d+\.\d"))

    @override_settings(OUTREACH_MAX_ATTEMPTS=2)
    def test_it_says_the_lead_is_not_the_problem(self):
        """The message says the failure is the provider's, not the lead's."""
        _lead()
        client = _ScriptedClient(rate_limit(), then=rate_limit())

        with _with_client(client):
            plan_outreach()

        further = OutreachAction.objects.get().further_action
        self.assertIn("not a problem with this lead", further)
        self.assertIn("Re-run the planner", further)
        # The classification survives in the row itself, not just in the prose.
        self.assertEqual(OutreachAction.objects.get().action_type, actions.COMPLETE_ONBOARDING)
        self.assertNotEqual(OutreachAction.objects.get().reason, "")

    @override_settings(OUTREACH_MAX_ATTEMPTS=4)
    def test_the_attempt_count_in_the_message_is_the_real_one(self):
        # Four configured attempts, all refused.
        _lead()
        client = _ScriptedClient(*[rate_limit()] * 3, then=rate_limit())

        with _with_client(client):
            plan_outreach()

        self.assertEqual(client.attempts, 4)
        self.assertIn(
            outreach.COPY_RETRIES_EXHAUSTED.format(
                attempts=4,
                elapsed="",
                provider="",
                kind="",
                detail="",
                action_type="",
            ).split(" over ")[0],
            OutreachAction.objects.get().further_action,
        )


@override_settings(COPY_VERIFY_LEVEL="off", **NO_SLEEP)
class NonRetryableFailureTests(TestCase):
    """A failure that retrying cannot fix is an engineer's problem, and says so."""

    def test_an_auth_error_is_not_retried(self):
        _lead()
        client = _ScriptedClient(
            LLMAuthError("Invalid API key", provider="groq", status_code=401),
            then=GOOD_COPY,
        )

        with _with_client(client):
            plan_outreach()

        # Exactly one attempt: retrying a bad key earns a longer lockout.
        self.assertEqual(client.attempts, 1)
        action = OutreachAction.objects.get()
        self.assertTrue(action.needs_human)
        self.assertIn("was not retryable", action.further_action)
        self.assertIn(outreach.FAILURE_KINDS[LLMAuthError], action.further_action)
        self.assertIn("an engineer should look at", action.further_action)

    def test_a_bad_request_is_not_retried_either(self):
        _lead()
        client = _ScriptedClient(
            LLMBadRequestError("model not found", provider="groq", status_code=404)
        )

        with _with_client(client):
            plan_outreach()

        self.assertEqual(client.attempts, 1)
        self.assertIn(
            outreach.FAILURE_KINDS[LLMBadRequestError], OutreachAction.objects.get().further_action
        )

    def test_the_two_failure_messages_are_visibly_different(self):
        """Both rows are "no copy, needs_human", so only the prose separates them."""
        self.assertNotEqual(outreach.COPY_RETRIES_EXHAUSTED, outreach.COPY_FAILED_PERMANENTLY)
        self.assertIn("transient provider failure", outreach.COPY_RETRIES_EXHAUSTED)
        self.assertIn("engineer should look at", outreach.COPY_FAILED_PERMANENTLY)


@override_settings(COPY_VERIFY_LEVEL="off", **NO_SLEEP)
class PerLeadBudgetTests(TestCase):
    """One lead cannot hold a semaphore slot for ever."""

    @override_settings(
        OUTREACH_REQUEST_TIMEOUT_S=0.05,
        OUTREACH_PER_LEAD_TIMEOUT_S=0.05,
        OUTREACH_MAX_ATTEMPTS=4,
    )
    def test_a_lead_that_never_answers_is_given_up_on(self):
        _lead()
        client = _HangingClient()

        with _with_client(client):
            planned = plan_outreach()

        # The run finished at all: without the outer deadline it would hang.
        self.assertEqual(len(planned), 1)
        action = OutreachAction.objects.get()
        self.assertTrue(action.needs_human)
        self.assertIn(outreach.FAILURE_KINDS[LLMTimeoutError], action.further_action)
        self.assertIn("OUTREACH_PER_LEAD_TIMEOUT_S", action.further_action)

    @override_settings(
        OUTREACH_REQUEST_TIMEOUT_S=0.05,
        OUTREACH_PER_LEAD_TIMEOUT_S=0.2,
        # A pool of ONE: the healthy lead can only be served if the expiring
        # slow one released its semaphore slot.
        OUTREACH_MAX_IN_FLIGHT=1,
    )
    def test_one_slow_lead_does_not_take_the_others_down(self):
        _lead("lead_slow")
        _lead("lead_fine")
        clients = {"lead_slow": _HangingClient(), "lead_fine": _ScriptedClient()}

        # One client for the whole run, so the slow/fast split comes from the
        # prompt, as in production where phase 3 holds no lead object.
        class _Router:
            provider_name = "groq"

            async def agenerate(self, prompt, max_tokens=None, timeout=None):
                key = "lead_slow" if "Summit Risk Advisors" in prompt else "lead_fine"
                return await clients[key].agenerate(prompt, max_tokens, timeout)

            async def aclose(self):
                return None

        # Both leads share an agency name in `_lead`'s defaults, so give the
        # healthy one its own.
        Lead.objects.filter(id="lead_fine").update(agency_name="Cascade Underwriters")

        with _with_client(_Router()):
            planned = plan_outreach()

        self.assertEqual(len(planned), 2)
        self.assertNotEqual(OutreachAction.objects.get(lead_id="lead_fine").suggested_copy, "")
        self.assertTrue(OutreachAction.objects.get(lead_id="lead_slow").needs_human)


@override_settings(COPY_VERIFY_LEVEL="off", **NO_SLEEP)
class MessagesStayDistinctTests(TestCase):
    """The pin. These three rows must never read the same again."""

    def test_the_unmatched_classification_message_is_unchanged(self):
        """This wording predates the component and must not be harmonised with
        the failure messages -- it is the one describing a real decision."""
        lead = _unmatched_lead()

        planned = plan_outreach()

        self.assertEqual(
            planned[0].further_action,
            outreach.CLASSIFICATION_UNMATCHED.format(
                contact_name=lead.contact_name, agency_name=lead.agency_name
            ),
        )
        self.assertIn("no automated outreach pattern matched", planned[0].further_action)

    def test_a_rate_limited_lead_and_an_unmatched_lead_do_not_read_alike(self):
        """Both rows are `needs_human` with no copy; the prose must differ."""
        _lead("lead_throttled")
        _unmatched_lead("lead_nothing_matched")
        client = _ScriptedClient(*[rate_limit()] * 3, then=rate_limit())

        with _with_client(client):
            plan_outreach()

        throttled = OutreachAction.objects.get(lead_id="lead_throttled").further_action
        unmatched = OutreachAction.objects.get(lead_id="lead_nothing_matched").further_action

        self.assertNotEqual(throttled, unmatched)
        self.assertIn("transient provider failure", throttled)
        self.assertNotIn("transient provider failure", unmatched)
        self.assertIn("no automated outreach pattern matched", unmatched)
        self.assertNotIn("no automated outreach pattern matched", throttled)

    def test_a_non_provider_failure_keeps_the_pre_existing_wording(self):
        """A prompt that will not build is neither of the new categories and
        keeps its pre-existing message."""
        from unittest.mock import patch

        _lead()

        with patch(
            "project.app.services.outreach._build_copy_prompt",
            side_effect=ValueError("unrenderable lead"),
        ):
            plan_outreach()

        self.assertEqual(
            OutreachAction.objects.get().further_action,
            outreach.COPY_FAILED_UNEXPECTEDLY.format(
                error="unrenderable lead", action_type=actions.COMPLETE_ONBOARDING
            ),
        )


class FailureKindTests(SimpleTestCase):
    """The label table, tested as a table."""

    def test_every_taxonomy_class_has_its_own_words(self):
        labels = [
            outreach.failure_kind(cls("boom"))
            for cls in (
                LLMRateLimitError,
                LLMTimeoutError,
                LLMTransientError,
                LLMAuthError,
                LLMBadRequestError,
                LLMMalformedResponseError,
            )
        ]
        self.assertEqual(len(set(labels)), len(labels))
        # Prose, not class names: this ends up in front of a BD, not in a log.
        for label in labels:
            self.assertNotIn("LLM", label)
            self.assertNotIn("Error", label)

    def test_an_unnamed_subclass_inherits_its_parents_words(self):
        # Lookup walks the MRO, so a provider adapter's own subclass is still
        # described as a rate limit rather than "unclassified".
        class LLMQuotaError(LLMRateLimitError):
            pass

        self.assertEqual(
            outreach.failure_kind(LLMQuotaError("boom")),
            outreach.FAILURE_KINDS[LLMRateLimitError],
        )

    def test_the_base_class_has_a_label_so_nothing_falls_through(self):
        self.assertEqual(outreach.failure_kind(LLMError("boom")), outreach.FAILURE_KINDS[LLMError])


class AgenerateCopyRetryUnitTests(SimpleTestCase):
    """`agenerate_copy`'s own contract, without a planner around it."""

    def test_it_raises_a_wrapper_carrying_the_attempt_count(self):
        client = _ScriptedClient(*[rate_limit()] * 3, then=rate_limit())

        with self.assertRaises(outreach.CopyGenerationGaveUp) as caught:
            asyncio.run(
                outreach.agenerate_copy(
                    None,
                    actions.NUDGE_USAGE,
                    "reason",
                    prompt="a prompt",
                    client=client,
                    retry=RetryPolicy(max_attempts=4, initial_backoff_s=0.0, max_backoff_s=0.0),
                    timeouts=Timeouts(request_s=5.0, per_lead_s=5.0),
                )
            )

        self.assertEqual(caught.exception.attempts, 4)
        self.assertIsInstance(caught.exception.error, LLMRateLimitError)
        self.assertGreaterEqual(caught.exception.elapsed_s, 0.0)

    def test_the_request_timeout_rides_on_every_attempt(self):
        seen = []

        class _Recorder:
            provider_name = "groq"

            async def agenerate(self, prompt, max_tokens=None, timeout=None):
                seen.append(timeout)
                if len(seen) < 3:
                    raise rate_limit()
                return LLMResult(text=GOOD_COPY, provider="groq", model="recorder-model")

            async def aclose(self):
                return None

        asyncio.run(
            outreach.agenerate_copy(
                None,
                actions.NUDGE_USAGE,
                "reason",
                prompt="a prompt",
                client=_Recorder(),
                retry=RetryPolicy(max_attempts=4, initial_backoff_s=0.0, max_backoff_s=0.0),
                timeouts=Timeouts(request_s=7.5, per_lead_s=30.0),
            )
        )

        # Every attempt, not just the first.
        self.assertEqual(seen, [7.5, 7.5, 7.5])

    def test_a_successful_call_returns_the_text_unwrapped(self):
        result = asyncio.run(
            outreach.agenerate_copy(
                None,
                actions.NUDGE_USAGE,
                "reason",
                prompt="a prompt",
                client=_ScriptedClient(then="drafted"),
                retry=RetryPolicy(max_attempts=1),
                timeouts=Timeouts(request_s=5.0, per_lead_s=5.0),
            )
        )

        self.assertEqual(result, "drafted")


@override_settings(COPY_VERIFY_LEVEL="off", **NO_SLEEP)
class ReRunActuallyWorksTests(TestCase):
    """``COPY_RETRIES_EXHAUSTED`` tells a reviewer to re-run the planner, so a
    failed row must not hold the dedupe slot against the lead it named."""

    def test_a_lead_that_failed_is_re_planned_on_the_next_run(self):
        _lead()
        throttled = _ScriptedClient(*[rate_limit()] * 3, then=rate_limit())

        with _with_client(throttled):
            plan_outreach()

        failed = OutreachAction.objects.get()
        self.assertTrue(failed.needs_human)
        self.assertEqual(failed.suggested_copy, "")
        self.assertIn("Re-run the planner", failed.further_action)

        # Now do exactly what the message says, and nothing else.
        healthy = _ScriptedClient()
        with _with_client(healthy):
            planned = plan_outreach()

        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0].suggested_copy, GOOD_COPY)
        self.assertFalse(planned[0].needs_human)

    def test_the_stale_failure_row_is_superseded_rather_than_left_beside_it(self):
        # Otherwise the queue shows the real draft and the stale failure side by side.
        _lead()
        with _with_client(_ScriptedClient(*[rate_limit()] * 3, then=rate_limit())):
            plan_outreach()
        with _with_client(_ScriptedClient()):
            plan_outreach()

        self.assertEqual(OutreachAction.objects.count(), 1)
        self.assertEqual(OutreachAction.objects.get().suggested_copy, GOOD_COPY)

    def test_an_unmatched_lead_still_suppresses_its_re_plan(self):
        """Why this is not just `.exclude(copy="")`: the exclusion distinguishes
        "no copy because we failed" from "no copy was ever to be written"."""
        _unmatched_lead()

        first = plan_outreach()
        second = plan_outreach()

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(OutreachAction.objects.count(), 1)

    def test_a_flagged_draft_still_suppresses_its_re_plan(self):
        # A generation that succeeded but failed a gate keeps its draft, so it
        # is not a "failed generation" and must go on holding the slot.
        _lead()
        with _with_client(_ScriptedClient(then="Subject: too short\n\nHi.")):
            plan_outreach()

        flagged = OutreachAction.objects.get()
        self.assertTrue(flagged.needs_human)
        self.assertNotEqual(flagged.suggested_copy, "")

        with _with_client(_ScriptedClient()):
            self.assertEqual(plan_outreach(), [])


@override_settings(COPY_VERIFY_LEVEL="off", **NO_SLEEP)
class BudgetExpiryReportsTheRealCauseTests(TestCase):
    """What the per-lead budget says when it fires."""

    @override_settings(
        # The request timeout must come down too: a per-lead budget shorter
        # than one attempt is rejected outright.
        OUTREACH_REQUEST_TIMEOUT_S=0.15,
        OUTREACH_PER_LEAD_TIMEOUT_S=0.15,
        OUTREACH_MAX_ATTEMPTS=50,
    )
    def test_a_run_of_429s_that_runs_out_of_time_is_reported_as_rate_limits(self):
        """`asyncio.timeout` cancels the in-flight call, so the budget branch
        must report the provider's own error rather than fabricating a timeout."""
        _lead()
        # Retry-After keeps it retrying until the budget goes.
        client = _ScriptedClient(*[rate_limit(retry_after=0.02)] * 50, then=GOOD_COPY)

        with override_settings(OUTREACH_INITIAL_BACKOFF_S=0.02, OUTREACH_MAX_BACKOFF_S=0.02):
            with _with_client(client):
                plan_outreach()

        further = OutreachAction.objects.get().further_action
        self.assertIn(outreach.FAILURE_KINDS[LLMRateLimitError], further)
        self.assertNotIn(outreach.FAILURE_KINDS[LLMTimeoutError], further)
        # ...and the budget is still named, because that is the knob to turn.
        self.assertIn("OUTREACH_PER_LEAD_TIMEOUT_S", further)

    @override_settings(OUTREACH_REQUEST_TIMEOUT_S=0.05, OUTREACH_PER_LEAD_TIMEOUT_S=0.05)
    def test_a_provider_that_simply_never_answers_is_reported_as_a_timeout(self):
        _lead()

        with _with_client(_HangingClient()):
            plan_outreach()

        further = OutreachAction.objects.get().further_action
        self.assertIn(outreach.FAILURE_KINDS[LLMTimeoutError], further)
        self.assertIn("did not answer", further)


@override_settings(COPY_VERIFY_LEVEL="off", **NO_SLEEP)
class ForeignTimeoutTests(TestCase):
    """A `TimeoutError` we did not cause must not be relabelled as our budget."""

    @override_settings(OUTREACH_PER_LEAD_TIMEOUT_S=120.0, OUTREACH_REQUEST_TIMEOUT_S=60.0)
    def test_a_bare_timeout_error_keeps_its_own_words(self):
        # A builtin `TimeoutError` raised by the client, not the taxonomy's,
        # must not be reported as the per-lead budget expiring.
        _lead()
        client = _ScriptedClient(TimeoutError("[Errno 60] Operation timed out"))

        with _with_client(client):
            plan_outreach()

        further = OutreachAction.objects.get().further_action
        self.assertIn("Operation timed out", further)
        self.assertNotIn("OUTREACH_PER_LEAD_TIMEOUT_S", further)


class DetailIsSafeToPersistTests(SimpleTestCase):
    """`further_action` is stored and rendered. Provider text is not trusted."""

    def test_an_api_key_shaped_string_is_redacted(self):
        detail = outreach._safe_detail(
            RuntimeError("401 from https://x.test/v1?api_key=sk-abcdef0123456789 (bad key)")
        )

        self.assertNotIn("sk-abcdef0123456789", detail)
        self.assertIn("[redacted]", detail)

    def test_a_bearer_token_is_redacted(self):
        detail = outreach._safe_detail(RuntimeError("sent Authorization: Bearer hunter2secret"))
        self.assertNotIn("hunter2secret", detail)

    def test_an_enormous_provider_body_is_truncated(self):
        # SDKs put the whole response body in the message, so a proxy's HTML
        # error page would otherwise land in a TextField once per lead.
        detail = outreach._safe_detail(RuntimeError("<html>" + "x" * 200_000 + "</html>"))

        self.assertLessEqual(len(detail), outreach.DETAIL_MAX_CHARS + 20)
        self.assertTrue(detail.endswith("(truncated)"))

    def test_punctuation_is_not_doubled(self):
        self.assertTrue(outreach._safe_detail(RuntimeError("Already punctuated.")).endswith("d."))
        self.assertTrue(outreach._safe_detail(RuntimeError("Not punctuated")).endswith("d."))
