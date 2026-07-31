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
import os
import re
import warnings
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from django.test import TestCase, override_settings

from project.app.models import Lead, OutreachAction
from project.app.services import outreach
from project.app.services.llm import LLMClient
from project.app.services.outreach import plan_outreach

# Pulled out of the prompt to identify which lead a stub was called for. Phase 3
# is deliberately handed no lead object (see `_agenerate_for`), so the prompt is
# genuinely the only channel available -- which is itself the point: a stub that
# could just read `lead.id` would not notice the day someone hands phase 3 a
# lead and reintroduces a lazy query inside the event loop.
_AGENCY_IN_PROMPT = re.compile(r"^- Agency: (SYNTH-\d+)", re.MULTILINE)


def _agency_of(prompt):
    match = _AGENCY_IN_PROMPT.search(prompt)
    if match is None:
        # Raised rather than asserted: a bare `assert` vanishes under `-O`, and
        # a stub that silently stopped identifying its lead would make every
        # out-of-order test below pass while checking nothing.
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

    async def __call__(self, lead, action_type, reason, *, prompt=None, client=None, **_runtime):
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
    # ``**_runtime`` on every stub in this module absorbs the ``retry=`` and
    # ``timeouts=`` the planner passes (MUS-26c). Swallowed rather than
    # asserted: these tests are about the pool, and the retry policy has its own
    # suite. What matters is that a signature mismatch here would be caught by
    # `_agenerate_for`'s `except Exception` and reported as a provider failure,
    # which is a silent and very confusing way for a test to go green.
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

        Asserting `calls == 0` (above) is satisfied by any implementation that
        skips the provider -- including one that acquires a slot first and only
        then decides it has nothing to do. With a pool of one and a slow matched
        lead, that version makes every unmatched lead wait out the slow one,
        which is the steady state once a queue has been worked down and most
        leads are unmatched.

        Observed by recording the order in which leads are *resolved*: with the
        hoist, all four decisions land before the slow provider call returns.
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
        # The load-bearing assertion. Behind the semaphore, the three skips
        # could only be decided after the slow call released its slot, so
        # "SLOW-CALL-RETURNED" would appear in the middle of this list.
        self.assertEqual(order[-1], "SLOW-CALL-RETURNED")
        self.assertEqual(
            {entry for entry in order if entry != "SLOW-CALL-RETURNED"},
            {"lead_000", "lead_skip_0", "lead_skip_1", "lead_skip_2"},
        )


@override_settings(COPY_VERIFY_LEVEL="off", OUTREACH_MAX_IN_FLIGHT=8)
class OutOfOrderCompletionTests(TestCase):
    """Concurrency reorders completions; it must not reorder results.

    ``OUTREACH_MAX_IN_FLIGHT`` is pinned rather than inherited: it is read from
    the environment, and a CI job exporting ``OUTREACH_MAX_IN_FLIGHT=1`` would
    turn the reordering test below into a tautology -- serial execution
    trivially preserves order, so it would still pass while testing nothing.
    """

    def test_each_leads_copy_lands_on_that_lead(self):
        leads = _make_leads(8)

        async def reversed_latency(
            lead, action_type, reason, *, prompt=None, client=None, **_runtime
        ):
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

        async def fail_one(lead, action_type, reason, *, prompt=None, client=None, **_runtime):
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


@override_settings(COPY_VERIFY_LEVEL="off", OUTREACH_MAX_IN_FLIGHT=8)
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


class _FakeClient(LLMClient):
    """A real ``LLMClient`` whose async path records what it was asked for.

    A real subclass rather than a ``Mock``: the thing under test is that the
    planner reaches ``acomplete`` -- the *async* method -- and a Mock answers
    every attribute identically, so it could not tell the two apart.
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
        # Recorded rather than raised: a test asserting `async_calls` would
        # otherwise pass on a run that also, wastefully, called this.
        self.sync_calls.append(prompt)
        return self.text

    async def acomplete(self, prompt, max_tokens=None, timeout=None):
        self.async_calls.append({"prompt": prompt, "max_tokens": max_tokens})
        if self.error is not None:
            raise self.error
        return self.text

    async def aclose(self):
        self.closed += 1


class AgenerateCopyTests(TestCase):
    """`agenerate_copy` itself, with nothing mocked in the middle.

    Every other test in this module patches `agenerate_copy` out, which leaves
    the one genuinely new production function untested. That is not an academic
    gap: change its body from `await client.acomplete(...)` to
    `client.complete(...)` and every concurrency test above still passes, while
    the pool silently collapses to a peak of 1 -- the exact regression this
    ticket exists to prevent, sailing straight through the tests written to
    catch it. These are what tie the mock seam to the thing it stands in for.
    """

    def test_it_awaits_the_async_path_and_forwards_the_token_cap(self):
        client = _FakeClient(text="drafted")

        result = asyncio.run(
            outreach.agenerate_copy(None, "nudge_usage", "reason", prompt="a prompt", client=client)
        )

        self.assertEqual(result, "drafted")
        self.assertEqual(client.async_calls, [{"prompt": "a prompt", "max_tokens": 500}])
        self.assertEqual(client.sync_calls, [])

    def test_it_builds_a_prompt_when_given_a_lead(self):
        # A duck-typed lead, deliberately, not a model instance. Building a
        # prompt walks `lead.events`, and doing that on a real model inside
        # `asyncio.run` is Django's `SynchronousOnlyOperation` -- which is
        # exactly why the planner builds its prompts in phase 2 and hands phase
        # 3 no lead at all.
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

        # The message names both entry points -- it is raised from the shared
        # helper, and blaming the sync twin from the async path is a five-minute
        # detour for whoever reads it.
        self.assertIn("agenerate_copy", str(caught.exception))


@override_settings(COPY_VERIFY_LEVEL="off")
class EndToEndProviderPathTests(TestCase):
    """One run with no mock between the planner and the client.

    `get_llm_client` is the only seam: everything downstream of it -- phase 3,
    the semaphore, `agenerate_copy`, `acomplete` -- runs for real.
    """

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
        """`asyncio.run` closes the loop but not the transports on it.

        `LoopBoundAsyncClient` keeps a hard reference to both, so without this
        every run strands a live connection pool on a dead loop until the next
        run overwrites the cache -- and every configuration change in the
        Settings UI mints a new adapter through an unbounded `lru_cache`,
        stranding the old one's pool for good. `LLMClient.aclose` says as much:
        callers that own the event loop should await it in a finally. This run
        owns the loop.
        """
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

        # Phase 3's results are already computed by the time the client is
        # closed. Losing them to a failed socket teardown would be absurd.
        self.assertEqual(len(planned), 2)


class RunningLoopGuardTests(TestCase):
    """Inside a loop, the runner refuses rather than improvises.

    Worth being precise about what this guard is for. Calling ``plan_outreach()``
    from a coroutine never reaches it: phase 1 is an ORM read, so Django's own
    ``SynchronousOnlyOperation`` fires several lines earlier -- pinned below, so
    nobody has to rediscover it. ``_run_coroutine`` is guarded anyway because
    the obvious accommodation (spawn a thread, give it a loop, block on it)
    would be a fix in the wrong place: it rescues phase 3 while phases 1, 2, 4
    and 5 stay on the caller's async thread and fail anyway. There is no correct
    way to call ``plan_outreach()`` from inside a loop, so relocating the
    failure is the only thing an accommodation could buy.
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

        # DJANGO_ALLOW_ASYNC_UNSAFE is routine in notebook workflows and would
        # suppress Django's guard, letting the run reach `_run_coroutine` and
        # raise RuntimeError instead -- failing this test against a
        # correctly-behaving codebase. Cleared for the duration.
        env = {k: v for k, v in os.environ.items() if k != "DJANGO_ALLOW_ASYNC_UNSAFE"}

        # Not the message this module's guard would give -- Django's is fine,
        # and arrives first. Pinned so a future reordering of the phases (or a
        # move to async ORM reads) surfaces here rather than as a surprise.
        with mock.patch.dict(os.environ, env, clear=True):
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
