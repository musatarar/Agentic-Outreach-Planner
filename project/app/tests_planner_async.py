"""Phase 3 of the planner, now concurrent (MUS-26b).

Four properties, all of which were free when the loop was serial and none of
which are free now:

* the pool is genuinely **bounded**, and genuinely **concurrent** -- a semaphore
  you forget to await bounds nothing, and a pool that never overlaps is the
  serial loop this ticket replaces. Both halves need an assertion;
* an outcome lands on the **right lead** even when the provider answers out of
  order, which a serial loop guaranteed structurally and ``gather`` guarantees
  only because it preserves argument order;
* ``plan_outreach()`` survives being called **twice in one process**, which is
  the dead-event-loop regression that any ``asyncio.run``-per-call design walks
  straight into; and
* the returned list is still **priority-sorted**, because the frontend renders
  it in the order it arrives.

The stubs here patch ``outreach.agenerate_copy``. ``mock.patch`` sees an
``async def`` and substitutes an ``AsyncMock``, so a plain ``return_value``
still works -- but the target has to be the function the planner actually
calls, or the test asserts nothing at all.
"""

import asyncio
import re
import warnings
from unittest.mock import patch

from django.test import TestCase, override_settings

from project.app.models import Lead, OutreachAction
from project.app.services import outreach
from project.app.services.outreach import plan_outreach

# Pulled out of the prompt to identify which lead a stub was called for. Phase 3
# is deliberately handed no lead object (see `_agenerate_for`), so the prompt is
# genuinely the only channel available -- which is itself the point: a stub that
# could just read `lead.id` would not notice the day someone hands phase 3 a
# lead and reintroduces a lazy query inside the event loop.
_AGENCY_IN_PROMPT = re.compile(r"^- Agency: (SYNTH-\d+)", re.MULTILINE)


def _agency_of(prompt):
    match = _AGENCY_IN_PROMPT.search(prompt)
    assert match is not None, f"stub could not identify the lead from prompt: {prompt[:200]!r}"
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

    ``demo_completed`` with no signup date is the one classification that is
    date-independent, so these tests never drift with the wall clock. Each lead
    gets a distinct ``agency_name`` because that field lands verbatim in the
    trusted section of the prompt, which is how the stubs tell them apart.
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

    No lock: the whole pool runs on one event loop in one thread, so the
    increment and the ``max`` are not interleaved with anything. A lock here
    would imply a threading model this code does not have.
    """

    def __init__(self, delay=0.005):
        self.delay = delay
        self.in_flight = 0
        self.peak = 0
        self.calls = 0

    async def __call__(self, lead, action_type, reason, *, prompt=None, client=None):
        self.in_flight += 1
        self.calls += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            # A real await, not `asyncio.sleep(0)`: the scheduler only has a
            # reason to start the next lead while this one is parked on I/O, so
            # a stub that returned synchronously would report a peak of 1 no
            # matter how broken the pool was.
            await asyncio.sleep(self.delay)
            return _copy_for(_agency_of(prompt))
        finally:
            self.in_flight -= 1


def _stub(func):
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
        """The other half of the bound: 3 must also be *reached*.

        ``assertLessEqual(peak, 3)`` on its own is satisfied by a serial loop --
        which is precisely the implementation this ticket replaces. Without this
        assertion the bound test would pass against the code it exists to stop
        us regressing to.
        """
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

    def test_leads_needing_no_copy_never_take_a_slot(self):
        # An unmatched lead has no provider call to make, so it must not queue
        # for permission to make one.
        _unmatched_lead()
        probe = _ConcurrencyProbe()

        with _stub(probe):
            planned = plan_outreach()

        self.assertEqual(probe.calls, 0)
        self.assertEqual(len(planned), 1)
        self.assertTrue(planned[0].needs_human)


@override_settings(COPY_VERIFY_LEVEL="off")
class OutOfOrderCompletionTests(TestCase):
    """Concurrency reorders completions; it must not reorder results."""

    def test_each_leads_copy_lands_on_that_lead(self):
        leads = _make_leads(8)

        async def reversed_latency(lead, action_type, reason, *, prompt=None, client=None):
            agency = _agency_of(prompt)
            index = int(agency.rsplit("-", 1)[1])
            # Later leads finish first, so completion order is the exact reverse
            # of submission order. A gather whose results were collected
            # as-completed would mis-assign every single row.
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

        async def fail_one(lead, action_type, reason, *, prompt=None, client=None):
            agency = _agency_of(prompt)
            await asyncio.sleep(0.002)
            if agency == "SYNTH-003":
                raise RuntimeError("provider exploded")
            return _copy_for(agency)

        with _stub(fail_one):
            planned = plan_outreach()

        # `return_exceptions=True` on the gather: one dead lead is one dead
        # lead, not a cancelled run. Without it the first failure would cancel
        # every sibling and throw away results already computed.
        self.assertEqual(len(planned), 6)
        failed = OutreachAction.objects.get(lead_id="lead_003")
        self.assertTrue(failed.needs_human)
        self.assertEqual(failed.suggested_copy, "")
        for lead_id in ("lead_000", "lead_001", "lead_002", "lead_004", "lead_005"):
            survivor = OutreachAction.objects.get(lead_id=lead_id)
            self.assertNotEqual(survivor.suggested_copy, "")


@override_settings(COPY_VERIFY_LEVEL="off")
class RepeatedRunTests(TestCase):
    """`asyncio.run()` builds a loop and closes it. Twice must still work."""

    def test_two_runs_in_one_process_both_produce_rows(self):
        _make_leads(3)
        probe = _ConcurrencyProbe()

        with _stub(probe):
            first = plan_outreach()
            # An open recommendation suppresses a re-run (CONTRACT 9.8), so
            # without this the second run would find nothing to do and the
            # dead-loop bug would sail past untested.
            OutreachAction.objects.update(status=OutreachAction.STATUS_APPROVED)
            second = plan_outreach()

        self.assertEqual(len(first), 3)
        self.assertEqual(len(second), 3)
        self.assertEqual(probe.calls, 6)

    def test_the_returned_list_is_sorted_by_priority(self):
        # Priority is scored from the record, so a spread of book sizes and
        # usage is what produces a spread of priorities rather than four p3s.
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
        # A single-valued list is trivially sorted, so the assertion above only
        # means something if the run actually produced a spread.
        self.assertGreater(len(set(priorities)), 1)


class RunningLoopGuardTests(TestCase):
    """Inside a loop, the runner refuses rather than improvises.

    Worth being precise about what this guard is for. Calling ``plan_outreach()``
    from a coroutine never reaches it: phase 1 is an ORM read, so Django's own
    ``SynchronousOnlyOperation`` fires several lines earlier -- pinned below, so
    nobody has to rediscover it. ``_run_coroutine`` is guarded anyway because
    the failure it prevents is the *silent* one: the obvious way to make a sync
    function drive a loop from inside another loop is to spawn a thread with its
    own loop and block on it, which would appear to work while running the
    planner's ORM phases on a thread whose connection handling nobody reasoned
    about.
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

        # Not the message this module's guard would give -- Django's is fine,
        # and arrives first. Pinned so a future reordering of the phases (or a
        # move to async ORM reads) surfaces here rather than as a surprise.
        with self.assertRaises(SynchronousOnlyOperation):
            asyncio.run(call_it())

    def test_the_abandoned_coroutine_is_closed_rather_than_leaked(self):
        # A coroutine created and never awaited emits a RuntimeWarning when it
        # is collected -- detached from the raise, and reading like a second,
        # unrelated bug. `python -W error` would turn it into a failure.
        async def call_it():
            with warnings.catch_warnings(record=True) as caught_warnings:
                warnings.simplefilter("always")
                with self.assertRaises(RuntimeError):
                    outreach._run_coroutine(_never_awaited())
            return [w for w in caught_warnings if issubclass(w.category, RuntimeWarning)]

        self.assertEqual(asyncio.run(call_it()), [])


async def _never_awaited():  # pragma: no cover - closed, never awaited
    return None
