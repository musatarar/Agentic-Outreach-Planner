"""Retries, and what the review queue says when they run out (MUS-26c).

This is the ticket's actual complaint. The review queue is a *finite* list of
things a human has to decide, and its whole value is that everything in it is
work. A 429 from a free tier used to land there looking exactly like "no
automated outreach pattern matched" -- same `needs_human`, same shape of
sentence about this lead -- so a reviewer had no way to tell a provider's bad
thirty seconds from a real judgement call, and learned to skim.

Two things fix that, and both are tested here: the 429 is **retried** rather
than escalated at all, and when retries genuinely run out the row says whose
problem it is.

Every test drives the real `agenerate_copy` -- the stub is a fake `LLMClient`,
one layer below. Patching `agenerate_copy` (as the pool tests do) would skip the
retry loop entirely, which is the thing under test.
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

# Backoff is switched off rather than shortened. `initial_backoff_s=0` makes
# `random.uniform(0, 0)` return 0 for every attempt, so these tests exercise the
# retry *count* deterministically without sleeping and without depending on a
# seeded PRNG. The schedule itself is tested in the retry helper's own suite.
NO_SLEEP = {"OUTREACH_INITIAL_BACKOFF_S": 0.0, "OUTREACH_MAX_BACKOFF_S": 0.0}


class _ScriptedClient:
    """A provider that fails on cue and counts how often it was asked.

    Duck-typed rather than an ``LLMClient`` subclass: the only surface the retry
    path touches is ``acomplete``, and a subclass would drag in the abstract
    ``generate`` for no benefit here.
    """

    provider_name = "groq"

    def __init__(self, *errors, then=GOOD_COPY):
        # `errors` is the script: one entry per attempt, consumed in order. When
        # it runs out, `then` is returned -- so `_ScriptedClient(rate_limit(),
        # rate_limit())` means "fail twice, then succeed".
        self.script = list(errors)
        self.then = then
        self.attempts = 0

    async def acomplete(self, prompt, max_tokens=None, timeout=None):
        self.attempts += 1
        if self.script:
            raise self.script.pop(0)
        if isinstance(self.then, BaseException):
            raise self.then
        return self.then

    async def aclose(self):
        return None


class _HangingClient:
    """A provider that never answers, for the per-lead budget."""

    provider_name = "groq"

    def __init__(self):
        self.attempts = 0

    async def acomplete(self, prompt, max_tokens=None, timeout=None):
        self.attempts += 1
        await asyncio.Event().wait()  # never resolves

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
        """**This is MUS-26's literal acceptance criterion**: *"Rate limits are
        retried, not reported as needing human review."*

        Two 429s then a success. The lead must come out of the run with copy and
        `needs_human = False` -- indistinguishable from a lead the provider
        answered first time, because from a reviewer's point of view it *is*
        indistinguishable. The attempt count is asserted too: without it, a
        planner that silently gave up and a planner that retried twice both
        satisfy "needs_human is False" as long as some copy exists.
        """
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

    def test_a_retry_after_header_is_honoured_without_stalling_the_test(self):
        # `retry_after` wins on magnitude over the configured backoff, capped by
        # OUTREACH_MAX_BACKOFF_S -- which is 0 here, so a hostile 300-second
        # header costs this test nothing. That the cap applies at all is the
        # assertion; the schedule itself is the retry helper's own suite.
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

        # Asserted against the constant, never a literal: a test that hard-codes
        # this wording keeps passing while the three messages drift back into
        # each other, which is the exact regression this component exists to
        # prevent. Only the wall-clock seconds are matched loosely -- `re.escape`
        # leaves the alphanumeric sentinel intact, so it survives to be swapped
        # for a number pattern.
        sentinel = "ELAPSEDSECONDS"
        expected = outreach.COPY_RETRIES_EXHAUSTED.format(
            attempts=3,
            elapsed=sentinel,
            provider="groq",
            kind=outreach.FAILURE_KINDS[LLMRateLimitError],
            detail="Rate limit reached",
            action_type=actions.COMPLETE_ONBOARDING,
        )
        self.assertRegex(action.further_action, re.escape(expected).replace(sentinel, r"\d+\.\d"))

    @override_settings(OUTREACH_MAX_ATTEMPTS=2)
    def test_it_says_the_lead_is_not_the_problem(self):
        """The sentence a reviewer actually acts on.

        Everything else in this row -- the priority, the action type, the reason
        -- is still correct and still worth acting on later. Saying so is what
        stops the row being read as "this lead is broken".
        """
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
        # Four configured attempts, all refused. A message that said "1 attempt"
        # would understate the effort and read like the run gave up instantly.
        _lead()
        client = _ScriptedClient(*[rate_limit()] * 3, then=rate_limit())

        with _with_client(client):
            plan_outreach()

        self.assertEqual(client.attempts, 4)
        self.assertIn("after 4 attempt(s)", OutreachAction.objects.get().further_action)


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

        # Exactly one attempt. Hammering an endpoint with a bad key burns the
        # retry budget and, on some providers, earns a longer lockout.
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
        """A reviewer must be able to tell them apart at a glance.

        Both are "no copy, needs_human". The only thing separating them is the
        prose, so the prose has to actually separate them.
        """
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

        # The run finished at all, which is the point: without the outer
        # deadline this test would hang, holding 1 of OUTREACH_MAX_IN_FLIGHT
        # slots -- and in production, 1/N of the run's throughput -- for as long
        # as the provider cared to stay silent.
        self.assertEqual(len(planned), 1)
        action = OutreachAction.objects.get()
        self.assertTrue(action.needs_human)
        self.assertIn(outreach.FAILURE_KINDS[LLMTimeoutError], action.further_action)
        self.assertIn("OUTREACH_PER_LEAD_TIMEOUT_S", action.further_action)

    @override_settings(OUTREACH_REQUEST_TIMEOUT_S=0.05, OUTREACH_PER_LEAD_TIMEOUT_S=0.2)
    def test_one_slow_lead_does_not_take_the_others_down(self):
        _lead("lead_slow")
        _lead("lead_fine")
        clients = {"lead_slow": _HangingClient(), "lead_fine": _ScriptedClient()}

        # One client for the whole run, so the slow/fast split has to come from
        # the prompt -- exactly as it does in production, where phase 3 holds no
        # lead object.
        class _Router:
            provider_name = "groq"

            async def acomplete(self, prompt, max_tokens=None, timeout=None):
                key = "lead_slow" if "Summit Risk Advisors" in prompt else "lead_fine"
                return await clients[key].acomplete(prompt, max_tokens, timeout)

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
        """Real BD work, and its wording predates this component.

        Pinned deliberately: the temptation when adding failure messages is to
        "harmonise" them all, and this one must not move. It is the only one of
        the four that describes something a reviewer can actually decide.
        """
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
        """The whole point of the component, as one assertion.

        Both rows are `needs_human = True` with no copy. Before this, both also
        carried a sentence about the lead and nothing to tell them apart.
        """
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
        """A prompt that will not build is neither of the new categories.

        It predates this component and its message is unchanged, so the tests
        that already pinned it keep passing for the right reason.
        """
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
        # A provider adapter inventing `LLMQuotaError(LLMRateLimitError)` should
        # be described as a rate limit, not as "unclassified" -- which is what a
        # lookup keyed on the exact type would say about the single most
        # classifiable thing that happens to a free tier.
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

            async def acomplete(self, prompt, max_tokens=None, timeout=None):
                seen.append(timeout)
                if len(seen) < 3:
                    raise rate_limit()
                return GOOD_COPY

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

        # Every attempt, not just the first: a retry that dropped the per-request
        # deadline would let attempt four hang for ever.
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
