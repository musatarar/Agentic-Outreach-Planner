"""The planner's database cost, pinned (MUS-26d): the event prefetch, the
phase-5 ``bulk_create``, and what that write does to primary keys and timestamps."""

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

# The non-INSERT cost of a run, fixed with respect to lead count. In order:
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
# this constant is a decision: every increment is a query on the once-per-run path.
PLANNER_QUERIES_WITHOUT_INSERTS = 11

# Columns `bulk_create` writes per row. SQLite caps a batch at 999 // len(fields);
# Postgres does not cap, so the tests below compute the INSERT count from
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
    """The N+1 lock: ``_events_list`` is called from four places in a run, and
    the query count stays flat in lead count."""

    def test_a_twelve_lead_run_costs_a_fixed_number_of_queries(self):
        _make_leads(12)

        with _stub():
            with self.assertNumQueries(planner_queries(12)):
                planned = plan_outreach()

        self.assertEqual(len(planned), 12)

    @override_settings(COPY_VERIFY_LEVEL="standard")
    def test_the_count_holds_at_the_production_verification_level(self):
        """Every other test here runs at `off`, which skips the verifier's event
        walk -- so this case is what pins `verify_copy`'s share of the prefetch."""
        _make_leads(12)

        with _stub():
            with self.assertNumQueries(planner_queries(12)):
                plan_outreach()

    def test_the_query_count_does_not_grow_with_the_number_of_leads(self):
        """Two runs of different sizes cost the same *non-INSERT* number.

        The sizes straddle SQLite's 55-row batch boundary on purpose, so the
        comparison is of read cost only, not of how the writes batch.
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
        self.assertEqual(
            planner_queries(3) - _expected_inserts(3),
            planner_queries(60) - _expected_inserts(60),
        )

    def test_events_are_read_from_the_prefetch_cache_not_re_queried(self):
        # `.all()` is served from `_prefetched_objects_cache`; `.filter()` or
        # `.count()` would bypass it and restore the N+1.
        _make_leads(4)
        leads = list(Lead.objects.prefetch_related("events"))

        with self.assertNumQueries(0):
            for lead in leads:
                list(lead.events.all())


@override_settings(COPY_VERIFY_LEVEL="off")
class BulkCreateTests(TestCase):
    """What `bulk_create` does and does not do to the rows it writes."""

    def test_every_returned_row_has_a_primary_key(self):
        """``bulk_create`` populates PKs via ``RETURNING`` on Postgres and on
        SQLite >= 3.35; the serializer emits ``id`` for every planned row."""
        _make_leads(5)

        with _stub():
            planned = plan_outreach()

        self.assertTrue(all(action.pk is not None for action in planned))
        self.assertEqual(len({action.pk for action in planned}), 5)

    def test_created_at_is_populated(self):
        # `auto_now_add` fires in the field's `pre_save`, not `Model.save()`,
        # so it survives bulk_create.
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
        # `bulk_create` returns rows in `work` order; the sort after it is
        # what the frontend relies on.
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
        """One statement per batch -- SQLite caps a batch at 55 rows for this
        model's 18 insertable fields, Postgres does not cap at all."""
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
        # RETURNING is applied per batch, so pk population must hold across them.
        _make_leads(60)

        with _stub():
            planned = plan_outreach()

        self.assertEqual(len({action.pk for action in planned}), 60)

    def test_a_run_with_no_work_writes_nothing_and_still_succeeds(self):
        # `bulk_create([])` emits no INSERT, though the atomic wrapper and the
        # supersede DELETE still cost a few queries.
        with _stub():
            planned = plan_outreach()

        self.assertEqual(planned, [])
        self.assertEqual(OutreachAction.objects.count(), 0)


class BulkCreatePkBootCheckTests(SimpleTestCase):
    """The pk-population guarantee above, enforced at boot for the databases
    CI never sees."""

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
