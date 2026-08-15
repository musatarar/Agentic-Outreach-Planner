"""Component artifact: lifecycle (MUS-47 component 4).

The run state machine and the endpoints in front of it: one active run at a
time, the free classify stage that writes a ``RunLead`` per scoped lead without
constructing an LLM client, and the two terminal exits that hand the slot back.

Four properties get the sharpest tests, because all four fail silently: one
active run is the *database's* answer and not a Python ``if``; re-classify
replaces rather than appends; both terminal exits are reachable over HTTP; and
the wire shape is the product, since ``lead_count`` is the frontend's zero-lead
gate and ``GET /api/runs/{id}/`` is where every ``RunLeadRow`` comes from.

Red at skeleton: every symbol these tests reach for lands with the lifecycle PR,
so the imports that name them sit inside the test bodies, where a missing symbol
is one failing test rather than a dead module.
"""

import unittest
from datetime import date
from decimal import Decimal
from unittest import mock

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from project.app.models import Event, Lead, OutreachAction
from project.app.services import dedupe, outreach
from project.app.tests_auth_utils import AuthenticatedAPITestCase

# Every fixture lead below is `demo_completed` with no signup, which the rules
# classify as `complete_onboarding` -- the one action they reach without
# consulting the wall clock, so nothing in this module drifts overnight.
SCOPE_ALL = {"stage": "demo_completed"}

# The contract's "Wire shapes" section, transcribed. `run_leads` is deliberately
# absent: it rides only on GET /api/runs/{id}/ and /classify/, so this is what
# every other run response must equal exactly.
RUN_DETAIL_KEYS = frozenset(
    "id status scope lead_count spread classify_ms discarded_suggestions "
    "read_provider read_model generate_provider generate_model "
    "read_cost_estimate_usd read_cost_actual_usd "
    "generate_cost_estimate_usd generate_cost_actual_usd "
    "created_at created_by finished_at".split()
)

RUN_LEAD_ROW_KEYS = frozenset(
    "lead_id agency_name contact_name "
    "rules_priority rules_action rules_reason "
    "effective_priority effective_action effective_reason "
    "rule_trace already_queued selected "
    "suggestion suggestion_state suggestion_decided_at suggestion_decided_by "
    "generated_action_id generation_error".split()
)

# `outreach.explain()`'s schema-v1 envelope -- the real keys, not an invented
# shape: `RunLead.rule_trace` stores exactly what `OutreachAction.rule_trace`
# stores, which is what lets MUS-40's RuleTrace component render either.
TRACE_KEYS = frozenset("version today generated_at priority action".split())


def make_lead(lead_id, **overrides):
    """A scoped lead carrying events. The two events are load-bearing:
    `_classify_action` reads `lead.events` for every lead it touches, so an
    eventless fixture would let an unprefetched classify pass the query-count
    test below."""
    defaults = dict(
        id=lead_id,
        agency_name=f"Agency {lead_id}",
        contact_name=f"Contact {lead_id}",
        contact_email=f"{lead_id}@example.com",
        contact_phone="555-0100",
        state="CA",
        num_producers=3,
        years_in_business=5,
        estimated_book_size_usd=1_000_000,
        stage="demo_completed",
        signed_up_date=None,
        last_login_date=date(2026, 6, 1),
        quotes_created=10,
        quotes_submitted=4,
        deals_closed=1,
        last_contacted_date=date(2026, 5, 1),
        hubspot_notes="",
    )
    defaults.update(overrides)
    lead = Lead.objects.create(**defaults)
    for event_type in ("login", "quote_created"):
        Event.objects.create(lead=lead, type=event_type, timestamp=timezone.now(), meta={})
    return lead


def open_action_for(lead):
    """A pending OutreachAction already holding this lead's dedupe slot, keyed off
    `determine_action` so it cannot drift away from the recommendation it is meant
    to collide with the day a rule changes."""
    action_type, reason = outreach.determine_action(lead)
    return OutreachAction.objects.create(
        lead=lead,
        priority=2,
        action_type=action_type,
        reason=reason,
        suggested_copy="Subject: Hello\n\nHi there,\n\nA nudge.\n\nBest,\nDana",
        status=OutreachAction.STATUS_PENDING,
        dedupe_key=dedupe.dedupe_key(lead.id, action_type),
    )


class RunLifecycleServiceTests(TestCase):
    """`services/compose/runs.py` -- the state machine with no HTTP in the way."""

    @unittest.expectedFailure
    def test_create_run_opens_a_draft_holding_the_active_slot(self):
        """`scope` is stored as `validate_scope` returned it -- coercion happens
        once, at the door -- and is compared against the validator's own output
        because those rules belong to the scope component."""
        from project.app.models import PlannerRun
        from project.app.services.compose import runs
        from project.app.services.compose import scope as scope_service

        run = runs.create_run(scope=SCOPE_ALL, created_by="tester@example.com")

        self.assertEqual(run.status, PlannerRun.STATUS_DRAFT)
        # True, not None: the partial unique index only counts True rows.
        self.assertTrue(run.active_sentinel)
        self.assertEqual(run.created_by, "tester@example.com")
        self.assertEqual(run.scope, scope_service.validate_scope(SCOPE_ALL))
        self.assertIsNone(run.finished_at)
        self.assertEqual(run.finished_by, "")
        self.assertEqual(run.trace_run_id, "")  # minted at generate, not here
        self.assertEqual(PlannerRun.objects.count(), 1)

    @unittest.expectedFailure
    def test_a_second_create_raises_run_conflict_naming_the_incumbent(self):
        """The conflict is the database's answer: `create_run` inserts and lets
        `pr_one_active_run` reject the loser, so two concurrent callers cannot both
        read "no active run" and both win. It names the incumbent because opening
        it is the only useful next move the UI has."""
        from project.app.models import PlannerRun
        from project.app.services.compose import runs

        first = runs.create_run(scope=SCOPE_ALL, created_by="first@example.com")

        with self.assertRaises(runs.RunConflict) as caught:
            runs.create_run(scope=SCOPE_ALL, created_by="second@example.com")

        self.assertEqual(caught.exception.active_run_id, first.pk)
        # Reachable only if the failed INSERT was rolled back to a savepoint. An
        # IntegrityError that escapes one poisons the atomic block, and this
        # raises TransactionManagementError instead of returning 1.
        self.assertEqual(PlannerRun.objects.count(), 1)

    @unittest.expectedFailure
    def test_active_run_sees_exactly_the_run_holding_the_slot(self):
        """The composer's front door -- "start a scope" or "resume mid-run". None
        rather than raising, and a terminal run is never mistaken for a live one
        because releasing the sentinel and ending the run are one write."""
        from project.app.services.compose import runs

        self.assertIsNone(runs.active_run())

        run = runs.create_run(scope=SCOPE_ALL, created_by="tester@example.com")
        self.assertEqual(runs.active_run().pk, run.pk)

        runs.discard_run(run, actor="tester@example.com")
        self.assertIsNone(runs.active_run())

    @unittest.expectedFailure
    def test_classify_writes_one_row_per_scoped_lead_with_the_rules_snapshot(self):
        """Every scoped lead gets exactly one row -- including leads a planner run
        would have skipped -- because the composer shows the whole scope rather
        than dropping rows from under a filter the operator just wrote.
        `effective_*` starts equal to `rules_*`: the model has not spoken, and if
        it never does, stage 04 generates what the rules said.
        """
        from project.app.models import PlannerRun, RunLead
        from project.app.services.compose import runs

        leads = [make_lead(f"lead_{index:03d}") for index in range(3)]
        run = runs.create_run(scope=SCOPE_ALL, created_by="tester@example.com")

        today = date.today()
        classified = runs.classify_run(run)
        self.assertEqual(today, date.today(), "the run straddled midnight")

        self.assertEqual(classified.status, PlannerRun.STATUS_CLASSIFIED)
        self.assertIsNotNone(classified.classify_ms)  # the free stage still reports its duration
        rows = {row.lead_id: row for row in classified.run_leads.all()}
        self.assertEqual(set(rows), {lead.id for lead in leads})
        for lead_id, row in rows.items():
            with self.subTest(lead=lead_id):
                self.assertIn(row.rules_priority, (1, 2, 3))
                self.assertTrue(row.rules_action)
                self.assertTrue(row.rules_reason)
                # The key IS the identity of the recommendation, derived from the
                # action the rules just chose -- never recomputed later from
                # something that might disagree with the row it sits on.
                self.assertEqual(row.dedupe_key, dedupe.dedupe_key(lead_id, row.rules_action))
                self.assertEqual(set(row.rule_trace), set(TRACE_KEYS))
                self.assertEqual(row.rule_trace["version"], outreach.TRACE_SCHEMA_VERSION)
                self.assertEqual(row.rule_trace["priority"]["value"], row.rules_priority)
                self.assertEqual(row.rule_trace["action"]["value"], row.rules_action)
                self.assertEqual(row.effective_priority, row.rules_priority)
                self.assertEqual(row.effective_action, row.rules_action)
                self.assertEqual(row.effective_reason, row.rules_reason)
                self.assertFalse(row.selected)
                self.assertEqual(row.suggestion, {})
                self.assertEqual(row.suggestion_state, RunLead.SUGGESTION_NONE)
                self.assertIsNone(row.generated_action_id)

        # The stored trace is `explain()`'s envelope, not a lookalike carrying
        # the same key names: one row compared whole against a fresh call, modulo
        # the wall-clock stamp.
        expected = outreach.explain(Lead.objects.get(id=leads[0].id), today)
        self.assertEqual(
            {k: v for k, v in rows[leads[0].id].rule_trace.items() if k != "generated_at"},
            {k: v for k, v in expected.items() if k != "generated_at"},
        )

    @unittest.expectedFailure
    def test_classify_constructs_no_llm_client_and_queues_nothing(self):
        """Stage 02 is the free one, and "free" is a property with a test.

        The patch goes on `_build_client`, the single lru_cache'd constructor
        both `get_llm_client` and `build_client` funnel through, so it holds
        however `runs.py` spells its import -- a patch aimed at either wrapper
        binds nothing against a `from ... import`, and `assert_not_called` then
        passes by construction. The positive control is what proves it is live.
        """
        from project.app.services import llm
        from project.app.services.compose import runs

        make_lead("lead_001")
        make_lead("lead_002")
        run = runs.create_run(scope=SCOPE_ALL, created_by="tester@example.com")

        with mock.patch("project.app.services.llm._build_client") as build_client:
            runs.classify_run(run)
            build_client.assert_not_called()

            llm.get_llm_client()  # positive control: the same patch, through the factory
            self.assertEqual(build_client.call_count, 1)

        # Nothing queued either: no OutreachAction exists until stage 04, and
        # none exists then without a human having selected the row.
        self.assertEqual(OutreachAction.objects.count(), 0)
        self.assertEqual(run.run_leads.count(), 2)  # ...and it still did its own job

    @unittest.expectedFailure
    def test_already_queued_flags_the_lead_whose_slot_is_taken_and_only_it(self):
        """An open triage item already holds this recommendation's dedupe slot, so
        generating again would double the inbox. The composer marks the row rather
        than dropping it: a lead that vanishes from a scope of forty is
        indistinguishable from a bug in the filter the operator just typed.
        """
        from project.app.services.compose import runs

        taken = make_lead("lead_001")
        make_lead("lead_002")
        make_lead("lead_003")
        open_action_for(taken)

        run = runs.create_run(scope=SCOPE_ALL, created_by="tester@example.com")
        runs.classify_run(run)

        flags = {row.lead_id: row.already_queued for row in run.run_leads.all()}
        self.assertEqual(flags, {"lead_001": True, "lead_002": False, "lead_003": False})

    @unittest.expectedFailure
    def test_reclassify_replaces_the_rows_rather_than_appending(self):
        """Stage 01 stays editable, so re-classify is the normal path. The row set
        must describe the *current* scope exactly: an append leaves dropped leads
        behind -- still listed, still selectable -- and stage 04 then spends money
        on leads the operator filtered out.
        """
        from project.app.models import PlannerRun
        from project.app.services.compose import runs

        make_lead("lead_small", estimated_book_size_usd=500_000)
        make_lead("lead_big", estimated_book_size_usd=5_000_000)
        run = runs.create_run(scope=SCOPE_ALL, created_by="tester@example.com")
        runs.classify_run(run)
        self.assertEqual({row.lead_id for row in run.run_leads.all()}, {"lead_small", "lead_big"})

        run.scope = {"stage": "demo_completed", "book_min": 1_000_000}
        run.save(update_fields=["scope"])
        runs.classify_run(run)

        self.assertEqual({row.lead_id for row in run.run_leads.all()}, {"lead_big"})
        self.assertEqual(run.run_leads.count(), 1)  # one row, not two: the set, not the union
        run.refresh_from_db()
        # classified -> classified is a legal self-transition, on purpose.
        self.assertEqual(run.status, PlannerRun.STATUS_CLASSIFIED)

    @unittest.expectedFailure
    def test_close_run_completes_it_stamps_the_finish_and_frees_the_slot(self):
        """The sentinel goes NULL in the same write that sets the status: what the
        check constraint demands, and what makes the slot genuinely free rather
        than merely relabelled.
        """
        from project.app.models import PlannerRun
        from project.app.services.compose import runs

        run = runs.create_run(scope=SCOPE_ALL, created_by="tester@example.com")
        # `completed` is reachable only from `generated`, and stage 04 belongs to
        # another component -- so the run is placed there directly.
        PlannerRun.objects.filter(pk=run.pk).update(status=PlannerRun.STATUS_GENERATED)
        run.refresh_from_db()

        closed = runs.close_run(run, actor="closer@example.com")

        self.assertEqual(closed.status, PlannerRun.STATUS_COMPLETED)
        self.assertIsNone(closed.active_sentinel)
        self.assertIsNotNone(closed.finished_at)
        self.assertEqual(closed.finished_by, "closer@example.com")
        self.assertIsNone(runs.active_run())
        # The assertion that matters: the slot is available again.
        successor = runs.create_run(scope=SCOPE_ALL, created_by="next@example.com")
        self.assertNotEqual(successor.pk, run.pk)

    @unittest.expectedFailure
    def test_discard_run_is_reachable_from_a_draft_and_frees_the_slot(self):
        """The escape hatch, legal from every active status -- including `draft`,
        exactly where an operator lands on realising the scope is wrong. Same
        terminal shape as close, different status.
        """
        from project.app.models import PlannerRun
        from project.app.services.compose import runs

        run = runs.create_run(scope=SCOPE_ALL, created_by="tester@example.com")
        self.assertEqual(run.status, PlannerRun.STATUS_DRAFT)

        discarded = runs.discard_run(run, actor="quitter@example.com")

        self.assertEqual(discarded.status, PlannerRun.STATUS_DISCARDED)
        self.assertIsNone(discarded.active_sentinel)
        self.assertIsNotNone(discarded.finished_at)
        self.assertEqual(discarded.finished_by, "quitter@example.com")
        self.assertIsNone(runs.active_run())
        successor = runs.create_run(scope=SCOPE_ALL, created_by="next@example.com")
        self.assertNotEqual(successor.pk, run.pk)

    @unittest.expectedFailure
    def test_an_illegal_transition_raises_instead_of_no_opping(self):
        """A silent no-op returns success with a stale row set, and the operator
        spends ten minutes wondering why their filter change did nothing. Both
        directions: a stage on a terminal run, and `close` from a status that
        cannot reach `completed`.
        """
        from project.app.services.compose import runs

        make_lead("lead_001")
        run = runs.create_run(scope=SCOPE_ALL, created_by="tester@example.com")

        with self.assertRaises(runs.InvalidRunTransition):
            runs.close_run(run, actor="tester@example.com")  # draft -> completed is not a road

        runs.discard_run(run, actor="tester@example.com")
        with self.assertRaises(runs.InvalidRunTransition):
            runs.classify_run(run)
        self.assertEqual(run.run_leads.count(), 0)  # the refused stage wrote nothing

    @unittest.expectedFailure
    def test_a_stage_on_a_vanished_run_raises_run_not_found(self):
        """`RunNotFound` is the lifecycle layer's "no such run" -- a `LookupError`
        callers can catch generically, and the exception the run views turn into
        the contract's 404. Each stage re-reads its row before transitioning,
        because a blind UPDATE keyed off a stale instance touches zero rows and
        reports success. Both stages below are legal from `draft`, so a refusal
        can only be the missing row, never a transition check.
        """
        from project.app.models import PlannerRun
        from project.app.services.compose import runs

        self.assertTrue(issubclass(runs.RunNotFound, LookupError))

        run = runs.create_run(scope=SCOPE_ALL, created_by="tester@example.com")
        PlannerRun.objects.filter(pk=run.pk).delete()

        for name, stage in (
            ("classify", lambda: runs.classify_run(run)),
            ("discard", lambda: runs.discard_run(run, actor="tester@example.com")),
        ):
            with self.subTest(stage=name), self.assertRaises(runs.RunNotFound):
                stage()

    @unittest.expectedFailure
    def test_classify_query_count_does_not_grow_with_the_scope(self):
        """`_classify_action` touches `lead.events` for every lead, so the scoped
        queryset has to prefetch or the wider run costs ten times the queries. Ten
        times the rows on an unchanged query count is the property; the row count
        keeps a classify that quietly did nothing from passing.
        """
        from project.app.services.compose import runs

        for index in range(3):
            make_lead(f"lead_{index:03d}")
        first = runs.create_run(scope=SCOPE_ALL, created_by="tester@example.com")
        with CaptureQueriesContext(connection) as small:
            runs.classify_run(first)
        runs.discard_run(first, actor="tester@example.com")

        for index in range(3, 30):
            make_lead(f"lead_{index:03d}")
        second = runs.create_run(scope=SCOPE_ALL, created_by="tester@example.com")
        with CaptureQueriesContext(connection) as large:
            runs.classify_run(second)

        self.assertEqual(second.run_leads.count(), 30)
        self.assertEqual(len(large), len(small))


class RunEndpointTests(AuthenticatedAPITestCase):
    """The HTTP surface for the same state machine. Paths are written out rather
    than reversed: the contract freezes the URLs, not the view names, so a test
    that reverses a name would pass against a route the FE cannot reach.
    """

    def _post(self, path, payload=None):
        return self.client.post(
            path, payload if payload is not None else {}, content_type="application/json"
        )

    @unittest.expectedFailure
    def test_post_runs_creates_a_draft_carrying_the_full_run_detail(self):
        """201 with the whole `RunDetail`, not a stub the FE has to re-fetch: stage
        01 renders from it, and `lead_count` is the zero-lead gate from the first
        paint. `created_by` comes off the session -- it is audit attribution, so
        the value posted below is ignored outright.
        """
        from project.app.models import PlannerRun

        resp = self._post("/api/runs/", {"scope": SCOPE_ALL, "created_by": "forged@example.com"})

        self.assertEqual(resp.status_code, 201)
        # No `run_leads`: that key rides only on the detail read and on classify.
        self.assertEqual(set(resp.data), set(RUN_DETAIL_KEYS))
        self.assertEqual(resp.data["status"], "draft")
        self.assertEqual(resp.data["scope"], SCOPE_ALL)
        self.assertEqual(resp.data["lead_count"], 0)
        self.assertIsNone(resp.data["spread"])  # null until classified
        self.assertIsNone(resp.data["classify_ms"])
        self.assertEqual(resp.data["discarded_suggestions"], 0)
        self.assertEqual(resp.data["read_provider"], "")
        self.assertEqual(resp.data["generate_model"], "")
        self.assertIsNone(resp.data["read_cost_estimate_usd"])
        self.assertIsNone(resp.data["generate_cost_actual_usd"])
        self.assertIsNone(resp.data["finished_at"])
        self.assertEqual(resp.data["created_by"], self.TEST_EMAIL)
        run = PlannerRun.objects.get(pk=resp.data["id"])
        self.assertEqual(run.created_by, self.TEST_EMAIL)
        self.assertTrue(run.active_sentinel)

    @unittest.expectedFailure
    def test_post_runs_409s_on_an_active_run_the_view_never_created(self):
        """The incumbent is inserted straight through the ORM, so the view had no
        hand in creating it -- the 409 has to come from the write itself. `code`
        is the machine slug `client.ts` branches on; `active_run_id` is the extra
        key that lets the FE offer "open the run in progress" rather than refuse.
        """
        from project.app.models import PlannerRun

        incumbent = PlannerRun.objects.create(scope={}, created_by="someone@example.com")

        resp = self._post("/api/runs/", {"scope": SCOPE_ALL})

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["code"], "run_active")
        self.assertTrue(resp.data["detail"])
        self.assertEqual(resp.data["active_run_id"], incumbent.pk)
        self.assertEqual(PlannerRun.objects.count(), 1)  # the failed INSERT was rolled back
        # The mechanism the view must lean on: the constraint refuses a second
        # active row on its own, which is why a read-then-write guard is
        # unnecessary and, under two concurrent POSTs, wrong.
        with self.assertRaises(IntegrityError), transaction.atomic():
            PlannerRun.objects.create(scope={}, created_by="third@example.com")

    @unittest.expectedFailure
    def test_get_active_run_404s_when_there_is_none_then_returns_it(self):
        """404 rather than 200-with-null: a null body is indistinguishable from a
        serializer that dropped a field or a fetch against the wrong endpoint.
        """
        empty = self.client.get("/api/runs/active/")
        self.assertEqual(empty.status_code, 404)
        self.assertEqual(empty.data["code"], "no_active_run")
        self.assertTrue(empty.data["detail"])

        created = self._post("/api/runs/", {"scope": SCOPE_ALL})
        found = self.client.get("/api/runs/active/")

        self.assertEqual(found.status_code, 200)
        self.assertEqual(set(found.data), set(RUN_DETAIL_KEYS))  # rows come from the detail read
        self.assertEqual(found.data["id"], created.data["id"])
        self.assertEqual(found.data["status"], "draft")

    @unittest.expectedFailure
    def test_classify_endpoint_moves_the_run_and_returns_the_rows(self):
        """The updated detail *with* its rows, not a bare acknowledgement: the FE
        re-renders the stage strip and the lead table off this one response, so
        anything less is a second round trip before the operator sees anything.
        """
        from project.app.models import RunLead

        make_lead("lead_001")
        make_lead("lead_002")
        run_id = self._post("/api/runs/", {"scope": SCOPE_ALL}).data["id"]

        resp = self._post(f"/api/runs/{run_id}/classify/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(resp.data), set(RUN_DETAIL_KEYS) | {"run_leads"})
        self.assertEqual(resp.data["id"], run_id)
        self.assertEqual(resp.data["status"], "classified")
        self.assertEqual(resp.data["lead_count"], 2)
        self.assertEqual(sum(resp.data["spread"].values()), 2)
        self.assertIsNotNone(resp.data["classify_ms"])
        self.assertEqual(len(resp.data["run_leads"]), 2)
        self.assertEqual(RunLead.objects.filter(run_id=run_id).count(), 2)

    @unittest.expectedFailure
    def test_get_run_detail_carries_every_row_the_frontend_renders(self):
        """`GET /api/runs/{id}/` is where every `RunLeadRow` on the FE comes from,
        so the row shape is pinned here rather than left to whichever stage wrote
        it. `lead_count` cannot be read off `status` -- a classified run over an
        empty scope is `classified` with nothing in it -- and `spread`'s keys are
        JSON object keys, strings rather than ints, which is what survives the
        wire and what `Record<string, number>` expects.
        """
        make_lead("lead_001")
        make_lead("lead_002")
        run_id = self._post("/api/runs/", {"scope": SCOPE_ALL}).data["id"]
        self._post(f"/api/runs/{run_id}/classify/")

        resp = self.client.get(f"/api/runs/{run_id}/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(resp.data), set(RUN_DETAIL_KEYS) | {"run_leads"})
        self.assertEqual(resp.data["lead_count"], 2)
        self.assertEqual(sum(resp.data["spread"].values()), resp.data["lead_count"])
        self.assertTrue(all(isinstance(band, str) for band in resp.data["spread"]))

        rows = {row["lead_id"]: row for row in resp.data["run_leads"]}
        self.assertEqual(set(rows), {"lead_001", "lead_002"})
        row = rows["lead_001"]
        self.assertEqual(set(row), set(RUN_LEAD_ROW_KEYS))
        # Joined from the lead, so the table renders from this one response.
        self.assertEqual(row["agency_name"], "Agency lead_001")
        self.assertEqual(row["contact_name"], "Contact lead_001")
        self.assertEqual(row["effective_priority"], row["rules_priority"])
        self.assertEqual(row["effective_action"], row["rules_action"])
        self.assertEqual(row["effective_reason"], row["rules_reason"])
        self.assertEqual(set(row["rule_trace"]), set(TRACE_KEYS))
        self.assertFalse(row["already_queued"])
        self.assertFalse(row["selected"])
        # `{}` is the one shape meaning "nothing was ever proposed". A read that
        # ran and declined still writes all five keys.
        self.assertEqual(row["suggestion"], {})
        self.assertEqual(row["suggestion_state"], "none")
        self.assertIsNone(row["suggestion_decided_at"])
        self.assertEqual(row["suggestion_decided_by"], "")
        self.assertIsNone(row["generated_action_id"])
        self.assertEqual(row["generation_error"], "")

    @unittest.expectedFailure
    def test_run_detail_serializes_money_as_strings_not_floats(self):
        """`REST_FRAMEWORK` does not set `COERCE_DECIMAL_TO_STRING`, so DRF's
        default applies and every cost column leaves as a JSON string. That is
        correct -- float money loses cents -- and the FE types all four as
        `string | null` and parses with `Number()` at the edge. The trailing zeros
        matter too: `"0.0200"` is the 4dp column rendered faithfully; `0.02` is not.
        """
        from project.app.models import PlannerRun

        run_id = self._post("/api/runs/", {"scope": SCOPE_ALL}).data["id"]
        PlannerRun.objects.filter(pk=run_id).update(
            read_cost_estimate_usd=Decimal("0.0200"),
            read_cost_actual_usd=Decimal("0.0187"),
            generate_cost_estimate_usd=Decimal("0.1372"),
            generate_cost_actual_usd=Decimal("0.1401"),
        )

        data = self.client.get(f"/api/runs/{run_id}/").data

        self.assertEqual(data["read_cost_estimate_usd"], "0.0200")
        self.assertEqual(data["read_cost_actual_usd"], "0.0187")
        self.assertEqual(data["generate_cost_estimate_usd"], "0.1372")
        self.assertEqual(data["generate_cost_actual_usd"], "0.1401")

    @unittest.expectedFailure
    def test_close_endpoint_completes_the_run_and_hands_the_slot_back(self):
        """Driven end to end, because a terminal state only reachable from a Python
        shell is unshipped scope. The final POST is the real assertion: a status
        flipping to `completed` is easy to get half-right, and a fresh run
        succeeding afterwards is what proves the sentinel was released.
        """
        from project.app.models import PlannerRun

        make_lead("lead_001")
        run_id = self._post("/api/runs/", {"scope": SCOPE_ALL}).data["id"]
        self._post(f"/api/runs/{run_id}/classify/")
        PlannerRun.objects.filter(pk=run_id).update(status=PlannerRun.STATUS_GENERATED)

        resp = self._post(f"/api/runs/{run_id}/close/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(resp.data), set(RUN_DETAIL_KEYS))
        self.assertEqual(resp.data["status"], "completed")
        self.assertIsNotNone(resp.data["finished_at"])
        run = PlannerRun.objects.get(pk=run_id)
        self.assertIsNone(run.active_sentinel)
        self.assertEqual(run.finished_by, self.TEST_EMAIL)
        self.assertEqual(self.client.get("/api/runs/active/").status_code, 404)
        self.assertEqual(self._post("/api/runs/", {"scope": SCOPE_ALL}).status_code, 201)

    @unittest.expectedFailure
    def test_discard_endpoint_discards_the_run_and_hands_the_slot_back(self):
        """The other terminal exit. Discard is the one an operator actually reaches
        for -- wrong scope, wrong day, changed their mind -- so it has to work from
        a draft, and the slot has to come back or the tool is bricked.
        """
        from project.app.models import PlannerRun

        run_id = self._post("/api/runs/", {"scope": SCOPE_ALL}).data["id"]

        resp = self._post(f"/api/runs/{run_id}/discard/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(resp.data), set(RUN_DETAIL_KEYS))
        self.assertEqual(resp.data["status"], "discarded")
        self.assertIsNotNone(resp.data["finished_at"])
        run = PlannerRun.objects.get(pk=run_id)
        self.assertIsNone(run.active_sentinel)
        self.assertEqual(run.finished_by, self.TEST_EMAIL)
        self.assertEqual(self.client.get("/api/runs/active/").status_code, 404)
        self.assertEqual(self._post("/api/runs/", {"scope": SCOPE_ALL}).status_code, 201)

    @unittest.expectedFailure
    def test_classifying_a_discarded_run_is_a_409_not_a_silent_no_op(self):
        """A stale browser tab is the ordinary way this happens: discarded in
        another tab, still showing a Classify button here. 409 with a named `code`
        so the FE can say what went wrong and reload -- a 200 that changed nothing
        leaves the operator staring at the old row set believing it is current.
        """
        from project.app.models import PlannerRun, RunLead

        make_lead("lead_001")
        run_id = self._post("/api/runs/", {"scope": SCOPE_ALL}).data["id"]
        self._post(f"/api/runs/{run_id}/discard/")

        resp = self._post(f"/api/runs/{run_id}/classify/")

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["code"], "invalid_transition")
        self.assertTrue(resp.data["detail"])
        self.assertEqual(PlannerRun.objects.get(pk=run_id).status, "discarded")
        self.assertEqual(RunLead.objects.filter(run_id=run_id).count(), 0)

    @unittest.expectedFailure
    def test_unknown_run_id_is_a_contract_shaped_404(self):
        """`RunNotFound` reaches the wire as the same envelope every other miss in
        this API uses, so the FE has one branch for "gone" rather than one per
        endpoint. `code` is the key `client.ts` reads into `ApiError.code` as "the
        machine slug callers branch on"; an envelope keyed `error` is one the
        frontend cannot branch on at all, so its absence is asserted directly.
        """
        for path in (
            "/api/runs/9999/",
            "/api/runs/9999/classify/",
            "/api/runs/9999/close/",
            "/api/runs/9999/discard/",
        ):
            with self.subTest(path=path):
                resp = self.client.get(path) if path.endswith("9999/") else self._post(path)
                self.assertEqual(resp.status_code, 404)
                self.assertEqual(resp.data["code"], "not_found")
                self.assertTrue(resp.data["detail"])
                self.assertNotIn("error", resp.data)

    def test_run_endpoints_are_401_when_anonymous(self):
        """401 exactly, not 403 -- MUS-38's route guard redirects on 401 and does
        nothing on 403. Worth its own test here in particular: the composer is the
        one surface where an unauthenticated POST can start spending money.
        """
        from project.app.models import PlannerRun

        run = PlannerRun.objects.create(scope={}, created_by="someone@example.com")
        self.client.logout()

        for path in (f"/api/runs/{run.pk}/", "/api/runs/active/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
        for path in (
            "/api/runs/",
            f"/api/runs/{run.pk}/classify/",
            f"/api/runs/{run.pk}/close/",
            f"/api/runs/{run.pk}/discard/",
        ):
            with self.subTest(path=path):
                self.assertEqual(self._post(path).status_code, 401)
