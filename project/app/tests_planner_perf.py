"""The planner's database cost, pinned (MUS-26d).

Two changes, one test module. Phase 1 prefetches events; phase 5 writes one
``bulk_create`` instead of one INSERT per lead. Both are the kind of fix that
works silently and regresses silently -- nothing fails when someone reintroduces
a per-lead query, the run just gets slower in a way that only shows up on a
dataset nobody runs locally. So the query count is an assertion.

``bulk_create`` also comes with two things that are commonly half-remembered:
whether it populates primary keys, and whether ``auto_now_add`` still fires.
Both are guarantees about the database and the field, not about our code, which
is exactly why they get tests rather than comments.
"""

from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from project.app.models import Event, Lead, OutreachAction
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

# The number the README cites. Fixed with respect to lead count, which is the
# whole claim. What it is made of, in order:
#
#   1  dismissed dedupe keys (the suppression ledger, read once)
#   2  open dedupe keys      (pending/snoozed, read once)
#   3  the leads
#   4  their events          <- the prefetch; this line used to be 4 x N
#   5-8 provider resolution  (LLMConfiguration x3 + LLMModel, inside get_llm_client)
#   9  BEGIN
#   10 one INSERT ... RETURNING id for every OutreachAction
#   11 COMMIT
#
# Bumping this is a decision, not a formality: every increment is a query
# someone added to a path that runs once per run, and a failing assertion is the
# only thing that makes that visible in review. Queries 5-8 are four reads to
# answer one question and are the obvious next thing to collapse -- left alone
# here because they are `get_llm_client`'s business, not the planner's, and
# because four is four whether there are 12 leads or 200.
PLANNER_QUERIES = 11


def _make_leads(count, events_each=3, offset=0):
    leads = []
    for index in range(offset, offset + count):
        lead = Lead.objects.create(
            id=f"lead_{index:03d}",
            agency_name=f"Agency {index:03d}",
            contact_name=f"Contact {index:03d}",
            contact_email=f"contact{index:03d}@example.com",
            contact_phone="555-0100",
            state="CA",
            num_producers=3,
            years_in_business=8,
            estimated_book_size_usd=1_000_000 + index,
            # demo_completed with no signup -> complete_onboarding, the one
            # classification that does not drift with the wall clock.
            stage="demo_completed",
            signed_up_date=None,
        )
        for event_index in range(events_each):
            Event.objects.create(
                lead=lead,
                type="login" if event_index % 2 else "email_sent",
                timestamp=timezone.now(),
                meta={"notes": f"note {event_index} for {lead.id}"},
            )
        leads.append(lead)
    return leads


def _stub(copy=GOOD_COPY):
    return patch("project.app.services.outreach.agenerate_copy", return_value=copy)


@override_settings(COPY_VERIFY_LEVEL="off")
class PlannerQueryCountTests(TestCase):
    """The N+1 lock.

    ``_events_list`` is called from four places in a run -- ``_notes_blob``
    while classifying, ``_format_events_for_prompt`` while building the prompt,
    ``verify_copy`` in phase 4 and ``explain`` in phase 5 -- so before the
    prefetch, a twelve-lead run paid dozens of queries just to re-read the same
    events. The count below is flat in lead count, and that is the assertion.
    """

    def test_a_twelve_lead_run_costs_a_fixed_number_of_queries(self):
        _make_leads(12)

        with _stub():
            with self.assertNumQueries(PLANNER_QUERIES):
                planned = plan_outreach()

        self.assertEqual(len(planned), 12)

    def test_the_query_count_does_not_grow_with_the_number_of_leads(self):
        """The actual claim, stated as a comparison rather than a constant.

        A single ``assertNumQueries`` pins today's number but would still pass
        if the run became, say, ``4 + N`` and someone updated the constant. Two
        runs of different sizes costing the *same* number of queries is the
        property that cannot be satisfied by an N+1.
        """
        _make_leads(3)
        with _stub():
            with self.assertNumQueries(PLANNER_QUERIES):
                plan_outreach()

        # Clear the run so the second one has work to do: an open recommendation
        # suppresses a re-run (CONTRACT 9.8).
        OutreachAction.objects.update(status=OutreachAction.STATUS_APPROVED)
        _make_leads(21, offset=3)  # 24 leads and 72 events in the second run

        with _stub():
            with self.assertNumQueries(PLANNER_QUERIES):
                plan_outreach()

        self.assertEqual(Lead.objects.count(), 24)
        self.assertEqual(Event.objects.count(), 72)

    def test_events_are_read_from_the_prefetch_cache_not_re_queried(self):
        # The mechanism, not just the count. `.all()` on a prefetched instance
        # is served from `_prefetched_objects_cache`; `.filter()` or `.count()`
        # would silently bypass it and restore the N+1 while this module's
        # totals still looked plausible on a small dataset.
        _make_leads(4)
        leads = list(Lead.objects.prefetch_related("events"))

        with self.assertNumQueries(0):
            for lead in leads:
                list(lead.events.all())


@override_settings(COPY_VERIFY_LEVEL="off")
class BulkCreateTests(TestCase):
    """What `bulk_create` does and does not do to the rows it writes."""

    def test_every_returned_row_has_a_primary_key(self):
        """Not an assumption -- a guarantee about the database.

        Django populates PKs from ``bulk_create`` using ``RETURNING`` on
        Postgres and on SQLite >= 3.35. Both CI legs qualify, but the serializer
        emits ``id`` and the triage queue addresses rows by it, so a run
        returning pk-less objects would serialize ``"id": null`` into the API
        response and the inbox would break on a row it could not name. If this
        ever fails, the fix is a re-query in phase 5, not a shrug.
        """
        _make_leads(5)

        with _stub():
            planned = plan_outreach()

        self.assertTrue(all(action.pk is not None for action in planned))
        self.assertEqual(len({action.pk for action in planned}), 5)

    def test_created_at_is_populated(self):
        # `auto_now_add` is applied by the field's own `pre_save`, not by
        # `Model.save()`, so it survives bulk_create -- but "bulk_create skips
        # save()" is exactly the half-remembered fact that turns into a null
        # column in production.
        _make_leads(3)

        with _stub():
            planned = plan_outreach()

        for action in planned:
            self.assertIsNotNone(action.created_at)
        # And the value reached the database, not just the in-memory instance.
        self.assertEqual(OutreachAction.objects.filter(created_at__isnull=True).count(), 0)

    def test_the_rows_actually_land_in_the_database_with_their_fields(self):
        _make_leads(2)

        with _stub():
            plan_outreach()

        self.assertEqual(OutreachAction.objects.count(), 2)
        stored = OutreachAction.objects.get(lead_id="lead_000")
        self.assertEqual(stored.suggested_copy, GOOD_COPY)
        self.assertEqual(stored.action_type, "complete_onboarding")
        self.assertNotEqual(stored.dedupe_key, "")
        self.assertNotEqual(stored.rule_trace, {})
        self.assertFalse(stored.needs_human)

    def test_the_returned_list_is_still_priority_sorted(self):
        # `bulk_create` returns rows in the order it was given them, which is
        # `work` order, not priority order. The sort after it is what the
        # frontend relies on.
        _make_leads(4)
        Lead.objects.filter(id="lead_000").update(
            estimated_book_size_usd=25_000_000, deals_closed=9, quotes_submitted=40
        )
        Lead.objects.filter(id="lead_001").update(estimated_book_size_usd=8_000_000)

        with _stub():
            planned = plan_outreach()

        priorities = [action.priority for action in planned]
        self.assertEqual(priorities, sorted(priorities))
        self.assertGreater(len(set(priorities)), 1)  # a flat list sorts trivially

    def test_the_write_is_one_statement(self):
        from django.test.utils import CaptureQueriesContext

        _make_leads(6)

        with _stub():
            with CaptureQueriesContext(connection) as captured:
                plan_outreach()

        inserts = [
            query["sql"]
            for query in captured.captured_queries
            if query["sql"].lstrip().upper().startswith("INSERT INTO")
            and "outreachaction" in query["sql"].lower()
        ]
        self.assertEqual(len(inserts), 1, f"expected one INSERT, got {len(inserts)}")

    def test_a_run_with_no_work_writes_nothing_and_still_succeeds(self):
        # `bulk_create([])` is legal and emits no query, but the empty run is
        # the one that used to be free by accident (an empty comprehension) and
        # is now free on purpose.
        with _stub():
            planned = plan_outreach()

        self.assertEqual(planned, [])
        self.assertEqual(OutreachAction.objects.count(), 0)
