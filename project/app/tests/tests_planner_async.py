"""Phase 3 of the planner, now concurrent (MUS-26b): a bounded and genuinely
overlapping pool, per-lead result identity, repeat runs, and priority order."""

import asyncio
import os
import re
import warnings
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from django.test import TestCase, override_settings

from project.app.models import Lead, OutreachAction
from project.app.services import outreach
from project.app.services.llm import LLMClient, LLMResult
from project.app.services.outreach import plan_outreach

# Phase 3 is handed no lead object, so the prompt is the only channel a stub
# has for identifying which lead it was called for.
_AGENCY_IN_PROMPT = re.compile(r"^- Agency: (SYNTH-\d+)", re.MULTILINE)


def _agency_of(prompt):
    match = _AGENCY_IN_PROMPT.search(prompt)
    if match is None:
        # Raised rather than asserted: a bare `assert` vanishes under `-O`.
        raise AssertionError(f"stub could not identify the lead from prompt: {prompt[:200]!r}")
    return match.group(1)


def _copy_for(agency):
    """A well-shaped email naming its own lead, so a mix-up is visible."""
    return (
        f"Subject: A quick idea for {agency}\n\n"
        "Hi there,\n\n"
        f"{agency} has been working steadily through the portal, and I wanted to "
        "share one small change that usually helps agencies of this size get more "
        "quotes over the line. It takes about fifteen minutes to walk through, and "
        "your producers can start using it the same day. I would rather show you "
        "than write it all out here, since the useful part is seeing it against "
        "your own book of business. Would you have time for a short call this "
        "week?\n\n"
        "Best,\nDana"
    )


def _make_leads(count):
    """``count`` leads that all classify to ``complete_onboarding``.

    ``demo_completed`` with no signup date is date-independent, so these never
    drift with the wall clock. Distinct ``agency_name``s land verbatim in the
    prompt, which is how the stubs tell the leads apart.
    """
    return [
        Lead.objects.create(
            id=f"lead_{index:03d}",
            agency_name=f"SYNTH-{index:03d}",
            contact_name=f"Contact {index:03d}",
            contact_email=f"contact{index:03d}@example.com",
            contact_phone="555-0100",
            state="CA",
            num_producers=3,
            years_in_business=8,
            estimated_book_size_usd=1_000_000 + index,
            stage="demo_completed",
            signed_up_date=None,
        )
        for index in range(count)
    ]


def _unmatched_lead(lead_id="lead_unknown"):
    """No stage, no dates, no usage -- falls through every rule to UNKNOWN."""
    return Lead.objects.create(
        id=lead_id,
        agency_name="SYNTH-999",
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


class _ConcurrencyProbe:
    """An ``agenerate_copy`` stub that records how many calls overlap.

    No lock: the pool runs on one event loop in one thread.
    """

    def __init__(self, delay=0.005):
        self.delay = delay
        self.in_flight = 0
        self.peak = 0
        self.calls = 0

    async def __call__(self, lead, action_type, reason, *, prompt=None, client=None, **_runtime):
        self.in_flight += 1
        self.calls += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            # A real await, not `asyncio.sleep(0)`: a stub that returned
            # synchronously would report a peak of 1 however broken the pool was.
            await asyncio.sleep(self.delay)
            return _copy_for(_agency_of(prompt))
        finally:
            self.in_flight -= 1


def _stub(func):
    # ``**_runtime`` on every stub absorbs the ``retry=``/``timeouts=`` the
    # planner passes; a signature mismatch would surface as a provider failure.
    return patch("project.app.services.outreach.agenerate_copy", func)


@override_settings(COPY_VERIFY_LEVEL="off")
class BoundedPoolTests(TestCase):
    """The semaphore bounds the run, and the bound is the configured one."""

    @override_settings(OUTREACH_MAX_IN_FLIGHT=3)
    def test_in_flight_calls_never_exceed_the_configured_bound(self):
        _make_leads(12)
        probe = _ConcurrencyProbe()

        with _stub(probe):
            planned = plan_outreach()

        self.assertEqual(probe.calls, 12)
        self.assertLessEqual(probe.peak, 3)
        self.assertEqual(len(planned), 12)

    @override_settings(OUTREACH_MAX_IN_FLIGHT=3)
    def test_the_bound_is_actually_reached(self):
        """The other half of the bound: 3 must also be *reached*, since
        ``peak <= 3`` alone is satisfied by a serial loop."""
        _make_leads(12)
        probe = _ConcurrencyProbe()

        with _stub(probe):
            plan_outreach()

        self.assertEqual(probe.peak, 3)

    @override_settings(OUTREACH_MAX_IN_FLIGHT=32)
    def test_a_bound_larger_than_the_run_is_not_an_error(self):
        _make_leads(4)
        probe = _ConcurrencyProbe()

        with _stub(probe):
            plan_outreach()

        self.assertEqual(probe.peak, 4)  # bounded by the work, not by the setting

    @override_settings(OUTREACH_MAX_IN_FLIGHT=1)
    def test_a_bound_of_one_serializes_the_run(self):
        _make_leads(5)
        probe = _ConcurrencyProbe()

        with _stub(probe):
            plan_outreach()

        self.assertEqual(probe.peak, 1)
        self.assertEqual(probe.calls, 5)

    def test_leads_needing_no_copy_make_no_provider_call(self):
        _unmatched_lead()
        probe = _ConcurrencyProbe()

        with _stub(probe):
            planned = plan_outreach()

        self.assertEqual(probe.calls, 0)
        self.assertEqual(len(planned), 1)
        self.assertTrue(planned[0].needs_human)

    @override_settings(OUTREACH_MAX_IN_FLIGHT=1)
    def test_leads_needing_no_copy_never_queue_behind_one_that_does(self):
        """The skip check sits *ahead* of the semaphore, not inside it.

        Observed by recording the order in which leads are resolved: all four
        decisions must land before the one slow provider call returns.
        """
        _make_leads(1)  # the one lead that calls the provider, slowly
        for index in range(3):
            _unmatched_lead(f"lead_skip_{index}")

        order = []
        undecorated = outreach._outcome_without_calling

        def spy(item, client_error):
            order.append(item.lead.id)
            return undecorated(item, client_error)

        async def slow_matched(lead, action_type, reason, *, prompt=None, client=None, **_runtime):
            agency = _agency_of(prompt)
            await asyncio.sleep(0.05)
            order.append("SLOW-CALL-RETURNED")
            return _copy_for(agency)

        with patch.object(outreach, "_outcome_without_calling", spy), _stub(slow_matched):
            planned = plan_outreach()

        self.assertEqual(len(planned), 4)
        # Behind the semaphore, this marker would land mid-list instead of last.
        self.assertEqual(order[-1], "SLOW-CALL-RETURNED")
        self.assertEqual(
            {entry for entry in order if entry != "SLOW-CALL-RETURNED"},
            {"lead_000", "lead_skip_0", "lead_skip_1", "lead_skip_2"},
        )


@override_settings(COPY_VERIFY_LEVEL="off", OUTREACH_MAX_IN_FLIGHT=8)
class OutOfOrderCompletionTests(TestCase):
    """Concurrency reorders completions; it must not reorder results.

    ``OUTREACH_MAX_IN_FLIGHT`` is pinned rather than inherited from the
    environment: a CI job exporting 1 would make these tests tautologies.
    """

    def test_each_leads_copy_lands_on_that_lead(self):
        leads = _make_leads(8)

        async def reversed_latency(
            lead, action_type, reason, *, prompt=None, client=None, **_runtime
        ):
            agency = _agency_of(prompt)
            index = int(agency.rsplit("-", 1)[1])
            # Later leads finish first, so completion order is the exact reverse
            # of submission order.
            await asyncio.sleep((8 - index) * 0.002)
            return _copy_for(agency)

        with _stub(reversed_latency):
            plan_outreach()

        self.assertEqual(OutreachAction.objects.count(), len(leads))
        for lead in leads:
            action = OutreachAction.objects.get(lead_id=lead.id)
            self.assertIn(
                lead.agency_name,
                action.suggested_copy,
                f"{lead.id} was written copy belonging to another lead",
            )

    def test_one_lead_failing_does_not_disturb_the_others(self):
        _make_leads(6)

        async def fail_one(lead, action_type, reason, *, prompt=None, client=None, **_runtime):
            agency = _agency_of(prompt)
            await asyncio.sleep(0.002)
            if agency == "SYNTH-003":
                raise RuntimeError("provider exploded")
            return _copy_for(agency)

        with _stub(fail_one):
            planned = plan_outreach()

        # `return_exceptions=True` on the gather: one dead lead, not a dead run.
        self.assertEqual(len(planned), 6)
        failed = OutreachAction.objects.get(lead_id="lead_003")
        self.assertTrue(failed.needs_human)
        self.assertEqual(failed.suggested_copy, "")
        for lead_id in ("lead_000", "lead_001", "lead_002", "lead_004", "lead_005"):
            survivor = OutreachAction.objects.get(lead_id=lead_id)
            self.assertNotEqual(survivor.suggested_copy, "")


@override_settings(COPY_VERIFY_LEVEL="off", OUTREACH_MAX_IN_FLIGHT=8)
class RepeatedRunTests(TestCase):
    """`asyncio.run()` builds a loop and closes it. Twice must still work."""

    def test_two_runs_in_one_process_both_produce_rows(self):
        _make_leads(3)
        probe = _ConcurrencyProbe()

        with _stub(probe):
            first = plan_outreach()
            # An open recommendation suppresses a re-run, so the second run
            # would otherwise find nothing to do.
            OutreachAction.objects.update(status=OutreachAction.STATUS_APPROVED)
            second = plan_outreach()

        self.assertEqual(len(first), 3)
        self.assertEqual(len(second), 3)
        self.assertEqual(probe.calls, 6)

    def test_the_returned_list_is_sorted_by_priority(self):
        # Priority is scored from the record, so the spread of book sizes and
        # usage below is what produces a spread of priorities.
        _make_leads(4)
        Lead.objects.filter(id="lead_000").update(
            estimated_book_size_usd=25_000_000, deals_closed=9, quotes_submitted=40
        )
        Lead.objects.filter(id="lead_001").update(estimated_book_size_usd=8_000_000)
        probe = _ConcurrencyProbe()

        with _stub(probe):
            planned = plan_outreach()

        priorities = [action.priority for action in planned]
        self.assertEqual(priorities, sorted(priorities))
        # A single-valued list is trivially sorted.
        self.assertGreater(len(set(priorities)), 1)


class _FakeClient(LLMClient):
    """A real ``LLMClient`` whose async path records what it was asked for.

    A real subclass rather than a ``Mock``, which answers sync and async
    attributes identically and so could not tell the two paths apart.
    """

    provider_name = "fake"

    def __init__(self, text="", error=None):
        super().__init__(model="fake-model", default_max_tokens=1)
        self.text = text
        self.error = error
        self.async_calls = []
        self.sync_calls = []
        self.closed = 0

    def generate(self, prompt, max_tokens=None, timeout=None):
        raise AssertionError("the planner must not take the blocking path")

    def complete(self, prompt, max_tokens=None, timeout=None):
        # Recorded rather than raised, so tests can assert it stayed empty.
        self.sync_calls.append(prompt)
        return self.text

    async def agenerate(self, prompt, max_tokens=None, timeout=None):
        # `acomplete` is inherited (it awaits this and takes `.text`), so one
        # recording covers whichever async entry point the caller picks.
        self.async_calls.append({"prompt": prompt, "max_tokens": max_tokens})
        if self.error is not None:
            raise self.error
        return LLMResult(text=self.text, provider=self.provider_name, model=self.model)

    async def aclose(self):
        self.closed += 1


class AgenerateCopyTests(TestCase):
    """`agenerate_copy` itself, unmocked -- every other test here patches it
    out, so these tie the mock seam to the function it stands in for."""

    def test_it_awaits_the_async_path_and_forwards_the_token_cap(self):
        client = _FakeClient(text="drafted")

        result = asyncio.run(
            outreach.agenerate_copy(None, "nudge_usage", "reason", prompt="a prompt", client=client)
        )

        self.assertEqual(result, "drafted")
        self.assertEqual(client.async_calls, [{"prompt": "a prompt", "max_tokens": 500}])
        self.assertEqual(client.sync_calls, [])

    def test_it_builds_a_prompt_when_given_a_lead(self):
        # Duck-typed, not a model instance: building a prompt walks
        # `lead.events`, which inside `asyncio.run` is SynchronousOnlyOperation.
        lead = SimpleNamespace(
            id="duck_001",
            agency_name="Duck Typed Agency",
            contact_name="Sam Reed",
            contact_email="sam@ducktyped.test",
            state="CO",
            num_producers=2,
            years_in_business=4,
            estimated_book_size_usd=750_000,
            stage="demo_completed",
            signed_up_date=None,
            last_login_date=None,
            last_contacted_date=None,
            quotes_created=0,
            quotes_submitted=0,
            deals_closed=0,
            hubspot_notes="",
            events=[],
        )
        client = _FakeClient(text="drafted")

        asyncio.run(outreach.agenerate_copy(lead, "complete_onboarding", "reason", client=client))

        self.assertIn("Duck Typed Agency", client.async_calls[0]["prompt"])

    def test_neither_a_lead_nor_a_prompt_is_a_loud_error(self):
        client = _FakeClient()

        with self.assertRaises(ValueError) as caught:
            asyncio.run(outreach.agenerate_copy(None, "nudge_usage", "reason", client=client))

        # Raised from the shared helper, so the message must name this path.
        self.assertIn("agenerate_copy", str(caught.exception))


@override_settings(COPY_VERIFY_LEVEL="off")
class EndToEndProviderPathTests(TestCase):
    """One run with `get_llm_client` as the only seam; everything downstream of
    it runs for real."""

    def test_the_planner_reaches_the_client_through_the_async_path(self):
        _make_leads(3)
        client = _FakeClient(text=_copy_for("SYNTH-000"))

        with patch("project.app.services.outreach.get_llm_client", return_value=client):
            planned = plan_outreach()

        self.assertEqual(len(planned), 3)
        self.assertEqual(len(client.async_calls), 3)
        self.assertEqual(client.sync_calls, [])
        self.assertTrue(all(call["max_tokens"] == 500 for call in client.async_calls))

    def test_the_run_closes_the_client_it_was_handed(self):
        """`asyncio.run` closes the loop but not the transports on it, so the
        run that owns the loop must await `aclose`."""
        _make_leads(2)
        client = _FakeClient(text=_copy_for("SYNTH-000"))

        with patch("project.app.services.outreach.get_llm_client", return_value=client):
            plan_outreach()

        self.assertEqual(client.closed, 1)

    def test_a_run_with_nothing_to_generate_closes_nothing(self):
        # `_resolve_client` returns (None, None) when no lead needs copy, so
        # there is no client to close and no configuration to read.
        _unmatched_lead()

        with patch("project.app.services.outreach.get_llm_client") as get_client:
            plan_outreach()

        get_client.assert_not_called()

    def test_a_failing_close_does_not_cost_the_run_its_results(self):
        _make_leads(2)
        client = _FakeClient(text=_copy_for("SYNTH-000"))

        async def explode():
            raise OSError("socket teardown went wrong")

        client.aclose = explode

        with patch("project.app.services.outreach.get_llm_client", return_value=client):
            planned = plan_outreach()

        self.assertEqual(len(planned), 2)


class RunningLoopGuardTests(TestCase):
    """Inside a loop, the runner refuses rather than improvises.

    Calling ``plan_outreach()`` from a coroutine never reaches the guard --
    phase 1's ORM read raises ``SynchronousOnlyOperation`` first, pinned below.
    """

    def test_the_runner_refuses_to_nest_a_loop(self):
        async def call_it():
            outreach._run_coroutine(_never_awaited())

        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(call_it())

        message = str(caught.exception)
        self.assertIn("plan_outreach()", message)
        self.assertIn("event loop", message)

    def test_plan_outreach_from_a_coroutine_dies_at_the_orm_first(self):
        from django.core.exceptions import SynchronousOnlyOperation

        async def call_it():
            plan_outreach()

        # DJANGO_ALLOW_ASYNC_UNSAFE would suppress Django's guard and let the
        # run reach `_run_coroutine` instead; cleared for the duration.
        env = {k: v for k, v in os.environ.items() if k != "DJANGO_ALLOW_ASYNC_UNSAFE"}

        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SynchronousOnlyOperation):
                asyncio.run(call_it())

    def test_the_abandoned_coroutine_is_closed_rather_than_leaked(self):
        # A coroutine created and never awaited emits a RuntimeWarning when it
        # is collected, which `python -W error` turns into a failure.
        async def call_it():
            with warnings.catch_warnings(record=True) as caught_warnings:
                warnings.simplefilter("always")
                with self.assertRaises(RuntimeError):
                    outreach._run_coroutine(_never_awaited())
            return [w for w in caught_warnings if issubclass(w.category, RuntimeWarning)]

        self.assertEqual(asyncio.run(call_it()), [])


async def _never_awaited():  # pragma: no cover - closed, never awaited
    return None
