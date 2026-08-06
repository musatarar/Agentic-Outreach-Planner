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

import math
from unittest.mock import patch

from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from project.app import checks
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

# The non-INSERT cost of a run. Fixed with respect to lead count, which is the
# whole claim. What it is made of, in order:
#
#   1  dismissed dedupe keys (the suppression ledger, read once)
#   2  open dedupe keys      (pending/snoozed, read once)
#   3  the leads
#   4  their events          <- the prefetch; this line used to be 7 x N
#   5-8 provider resolution  (LLMConfiguration x3 + LLMModel, inside get_llm_client)
#   9  the transaction open  (SAVEPOINT under TestCase; BEGIN in production)
#   10 supersede failed rows (MUS-26c)
#   11 the transaction close (RELEASE SAVEPOINT / COMMIT)
#
# Plus the INSERTs, which are NOT flat -- see PLANNER_INSERT_FIELDS. Bumping
# this is a decision, not a formality: every increment is a query someone added
# to a path that runs once per run, and a failing assertion is the only thing
# that makes that visible in review. Queries 5-8 are four reads to answer one
# question and are the obvious next thing to collapse -- left alone here because
# they are `get_llm_client`'s business, not the planner's, and because four is
# four whether there are 12 leads or 200.
PLANNER_QUERIES_WITHOUT_INSERTS = 11

# How many columns `bulk_create` writes per row. Django's SQLite backend caps a
# batch at `max_query_params (999) // len(fields)`; Postgres does not cap at all.
# So the INSERT count is backend-dependent, and the tests below compute it from
# `connection.ops.bulk_batch_size` rather than hardcoding either answer.
PLANNER_INSERT_FIELDS = 18


def _expected_inserts(rows):
    from project.app.models import OutreachAction

    fields = [f for f in OutreachAction._meta.concrete_fields if not f.primary_key]
    batch = connection.ops.bulk_batch_size(fields, range(rows))
    return math.ceil(rows / batch) if batch else 1


def planner_queries(rows):
    """Total queries for a run that writes ``rows`` actions, on this backend."""
    return PLANNER_QUERIES_WITHOUT_INSERTS + _expected_inserts(rows)


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
            with self.assertNumQueries(planner_queries(12)):
                planned = plan_outreach()

        self.assertEqual(len(planned), 12)

    @override_settings(COPY_VERIFY_LEVEL="standard")
    def test_the_count_holds_at_the_production_verification_level(self):
        """`off` is exactly the level that skips the verifier's event walk.

        Every other test here runs at `off` for determinism, which means the
        lock would not cover `verify_copy` -- one of the four places the prefetch
        exists for. Reverting the prefetch costs 94 queries at `off` and 142 at
        `standard`, so this is the case that pins the bigger half.
        """
        _make_leads(12)

        with _stub():
            with self.assertNumQueries(planner_queries(12)):
                plan_outreach()

    def test_the_query_count_does_not_grow_with_the_number_of_leads(self):
        """The actual claim, stated as a comparison rather than a constant.

        A single `assertNumQueries` pins today's number but would still pass if
        the run became `4 + N` and someone updated the constant. Two runs of
        different sizes costing the same *non-INSERT* number is the property no
        N+1 satisfies.

        The sizes straddle SQLite's 55-row batch boundary on purpose: 3 leads is
        one INSERT and 60 is two, so a test that only compared totals would fail
        for a reason that has nothing to do with N+1s. Comparing the non-INSERT
        cost separates "we read the same amount" from "we write in batches".
        """
        _make_leads(3)
        with _stub():
            with self.assertNumQueries(planner_queries(3)):
                plan_outreach()

        # Clear the run so the second one has work to do: an open recommendation
        # suppresses a re-run.
        OutreachAction.objects.update(status=OutreachAction.STATUS_APPROVED)
        _make_leads(57, offset=3)  # 60 leads -- past SQLite's 55-row batch

        with _stub():
            with self.assertNumQueries(planner_queries(60)):
                plan_outreach()

        self.assertEqual(Lead.objects.count(), 60)
        # The read cost is identical at 3 leads and at 60; only the write scaled,
        # and only on a backend that caps batch size.
        self.assertEqual(
            planner_queries(3) - _expected_inserts(3),
            planner_queries(60) - _expected_inserts(60),
        )

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

    def test_the_write_is_one_statement_per_batch(self):
        """Not "one statement" -- that is only true on Postgres.

        Django's SQLite backend caps a batch at `max_query_params (999) //
        len(fields)`, which is 55 rows for this model's 18 insertable fields.
        Postgres does not cap, so it sends one. A test that asserted `== 1`
        would pass at 6 leads and fail on the SQLite CI leg the first time
        someone raised a fixture past 55, with nothing explaining why.
        """
        from django.test.utils import CaptureQueriesContext

        rows = 60  # past SQLite's boundary, so the two backends genuinely differ
        _make_leads(rows)

        with _stub():
            with CaptureQueriesContext(connection) as captured:
                plan_outreach()

        inserts = [
            query["sql"]
            for query in captured.captured_queries
            if query["sql"].lstrip().upper().startswith("INSERT INTO")
            and "outreachaction" in query["sql"].lower()
        ]
        self.assertEqual(len(inserts), _expected_inserts(rows))
        # And whatever the batching, it is never one statement per lead.
        self.assertLess(len(inserts), rows)

    def test_primary_keys_survive_multiple_batches(self):
        # RETURNING is applied per batch, so pk population has to hold across
        # them -- the serializer emits `id` for every row, not just the first 55.
        _make_leads(60)

        with _stub():
            planned = plan_outreach()

        self.assertEqual(len({action.pk for action in planned}), 60)

    def test_a_run_with_no_work_writes_nothing_and_still_succeeds(self):
        # `bulk_create([])` emits no INSERT, though the run is not free: the
        # `transaction.atomic()` wrapper and the supersede DELETE still cost a
        # few queries. Cheap enough to leave unconditional -- a `if rows:` guard
        # would save two savepoint statements on a run that had nothing to do.
        with _stub():
            planned = plan_outreach()

        self.assertEqual(planned, [])
        self.assertEqual(OutreachAction.objects.count(), 0)


class BulkCreatePkBootCheckTests(SimpleTestCase):
    """The pk-population guarantee above, enforced at boot for the databases
    CI never sees (test_every_returned_row_has_a_primary_key covers the two
    CI legs, which both qualify)."""

    def test_a_qualifying_database_reports_nothing(self):
        self.assertEqual(checks.bulk_create_pk_check(None), [])

    def test_a_database_without_returning_fails_manage_py_check(self):
        # Patched on the features *class*: on SQLite the flag is a read-only
        # property, so an instance-level patch would have nothing to set.
        with patch.object(type(connection.features), "can_return_rows_from_bulk_insert", False):
            errors = checks.bulk_create_pk_check(None)

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].id, "app.E003")
        self.assertIn("bulk_create", errors[0].msg)
        self.assertIn("3.35", errors[0].msg)
