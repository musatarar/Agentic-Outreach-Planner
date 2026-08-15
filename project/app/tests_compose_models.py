"""Component artifact: models (MUS-47).

Pins the Run Composer schema (migration ``0008_run_composer``): the
``PlannerRun`` status machine, the single-active-run slot the database itself
enforces, the check constraint that keeps ``active_sentinel`` honest about
``status``, the per-run ``RunLead`` row with its rules/effective split and its
selection-order index, and ``SavedScope``.

"One active run" is a *fact about the database*, not a convention in
``services/compose/runs.py``: ``create_run`` inserts and lets the index decide,
so two concurrent POSTs cannot both pass a read-then-write check. That only
works if the partial unique index and its companion check constraint behave
exactly as described here, so both are pinned at the DB.

Planted red by the skeleton PR: every test is ``@unittest.expectedFailure`` and
imports ``PlannerRun`` / ``RunLead`` / ``SavedScope`` inside its own body, so a
missing symbol is an absorbed ``ImportError`` rather than a collection error
taking every sibling artifact's count down with it.
"""

import datetime
import unittest

from django.db import IntegrityError, connection, transaction
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, TestCase

from project.app.models import Lead
from project.app.services import actions, dedupe
from project.app.services.outreach import TRACE_SCHEMA_VERSION, explain

# The contract's ALLOWED_TRANSITIONS table, spelled out in literals so this file
# is a specification rather than a mirror of whatever models.py happens to say.
_TRANSITIONS = {
    "draft": ("classified", "discarded"),
    "classified": ("classified", "read", "generated", "discarded"),
    "read": ("read", "generated", "discarded"),
    "generated": ("generated", "completed", "discarded"),
    "completed": (),
    "discarded": (),
}
_ALL_STATUSES = tuple(_TRANSITIONS)

# The working stages, in the order a run walks them. Terminal statuses are
# deliberately absent: they are not positions on this path.
_WORKING_ORDER = ("draft", "classified", "read", "generated")

# Fixed so `explain()` envelopes are reproducible against `_lead()`'s dates.
_TODAY = datetime.date(2026, 3, 1)


def _legal_by_product_rules(source: str, target: str) -> bool:
    """A second, *independent* derivation of the transition rules, written from
    the product statements rather than read off ``_TRANSITIONS`` -- a table
    checked only against itself proves nothing but its own reflexivity.

    In precedence order: a terminal run accepts nothing, its own status
    included (a reopened run would need the active slot back and nothing hands
    it back); discard is available from every working stage; only a
    ``generated`` run can complete; ``draft`` is entered once, at creation, and
    never re-entered; classify cannot be skipped, since nothing downstream has
    ``RunLead`` rows until it has run; otherwise a working run may re-enter the
    stage it is in (re-classify after a scope edit, re-read after a provider
    change, re-generate after selecting more leads) or move forward, never back.
    """
    if source not in _WORKING_ORDER:
        return False
    if target == "discarded":
        return True
    if target == "completed":
        return source == "generated"
    if target == "draft":
        return False
    if source == "draft":
        return target == "classified"
    return _WORKING_ORDER.index(target) >= _WORKING_ORDER.index(source)


def _lead(lead_id="lead_cm1"):
    """A lead complete enough for ``outreach.explain()`` to score. The dates are
    populated rather than left NULL because ``RunLead.rule_trace`` holds the
    real envelope, and a lead with no history produces a degenerate one."""
    return Lead.objects.create(
        id=lead_id,
        agency_name=f"Agency {lead_id}",
        contact_name="Dana Reyes",
        contact_email=f"{lead_id}@example.com",
        contact_phone="555-0100",
        state="CA",
        num_producers=3,
        years_in_business=5,
        estimated_book_size_usd=1_000_000,
        stage="active_trial",
        signed_up_date=datetime.date(2026, 1, 10),
        last_login_date=datetime.date(2026, 2, 1),
        last_contacted_date=datetime.date(2026, 1, 20),
        quotes_created=4,
        quotes_submitted=1,
        deals_closed=0,
    )


def _terminal_run(status):
    """A finished run: terminal status AND a NULL sentinel, together. The two
    writes are one atomic fact -- ``close_run`` sets both or neither -- which is
    what ``pr_sentinel_matches_status`` exists to enforce. Deferred import:
    ``PlannerRun`` does not exist until the models component lands."""
    from project.app.models import PlannerRun

    return PlannerRun.objects.create(status=status, active_sentinel=None)


def _run_lead(run, lead, **overrides):
    """A classified row: rules columns written once, effective columns seeded
    from them, every non-defaulted column supplied so a dropped default
    surfaces here rather than downstream. ``rule_trace`` is a real
    ``outreach.explain()`` envelope -- the schema-v1 mapping
    ``OutreachAction.rule_trace`` holds -- not an invented stand-in; pass
    ``rule_trace=None`` to omit the column and let the field default answer."""
    from project.app.models import RunLead

    fields = {
        "rules_priority": 2,
        "rules_action": actions.NUDGE_USAGE,
        "rules_reason": "Active but underusing the portal.",
        "rule_trace": explain(lead, today=_TODAY),
        "dedupe_key": dedupe.dedupe_key(lead.id, actions.NUDGE_USAGE),
        "effective_priority": 2,
        "effective_action": actions.NUDGE_USAGE,
        "effective_reason": "Active but underusing the portal.",
    }
    fields.update(overrides)
    if fields["rule_trace"] is None:
        del fields["rule_trace"]
    return RunLead.objects.create(run=run, lead=lead, **fields)


class PlannerRunStatusTests(SimpleTestCase):
    """The status machine, exercised entirely in memory -- no run is saved, so
    a transition rule can never be confused with a constraint."""

    @unittest.expectedFailure
    def test_status_constants_and_the_active_terminal_partition(self):
        """The six strings live in a CharField *and* are inlined a second time
        as literals inside ``Meta`` (which cannot see the enclosing class
        namespace -- the same reason ``ReviewDecision``'s constraints spell
        their kinds out). Two copies of a string is the shape that drifts."""
        from project.app.models import PlannerRun

        self.assertEqual(
            (
                PlannerRun.STATUS_DRAFT,
                PlannerRun.STATUS_CLASSIFIED,
                PlannerRun.STATUS_READ,
                PlannerRun.STATUS_GENERATED,
                PlannerRun.STATUS_COMPLETED,
                PlannerRun.STATUS_DISCARDED,
            ),
            ("draft", "classified", "read", "generated", "completed", "discarded"),
        )
        self.assertEqual(PlannerRun.ACTIVE_STATUSES, ("draft", "classified", "read", "generated"))
        self.assertEqual(PlannerRun.TERMINAL_STATUSES, ("completed", "discarded"))
        # Active and terminal must partition the space: a status in neither
        # would be a run the active-slot index cannot reason about at all.
        active, terminal = set(PlannerRun.ACTIVE_STATUSES), set(PlannerRun.TERMINAL_STATUSES)
        self.assertEqual(active & terminal, set())
        self.assertEqual(active | terminal, set(_ALL_STATUSES))

    @unittest.expectedFailure
    def test_allowed_transitions_is_exactly_the_contract_table(self):
        """Pinned whole. Adding an edge is a product decision (it changes which
        stages an operator can re-enter and what a paid stage can be repeated
        from), so it should cost a deliberate test edit."""
        from project.app.models import PlannerRun

        self.assertEqual(PlannerRun.ALLOWED_TRANSITIONS, _TRANSITIONS)

    @unittest.expectedFailure
    def test_can_transition_to_agrees_with_the_table_on_every_ordered_pair(self):
        """All 36 ordered pairs, three ways: the model, the contract's literal
        table, and ``_legal_by_product_rules`` must all agree.

        The independent derivation is the point. Checked against ``_TRANSITIONS``
        alone, this proves only that the model can read a table back; the rules
        were written apart from it, so a disagreement between the two is real
        information. Subsumes terminal-accepts-nothing and
        stages-are-re-enterable -- both are subsets of these pairs.
        """
        from project.app.models import PlannerRun

        run = PlannerRun()
        for source in _ALL_STATUSES:
            for target in _ALL_STATUSES:
                with self.subTest(source=source, target=target):
                    expected = _legal_by_product_rules(source, target)
                    self.assertEqual(target in _TRANSITIONS[source], expected)
                    run.status = source
                    self.assertEqual(run.can_transition_to(target), expected)

    @unittest.expectedFailure
    def test_an_unknown_status_has_no_legal_transitions(self):
        """Same guarantee ``OutreachAction`` gives (``tests_queue.py``): a value
        that is not in the table yields ``()``, not a ``KeyError``. A row can
        carry an unknown status after a downgrade, and the failure mode has to
        be "nothing is legal", not a 500."""
        from project.app.models import PlannerRun

        run = PlannerRun(status="bogus")
        for target in _ALL_STATUSES:
            with self.subTest(target=target):
                self.assertFalse(run.can_transition_to(target))


class ActiveRunSlotTests(TestCase):
    """``pr_one_active_run`` and ``pr_sentinel_matches_status`` -- the pair that
    makes "one active run" true rather than intended."""

    @unittest.expectedFailure
    def test_a_new_run_takes_the_active_slot_by_default(self):
        """The defaults *are* the mechanism. ``create_run`` never sets
        ``active_sentinel``; it relies on ``default=True`` to make the insert
        contend for the slot. A ``default=None`` would silently switch the
        exclusion off and every test below would still pass."""
        from project.app.models import PlannerRun

        run = PlannerRun.objects.create(scope={"stage": "active_trial"})
        run.refresh_from_db()
        self.assertEqual(run.status, PlannerRun.STATUS_DRAFT)
        self.assertIs(run.active_sentinel, True)  # True, not merely truthy
        self.assertEqual(run.scope, {"stage": "active_trial"})
        self.assertIsNone(run.finished_at)
        self.assertEqual(run.finished_by, "")
        self.assertEqual(run.created_by, "")
        # Nothing minted and nothing spent until the stages that do those run.
        self.assertEqual(run.trace_run_id, "")
        self.assertIsNone(run.classify_ms)
        self.assertEqual(run.discarded_suggestions, 0)
        for column in (
            "read_cost_estimate_usd",
            "read_cost_actual_usd",
            "generate_cost_estimate_usd",
            "generate_cost_actual_usd",
        ):
            with self.subTest(column=column):
                # NULL, not Decimal("0") -- "not priced yet" and "priced at zero"
                # are different answers and the estimate view renders them
                # differently.
                self.assertIsNone(getattr(run, column))

    @unittest.expectedFailure
    def test_a_second_active_run_is_rejected_by_the_partial_unique_index(self):
        """THE constraint. Two active runs would mean two competing selections
        writing ``OutreachAction`` rows against one queue, so the second insert
        must fail at the database -- which is what lets ``create_run`` be a bare
        insert whose ``IntegrityError`` becomes ``RunConflict``. Tried at two
        active statuses: the index keys on the sentinel, not the status, so a
        classified run is no more entitled to the slot than a fresh draft."""
        from project.app.models import PlannerRun

        first = PlannerRun.objects.create()
        for status in (PlannerRun.STATUS_DRAFT, PlannerRun.STATUS_CLASSIFIED):
            with self.subTest(status=status):
                # Savepoint: without it the failed INSERT poisons the surrounding
                # TestCase transaction and every later assertion errors instead.
                with self.assertRaises(IntegrityError), transaction.atomic():
                    PlannerRun.objects.create(status=status)
        self.assertEqual(PlannerRun.objects.count(), 1)
        self.assertEqual(PlannerRun.objects.get().pk, first.pk)

    @unittest.expectedFailure
    def test_the_slot_is_released_when_a_run_goes_terminal_and_cycles(self):
        """The other direction, and the one no unit test of ``create_run``
        would catch: if closing a run left the sentinel set, the product would
        be permanently wedged with no UI path out. Completed and discarded both
        release the slot, repeatedly."""
        from project.app.models import PlannerRun

        first = PlannerRun.objects.create()
        PlannerRun.objects.filter(pk=first.pk).update(
            status=PlannerRun.STATUS_COMPLETED, active_sentinel=None
        )
        second = PlannerRun.objects.create()  # slot freed by completion

        PlannerRun.objects.filter(pk=second.pk).update(
            status=PlannerRun.STATUS_DISCARDED, active_sentinel=None
        )
        third = PlannerRun.objects.create()  # and freed again by discard

        self.assertEqual(PlannerRun.objects.count(), 3)
        held = PlannerRun.objects.filter(active_sentinel=True)
        self.assertEqual([row.pk for row in held], [third.pk])

    @unittest.expectedFailure
    def test_many_terminal_runs_coexist_because_nulls_are_distinct(self):
        """Why the column is nullable rather than a plain ``BooleanField``: run
        history is unbounded, and both backends treat NULLs as distinct in a
        unique index, so every finished run carries the same "not active"
        marker without colliding. ``False`` would make the second one
        un-storable."""
        from project.app.models import PlannerRun

        for i in range(6):
            status = PlannerRun.STATUS_COMPLETED if i % 2 == 0 else PlannerRun.STATUS_DISCARDED
            _terminal_run(status)
        current = PlannerRun.objects.create()

        self.assertEqual(PlannerRun.objects.count(), 7)
        self.assertEqual(PlannerRun.objects.filter(active_sentinel__isnull=True).count(), 6)
        self.assertEqual(
            [row.pk for row in PlannerRun.objects.filter(active_sentinel=True)], [current.pk]
        )

    @unittest.expectedFailure
    def test_the_sentinel_must_agree_with_the_status(self):
        """``pr_sentinel_matches_status`` rules out the half-written close.

        Two rows are illegal in opposite ways. ``(True, "completed")`` is a
        finished run still occupying the slot -- the wedge above. ``(None,
        "draft")`` is its mirror: a run that reports itself active to every
        serializer and stage guard while holding no slot, so a *second* run can
        be created alongside it and both accept classify calls.

        The mirror case needs care. Rendered naively the constraint reads
        ``(sentinel AND status IN active) OR (sentinel IS NULL AND status IN
        terminal)``, which for a NULL sentinel is ``NULL OR FALSE`` -> NULL,
        and SQL rejects a row only when a CHECK is FALSE, never when it is
        unknown. It must be written NULL-safely (an explicit ``IS NOT NULL`` in
        the first disjunct) or this half does not exist. Verified against both
        renderings on SQLite before this test was written.

        ``False`` is illegal at every status: the marker is two-valued
        True-or-NULL, and a ``False`` row sits outside the partial index --
        invisible to the exclusion it is supposed to obey."""
        from project.app.models import PlannerRun

        illegal = [
            (True, PlannerRun.STATUS_COMPLETED),
            (True, PlannerRun.STATUS_DISCARDED),
            (None, PlannerRun.STATUS_DRAFT),
            (None, PlannerRun.STATUS_GENERATED),
            (False, PlannerRun.STATUS_DRAFT),
            (False, PlannerRun.STATUS_COMPLETED),
        ]
        for sentinel, status in illegal:
            with self.subTest(active_sentinel=sentinel, status=status):
                with self.assertRaises(IntegrityError) as caught, transaction.atomic():
                    PlannerRun.objects.create(status=status, active_sentinel=sentinel)
                # Name the constraint that fired: both backends embed it in the
                # message, and without this the unique index could be answering
                # instead and the check could be missing entirely.
                self.assertIn("pr_sentinel_matches_status", str(caught.exception))
        self.assertEqual(PlannerRun.objects.count(), 0)

        # And the legal shapes: any number of NULL/terminal rows, plus one
        # True/active row.
        _terminal_run(PlannerRun.STATUS_COMPLETED)
        _terminal_run(PlannerRun.STATUS_DISCARDED)
        PlannerRun.objects.create(status=PlannerRun.STATUS_CLASSIFIED, active_sentinel=True)
        self.assertEqual(PlannerRun.objects.count(), 3)


class RunLeadFieldTests(TestCase):
    """``RunLead``'s columns: the defaults the read stage reads, and the
    rules/effective split that organizing principle #1 rests on."""

    @unittest.expectedFailure
    def test_rule_trace_defaults_to_a_mapping_and_holds_the_v1_envelope(self):
        """``default=dict``, not ``default=list``. MUS-42 already ships the
        structured trace as ``outreach.explain()``'s schema-v1 *envelope* -- a
        mapping -- and ``OutreachAction.rule_trace`` stores exactly that. Same
        shape in both places is what lets MUS-40's ``RuleTrace`` component
        render a composer row unchanged; a list would need a second renderer.

        The envelope here is a real ``explain()`` call, not a hand-written
        stand-in, so a key that moves fails here rather than in the FE that
        renders it."""
        from project.app.models import PlannerRun

        lead = _lead()
        run = PlannerRun.objects.create()
        # rule_trace omitted from the INSERT: the field default answers, and a
        # `default=list` (or a nullable column) is caught right here.
        blank = _run_lead(run, lead, rule_trace=None)
        blank.refresh_from_db()
        self.assertEqual(blank.rule_trace, {})
        self.assertIsInstance(blank.rule_trace, dict)

        envelope = explain(lead, today=_TODAY)
        blank.rule_trace = envelope
        blank.save(update_fields=["rule_trace"])
        blank.refresh_from_db()
        stored = blank.rule_trace
        self.assertIsInstance(stored, dict)
        self.assertEqual(stored["version"], TRACE_SCHEMA_VERSION)
        # The v1 envelope's real keys, round-tripped through JSONField intact.
        self.assertEqual(set(stored), {"version", "today", "generated_at", "priority", "action"})
        self.assertEqual(stored["today"], "2026-03-01")
        self.assertEqual(stored, envelope)

    @unittest.expectedFailure
    def test_a_fresh_row_carries_no_suggestion(self):
        """``suggestion_state`` defaults to the string ``"none"`` -- a state,
        not Python ``None``. A lead the read never reached, one whose provider
        call failed, and one whose suggestion ``validate_suggestion`` discarded
        all land in exactly this value, which is what lets a single lead's
        failure leave the run intact. A nullable column would invite three-way
        branching on a two-way fact."""
        from project.app.models import PlannerRun, RunLead

        row = _run_lead(PlannerRun.objects.create(), _lead())
        row.refresh_from_db()
        self.assertEqual(
            (
                RunLead.SUGGESTION_NONE,
                RunLead.SUGGESTION_PROPOSED,
                RunLead.SUGGESTION_ACCEPTED,
                RunLead.SUGGESTION_REJECTED,
            ),
            ("none", "proposed", "accepted", "rejected"),
        )
        self.assertEqual(row.suggestion_state, RunLead.SUGGESTION_NONE)
        self.assertEqual(row.suggestion, {})
        self.assertIsNone(row.suggestion_decided_at)
        self.assertEqual(row.suggestion_decided_by, "")
        # Selection and generation state, likewise unset until their stages run.
        self.assertFalse(row.already_queued)
        self.assertFalse(row.selected)
        self.assertIsNone(row.generated_action)
        self.assertEqual(row.generation_error, "")

    @unittest.expectedFailure
    def test_rules_and_effective_are_independent_columns(self):
        """Six columns, not three plus a property. Deriving
        ``effective_priority`` from ``rules_priority`` is the obvious
        simplification and it destroys the feature: an accepted suggestion
        becomes unrepresentable and the audit answer this product owes -- what
        the rules said, beside what the human approved -- is gone.

        This does not claim the rules columns are immutable (component 8 pins
        that); it claims they are separately *storable*, the precondition for
        immutability to mean anything."""
        from project.app.models import PlannerRun, RunLead

        row = _run_lead(PlannerRun.objects.create(), _lead())

        # Accept-shaped write: only the effective columns move.
        RunLead.objects.filter(pk=row.pk).update(
            effective_priority=1,
            effective_action=actions.REENGAGE_DORMANT,
            effective_reason="Reviewer accepted an agent suggestion.",
        )
        # .values() reads concrete columns only -- a property would raise here.
        self.assertEqual(
            RunLead.objects.filter(pk=row.pk)
            .values("rules_priority", "rules_action", "effective_priority", "effective_action")
            .get(),
            {
                "rules_priority": 2,
                "rules_action": actions.NUDGE_USAGE,
                "effective_priority": 1,
                "effective_action": actions.REENGAGE_DORMANT,
            },
        )

        # And the reverse: touching the rules columns does not drag the
        # effective ones along.
        RunLead.objects.filter(pk=row.pk).update(rules_priority=3)
        row.refresh_from_db()
        self.assertEqual(row.rules_priority, 3)
        self.assertEqual(row.effective_priority, 1)
        self.assertEqual(row.effective_reason, "Reviewer accepted an agent suggestion.")


class SavedScopeTests(TestCase):
    @unittest.expectedFailure
    def test_scope_names_are_unique_and_ordered(self):
        """``name`` is the handle the FE saves and re-selects by, so two rows
        called "Dormant CA" would make "load my saved scope" ambiguous with no
        tiebreak the user can see. ``Meta.ordering = ["name"]`` is what keeps
        the picker stable without every view remembering to sort."""
        from project.app.models import SavedScope

        first = SavedScope.objects.create(name="Dormant CA", filters={"state": "CA"})
        self.assertEqual(first.created_by, "")
        with self.assertRaises(IntegrityError), transaction.atomic():
            SavedScope.objects.create(name="Dormant CA", filters={"state": "TX"})

        blank = SavedScope.objects.create(name="All leads")
        self.assertEqual(blank.filters, {})  # default=dict, never NULL
        SavedScope.objects.create(name="Mid-book", filters={"book_min": 500000})
        self.assertEqual(
            [row.name for row in SavedScope.objects.all()],
            ["All leads", "Dormant CA", "Mid-book"],
        )


class RunComposerMigrationTests(TestCase):
    """That ``0008_run_composer`` exists under that name, sits where the file
    map says, and actually carries ``RunLead``'s uniqueness constraint and its
    selection-order index into the database."""

    @unittest.expectedFailure
    def test_migration_0008_run_composer_is_the_named_node_that_creates_the_tables(self):
        """CI's ``makemigrations --check --dry-run`` proves models and
        migrations agree; it does not care what the migration is *called* or
        where it hangs. Both matter: the file map assigns the name, and a node
        forked off something other than ``0007_agent_loop`` would give this
        branch a migration graph that cannot merge back."""
        from project.app.models import PlannerRun, RunLead, SavedScope

        node = ("app", "0008_run_composer")
        loader = MigrationLoader(connection)
        self.assertIn(node, loader.graph.nodes)
        parents = {parent.key for parent in loader.graph.node_map[node].parents}
        self.assertIn(("app", "0007_agent_loop"), parents)

        tables = connection.introspection.table_names()
        for model in (PlannerRun, RunLead, SavedScope):
            with self.subTest(model=model.__name__):
                self.assertIn(model._meta.db_table, tables)

    @unittest.expectedFailure
    def test_a_lead_may_appear_once_in_each_of_two_different_runs(self):
        """Uniqueness is per ``(run, lead)``, not per lead. A lead last week's
        run looked at showing up again in today's is the normal case; a
        per-lead constraint would make the second run of the week impossible,
        and only in production."""
        from project.app.models import PlannerRun, RunLead

        lead = _lead()
        first = PlannerRun.objects.create()
        _run_lead(first, lead)
        PlannerRun.objects.filter(pk=first.pk).update(
            status=PlannerRun.STATUS_COMPLETED, active_sentinel=None
        )
        second = PlannerRun.objects.create()
        _run_lead(second, lead, rules_priority=1, effective_priority=1)

        self.assertEqual(RunLead.objects.filter(lead=lead).count(), 2)
        self.assertEqual(first.run_leads.count(), 1)
        self.assertEqual(second.run_leads.count(), 1)
        # The two rows classified the same lead differently, and both survive.
        self.assertEqual(
            sorted(RunLead.objects.filter(lead=lead).values_list("rules_priority", flat=True)),
            [1, 2],
        )

    @unittest.expectedFailure
    def test_a_lead_cannot_appear_twice_in_one_run(self):
        """``rl_one_row_per_lead_per_run``. Re-classify is specified to *replace*
        a run's RunLead rows rather than add to them, and selection counts,
        estimates and the generate loop all assume one row per lead. A duplicate
        would double-charge the operator for a single lead."""
        from project.app.models import PlannerRun, RunLead

        lead = _lead()
        run = PlannerRun.objects.create()
        _run_lead(run, lead)
        with self.assertRaises(IntegrityError), transaction.atomic():
            # Different rules columns, same (run, lead) -- the constraint is on
            # identity, not on content.
            _run_lead(run, lead, rules_priority=1, rules_action=actions.REENGAGE_DORMANT)
        self.assertEqual(RunLead.objects.filter(run=run).count(), 1)

    @unittest.expectedFailure
    def test_rl_selection_order_index_covers_the_one_query_every_stage_runs(self):
        """``Index(fields=["run", "effective_priority", "lead"],
        name="rl_selection_order")``, pinned by name and by field list.

        Every stage after classify reads the same thing -- this run's rows,
        priority 1 first, tie-broken by lead so the selection table is stable
        between polls -- which is this index's column order, the shape
        ``oa_queue_order`` already gives the queue. The order is load-bearing:
        ``run`` must lead or the index is no use as a prefix for the per-run
        filter, and it is ``effective_priority``, not ``rules_priority``,
        because an accepted suggestion moves a lead and the sort follows it.

        Asserted twice -- declared in ``Meta``, present in the database -- so a
        migration that quietly dropped it cannot pass."""
        from project.app.models import RunLead

        declared = {index.name: index.fields for index in RunLead._meta.indexes}
        self.assertIn("rl_selection_order", declared)
        self.assertEqual(declared["rl_selection_order"], ["run", "effective_priority", "lead"])

        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, RunLead._meta.db_table)
        self.assertIn("rl_selection_order", constraints)
        self.assertTrue(constraints["rl_selection_order"]["index"])
        self.assertEqual(
            constraints["rl_selection_order"]["columns"],
            # FK fields land as `<name>_id` columns; resolve rather than guess.
            [RunLead._meta.get_field(f).column for f in ("run", "effective_priority", "lead")],
        )
