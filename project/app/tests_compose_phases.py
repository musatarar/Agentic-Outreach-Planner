"""Component artifact: phases (MUS-47).

The composer needs the planner's three per-lead steps as callable functions: ``classify_lead``
(today's ``_build_work_item``), ``review_outcome`` (today's ``_review``) and ``snapshot_for``
(today's phase-5 snapshot comprehension). It is a **pure extraction**, so the acceptance criterion
is invisibility: the first three cases pin each function against the primitives it is built from
(``determine_priority``, ``determine_action``, ``dedupe.dedupe_key``, ``validate_copy``,
``verify.verify_copy``, ``explain``, ``queue_copy.build_verification``) computed independently for
the same lead, and :class:`PlannerInvarianceTests` then pins ``plan_outreach()``'s own rows against
all three composed by hand, field by field.

That last case is the one that matters: every other test here would pass against an implementation
that agrees with the rules while ``plan_outreach()`` quietly keeps a private copy of the logic, and
only a row-for-row comparison proves the two run the *same* classification. The contract's first
organizing principle — "``plan_outreach()`` does not change behavior" — has no other mechanical
expression.

The three pure cases are ``django.test.SimpleTestCase``, which actually **blocks** database access
rather than merely declining to use it: the extracted functions inherit the rules' Django-free,
duck-typed contract (``evals/run_rules_eval.py`` feeds them ``SimpleNamespace`` leads whose events
are a plain list), so an ORM read smuggled into a phase whose query count ``tests_planner_perf``
pins fails here first. Bare ``unittest.TestCase`` would block nothing and prove nothing.

Planted red by the skeleton PR: every test is ``@unittest.expectedFailure``, and the three symbols
are imported *inside* each method because a module-level import of a name that does not exist yet
would kill collection for the whole file rather than fail one test.
"""

import datetime
import unittest
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from project.app.models import Lead, OutreachAction
from project.app.services import actions, dedupe, outreach, queue_copy, verify
from project.app.services.llm import LLMRateLimitError

# Frozen for the pure cases. Every fixture dates itself *relative* to the run's `today`, so the same
# builder produces the same five classifications whether handed this constant or the wall clock —
# which is what lets the planner-invariance case reuse it against `date.today()`.
TODAY = datetime.date(2026, 6, 12)


class _EventSet:
    """Duck-types a Django related manager (``lead.events.all()``)."""

    def __init__(self, events):
        self._events = list(events)

    def all(self):
        return list(self._events)


# Every column ``Lead`` declares, so a fixture dict goes straight into ``Lead.objects.create()``.
_LEAD_DEFAULTS = dict(
    id="lead_x",
    agency_name="Summit Risk Advisors",
    contact_name="Priya Nair",
    contact_email="priya.nair@summitrisk.test",
    contact_phone="555-0000",
    state="CO",
    num_producers=4,
    years_in_business=12,
    estimated_book_size_usd=5_000_000,
    stage="active_trial",
    signed_up_date=None,
    last_login_date=None,
    last_contacted_date=None,
    quotes_created=8,
    quotes_submitted=3,
    deals_closed=4,
    hubspot_notes="",
)


def _lead(**fields):
    """A duck-typed lead: the shape ``evals/run_rules_eval.py`` feeds the rules."""
    return SimpleNamespace(**{**_LEAD_DEFAULTS, **fields}, events=_EventSet([]))


def _fixture_fields(today):
    """Five leads that land on five *different* rules, spanning priority 1..3.

    Complete field dicts, so the pure cases can wrap them in ``SimpleNamespace`` while the
    invariance case hands the identical values to ``Lead.objects.create()`` — one fixture, two
    shapes. Dates are offsets from ``today``: absolute ones would re-band these leads as the wall
    clock moved past them, and a fixture that changes meaning over time reports its own drift as a
    regression.
    """

    def ago(days):
        return today - datetime.timedelta(days=days)

    specific = [
        # R1: demo completed, never signed up. The one date-independent rule.
        dict(
            id="lead_onboarding",
            agency_name="Cascade Insurance Group",
            contact_name="Dana Whitfield",
            contact_email="dana@cascade.test",
            stage="demo_completed",
            signed_up_date=None,
            estimated_book_size_usd=6_000_000,
            quotes_created=0,
            quotes_submitted=0,
            deals_closed=0,
        ),
        # R2: power user with a milestone in the notes.
        dict(
            id="lead_power",
            agency_name="Summit Risk Advisors",
            contact_name="Priya Nair",
            contact_email="priya@summitrisk.test",
            stage="active_trial",
            signed_up_date=ago(220),
            last_login_date=ago(2),
            last_contacted_date=ago(11),
            quotes_created=19,
            quotes_submitted=14,
            deals_closed=6,
            hubspot_notes="Asked about volume pricing at 20 closed deals.",
        ),
        # R4: signed up, then stopped logging in.
        dict(
            id="lead_dormant",
            agency_name="Harbor Point Agency",
            contact_name="Miguel Ortega",
            contact_email="miguel@harborpoint.test",
            stage="active_trial",
            signed_up_date=ago(195),
            last_login_date=ago(72),
            last_contacted_date=ago(42),
            quotes_created=2,
            quotes_submitted=0,
            deals_closed=0,
            estimated_book_size_usd=1_000_000,
        ),
        # R5: active but never submits a quote.
        dict(
            id="lead_nudge",
            agency_name="Beacon Risk Partners",
            contact_name="Sofia Lang",
            contact_email="sofia@beaconrisk.test",
            stage="active_trial",
            signed_up_date=ago(45),
            last_login_date=ago(1),
            last_contacted_date=ago(7),
            quotes_created=6,
            quotes_submitted=0,
            deals_closed=0,
            estimated_book_size_usd=900_000,
        ),
        # R6: nothing matches — straight to a human, and no prompt at all.
        dict(
            id="lead_unmatched",
            agency_name="Nowhere Agency",
            contact_name="Pat Quinn",
            contact_email="pat@nowhere.test",
            stage="",
            signed_up_date=None,
            last_login_date=None,
            last_contacted_date=None,
            quotes_created=0,
            quotes_submitted=0,
            deals_closed=0,
            estimated_book_size_usd=0,
        ),
    ]
    return [{**_LEAD_DEFAULTS, **fields} for fields in specific]


def _fixture_leads(today=TODAY):
    return [_lead(**fields) for fields in _fixture_fields(today)]


def _lead_by_id(lead_id, today=TODAY):
    return next(lead for lead in _fixture_leads(today) if lead.id == lead_id)


def _draft_for(lead):
    """A draft that passes **both** output gates for any fixture lead: well-shaped (subject line,
    no preamble, one CTA, inside the 60..200-word band) and grounded — it names only the contact
    and the agency and spells its one quantity in words, so ``verify`` finds no number to
    contradict. A draft tripping either gate by accident would make the clean-success case assert
    the failure path."""
    first = lead.contact_name.split()[0]
    return (
        f"Subject: A quick idea for {lead.agency_name}\n\n"
        f"Hi {first},\n\n"
        f"I have been looking at how {lead.agency_name} is getting on with the portal, and there "
        "is one small change that usually helps agencies of your size get more quotes over the "
        "line. It takes about fifteen minutes to walk through, and your producers can start using "
        "it the same day. I would rather show you against your own book of business than write it "
        "all out here, because the useful part is seeing where things stall for you specifically."
        "\n\n"
        "Would you have time for a short call this week?\n\n"
        "Best,\nDana"
    )


def _shape_broken(draft):
    """The same draft with its ``Subject:`` line removed — shape gate only, exactly one problem."""
    return draft.split("\n", 1)[1].lstrip("\n")


def _ungrounded(draft):
    """The same draft claiming 47 closed deals — grounding gate only. Every fixture lead has fewer,
    so ``verify`` raises one ``wrong_count`` violation while the shape gate stays happy."""
    return draft.replace(
        "I have been looking at how",
        "Congratulations on your 47 closed deals this quarter at",
    )


class ClassifyLeadTests(SimpleTestCase):
    """``classify_lead`` is ``_build_work_item`` with keyword-only extras.

    ``SimpleTestCase``, so a query raises ``DatabaseOperationForbidden`` rather than passing
    unnoticed: this is the phase whose query count ``tests_planner_perf`` pins, and a case that
    cannot reach a database is the cheapest proof the extraction smuggled in no ORM read.
    """

    @unittest.expectedFailure
    def test_classification_matches_the_rule_functions_computed_independently(self):
        """The rules keep sole authority: every ``WorkItem`` field is the primitive's own answer."""
        from project.app.services.outreach import classify_lead

        seen_actions = set()
        for lead in _fixture_leads():
            with self.subTest(lead=lead.id):
                item = classify_lead(lead, today=TODAY)
                self.assertIsInstance(item, outreach.WorkItem)
                self.assertIs(item.lead, lead)  # carried, never copied
                # Computed independently, from the same lead and the same date.
                self.assertEqual(item.priority, outreach.determine_priority(lead, TODAY))
                action_type, reason = outreach.determine_action(lead, TODAY)
                self.assertEqual(item.action_type, action_type)
                self.assertEqual(item.reason, reason)
                # The key is the identity of the recommendation (MUS-39), computed in the same
                # phase that decided `action_type` — so it is keyed on the action actually chosen.
                self.assertEqual(item.dedupe_key, dedupe.dedupe_key(lead.id, action_type))
                seen_actions.add(item.action_type)

        # Five leads that all classified alike would satisfy every assertion above on one rule.
        self.assertEqual(len(seen_actions), 5)

    @unittest.expectedFailure
    def test_the_two_skip_rules_return_none_before_any_prompt_is_built(self):
        """A dismissed or already-open recommendation must cost neither a prompt nor a provider
        call, so the check sits between the classification and the prompt — not after it."""
        from project.app.services.outreach import classify_lead

        lead = _lead_by_id("lead_nudge")
        key = dedupe.dedupe_key(lead.id, actions.NUDGE_USAGE)

        with mock.patch.object(outreach, "_build_copy_prompt") as build_prompt:
            self.assertIsNone(classify_lead(lead, today=TODAY, suppressed=frozenset({key})))
            self.assertIsNone(classify_lead(lead, today=TODAY, open_keys=frozenset({key})))
            build_prompt.assert_not_called()

            # Positive control. Without this, `assert_not_called` is satisfied by a patch aimed at
            # a function `classify_lead` never calls — the skip rules would be untested and the
            # test would still be green.
            item = classify_lead(lead, today=TODAY)
            self.assertIsNotNone(item)
            self.assertEqual(build_prompt.call_count, 1)

    @unittest.expectedFailure
    def test_an_unknown_action_leaves_both_prompt_fields_none(self):
        """No pattern matched means no copy to write, and that is BD work rather than a bug — so
        ``prompt_error`` stays ``None`` and phase 4 can tell the two apart from the item alone."""
        from project.app.services.outreach import classify_lead

        item = classify_lead(_lead_by_id("lead_unmatched"), today=TODAY)

        self.assertEqual(item.action_type, actions.UNKNOWN)
        self.assertIsNone(item.prompt)
        self.assertIsNone(item.prompt_error)
        # Still classified and still carrying its key: the row it produces has to suppress a
        # re-plan like any other.
        self.assertEqual(item.dedupe_key, dedupe.dedupe_key(item.lead.id, actions.UNKNOWN))

    @unittest.expectedFailure
    def test_a_prompt_build_failure_is_carried_on_the_item_not_raised(self):
        """Prompt building used to sit inside ``generate_copy``'s ``try``, so a malformed lead cost
        one row and the run continued; hoisting it into classification keeps that blast radius."""
        from project.app.services.outreach import classify_lead

        lead = _lead_by_id("lead_nudge")
        boom = ValueError("events blew up")

        with mock.patch.object(outreach, "_build_copy_prompt", side_effect=boom):
            item = classify_lead(lead, today=TODAY)

        self.assertIsNotNone(item)
        self.assertIsNone(item.prompt)
        self.assertIs(item.prompt_error, boom)  # the exception itself, not a message
        # The classification survives the prompt failure — a reviewer still gets the action and the
        # reason, which is the whole point of not raising.
        self.assertEqual(item.action_type, actions.NUDGE_USAGE)
        self.assertEqual(item.priority, outreach.determine_priority(lead, TODAY))
        self.assertEqual(item.dedupe_key, dedupe.dedupe_key(lead.id, actions.NUDGE_USAGE))


class ReviewOutcomeTests(SimpleTestCase):
    """``review_outcome`` is ``_review``: the two output gates, both fail-closed.

    The composer reuses it verbatim so a composer draft passes exactly the gates a planner draft
    does (contract, "generate"); these pin all five verdicts. ``SimpleTestCase`` for the reason
    above — ``validate_copy`` and ``verify.verify_copy`` are pure, and this keeps them that way.
    """

    def _item(self, lead_id="lead_nudge"):
        from project.app.services.outreach import classify_lead

        return classify_lead(_lead_by_id(lead_id), today=TODAY)

    @unittest.expectedFailure
    def test_a_clean_draft_passes_both_gates_and_needs_no_human(self):
        from project.app.services.outreach import review_outcome

        item = self._item()
        draft = _draft_for(item.lead)
        # The premise, asserted rather than assumed: a future tightening of either gate fails here
        # as the fixture drift it is, instead of turning the success case into an unnoticed failure.
        self.assertEqual(outreach.validate_copy(draft), [])
        self.assertEqual(
            verify.verify_copy(
                item.lead, draft, item.action_type, level=verify.LEVEL_STANDARD, today=TODAY
            ),
            [],
        )

        review = review_outcome(
            item, outreach.CopyOutcome(text=draft), verify.LEVEL_STANDARD, TODAY
        )

        self.assertIsInstance(review, outreach.ReviewOutcome)
        self.assertEqual(review.suggested_copy, draft)
        self.assertFalse(review.needs_human)
        self.assertEqual(review.further_action, "")
        self.assertEqual(review.shape_problem_count, 0)
        self.assertEqual(review.violation_count, 0)

    @unittest.expectedFailure
    def test_a_shape_problem_keeps_the_draft_and_names_the_shape_failure(self):
        """A draft that stopped looking like the requested email is the classic symptom of a
        successful injection: kept, because a reviewer needs to see what came back, and routed to a
        human with the problems spelled out."""
        from project.app.services.outreach import review_outcome

        item = self._item()
        draft = _shape_broken(_draft_for(item.lead))
        problems = outreach.validate_copy(draft)
        self.assertEqual(len(problems), 1)  # the missing Subject line, and only that

        review = review_outcome(
            item, outreach.CopyOutcome(text=draft), verify.LEVEL_STANDARD, TODAY
        )

        self.assertEqual(review.suggested_copy, draft)  # kept for reference
        self.assertTrue(review.needs_human)
        self.assertEqual(review.shape_problem_count, 1)
        self.assertEqual(review.violation_count, 0)  # the grounding gate is clean
        self.assertEqual(review.further_action, outreach.format_shape_problems(problems))

    @unittest.expectedFailure
    def test_a_grounding_violation_keeps_the_draft_and_names_the_violations(self):
        """The mirror image: well-shaped copy that contradicts the record. The counts are carried
        rather than recomputed — re-running a fail-closed gate is a second chance to disagree with
        the verdict already reached."""
        from project.app.services.outreach import review_outcome

        item = self._item()
        draft = _ungrounded(_draft_for(item.lead))
        self.assertEqual(outreach.validate_copy(draft), [])  # shape is fine
        violations = verify.verify_copy(
            item.lead, draft, item.action_type, level=verify.LEVEL_STANDARD, today=TODAY
        )
        self.assertEqual(len(violations), 1)

        review = review_outcome(
            item, outreach.CopyOutcome(text=draft), verify.LEVEL_STANDARD, TODAY
        )

        self.assertEqual(review.suggested_copy, draft)
        self.assertTrue(review.needs_human)
        self.assertEqual(review.shape_problem_count, 0)
        self.assertEqual(review.violation_count, 1)
        self.assertEqual(review.further_action, verify.format_violations(violations))

    @unittest.expectedFailure
    def test_a_failed_copy_outcome_reports_what_the_run_spent(self):
        """The clause telling a reviewer this row is noise rather than work is "gave up after 4
        attempt(s) over 31.4s", and neither number exists anywhere but the ``CopyOutcome``."""
        from project.app.services.outreach import review_outcome

        item = self._item()
        error = LLMRateLimitError("slow down", provider="claude", status_code=429)
        outcome = outreach.CopyOutcome(error=error, attempts=4, elapsed_s=31.44)

        review = review_outcome(item, outcome, verify.LEVEL_STANDARD, TODAY)

        self.assertEqual(review.suggested_copy, "")  # nothing came back to keep
        self.assertTrue(review.needs_human)
        self.assertEqual(
            review.further_action,
            outreach.COPY_RETRIES_EXHAUSTED.format(
                attempts=4,
                elapsed="31.4",
                provider="claude",
                kind=outreach.failure_kind(error),
                detail=outreach._safe_detail(error),
                action_type=item.action_type,
            ),
        )
        # Neither gate ran: there is no copy to check.
        self.assertEqual(review.shape_problem_count, 0)
        self.assertEqual(review.violation_count, 0)

    @unittest.expectedFailure
    def test_an_unmatched_classification_never_reaches_the_gates(self):
        """``UNKNOWN`` is decided ahead of both gates and gets its own message — real BD judgement,
        which must read differently from a provider having a bad thirty seconds (MUS-26c)."""
        from project.app.services.outreach import review_outcome

        item = self._item("lead_unmatched")

        review = review_outcome(item, outreach.CopyOutcome(), verify.LEVEL_STANDARD, TODAY)

        self.assertEqual(review.suggested_copy, "")
        self.assertTrue(review.needs_human)
        self.assertEqual(
            review.further_action,
            outreach.CLASSIFICATION_UNMATCHED.format(
                contact_name=item.lead.contact_name,
                agency_name=item.lead.agency_name,
            ),
        )


class SnapshotForTests(SimpleTestCase):
    """``snapshot_for`` is phase 5's snapshot comprehension, lifted out whole.

    Both snapshots are taken once at planning time and never recomputed: every relative figure in
    the trace ("72d since last login") is only true as of ``trace.today``, so a read-time recompute
    would contradict the ``reason`` prose persisted beside it (section 9.9). ``SimpleTestCase``
    again — ``explain`` and ``build_verification`` are pure, and a snapshot reaching for the ORM
    would put queries inside the pinned phase.
    """

    def _item_and_review(self, lead_id="lead_nudge"):
        from project.app.services.outreach import classify_lead, review_outcome

        item = classify_lead(_lead_by_id(lead_id), today=TODAY)
        outcome = outreach.CopyOutcome(text=_draft_for(item.lead))
        return item, review_outcome(item, outcome, verify.LEVEL_STANDARD, TODAY)

    @unittest.expectedFailure
    def test_rule_trace_is_explains_envelope_modulo_the_generated_at_stamp(self):
        """``RunLead.rule_trace`` holds ``explain()``'s schema-v1 envelope — the same value
        ``OutreachAction.rule_trace`` holds, which is what lets MUS-40's ``RuleTrace`` component
        render a composer row unchanged."""
        from project.app.services.outreach import snapshot_for

        item, review = self._item_and_review()

        rule_trace, _verification = snapshot_for(
            item, review, level=verify.LEVEL_STANDARD, today=TODAY
        )
        expected = outreach.explain(item.lead, TODAY)

        self.assertEqual(rule_trace["version"], outreach.TRACE_SCHEMA_VERSION)
        self.assertEqual(rule_trace["today"], TODAY.isoformat())
        # `generated_at` is `datetime.now()` at call time and the two calls can land on either side
        # of a second boundary, so it is checked for shape and dropped from the content comparison.
        self.assertRegex(rule_trace["generated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(
            {k: v for k, v in rule_trace.items() if k != "generated_at"},
            {k: v for k, v in expected.items() if k != "generated_at"},
        )
        # The envelope's own authority claim: the trace agrees with the item it was snapshotted
        # beside, so a UI can render one from the other.
        self.assertEqual(rule_trace["priority"]["value"], item.priority)
        self.assertEqual(rule_trace["action"]["value"], item.action_type)

    @unittest.expectedFailure
    def test_the_verification_describes_the_reviewed_copy_not_the_raw_draft(self):
        """``verification`` always describes the copy in play, and phase 4 decides what that is: a
        draft the gates kept, or the empty string a failed generation leaves behind. Snapshotting
        ``outcome.text`` would report on copy that was never saved."""
        from project.app.services.outreach import review_outcome, snapshot_for

        item, review = self._item_and_review()
        draft = review.suggested_copy
        self.assertNotEqual(draft, "")

        _trace, verification = snapshot_for(item, review, level=verify.LEVEL_STANDARD, today=TODAY)

        self.assertEqual(
            verification,
            queue_copy.build_verification(
                item.lead, draft, item.action_type, level=verify.LEVEL_STANDARD, today=TODAY
            ),
        )
        self.assertEqual(verification["copy"], queue_copy.normalize_copy(draft))
        self.assertTrue(verification["can_approve"])  # a clean draft is approvable
        self.assertIn("claims", verification)

        # The failure case, where review and outcome disagree about the copy: a provider error
        # keeps its text out of the row entirely.
        failed = review_outcome(
            item,
            outreach.CopyOutcome(error=LLMRateLimitError("slow down", provider="claude")),
            verify.LEVEL_STANDARD,
            TODAY,
        )
        _trace, empty = snapshot_for(item, failed, level=verify.LEVEL_STANDARD, today=TODAY)
        self.assertEqual(empty["copy"], "")
        # No copy means nothing to check — and the row still reaches a reviewer, because
        # `needs_human` is what routes it. The report describes the copy in play, not the gate.
        self.assertEqual(empty["checked_count"], 0)
        self.assertEqual(empty["claims"], [])
        self.assertTrue(failed.needs_human)


class _StubProvider:
    """``agenerate_copy`` for the invariance run: deterministic, and counted.

    ``outreach.agenerate_copy`` is the seam phase 3 goes through (see ``tests_planner_async``),
    patched as a module attribute so the binding holds. The lead is identified from the prompt
    because phase 3 is deliberately handed no lead object. ``calls`` is a positive control: if
    client resolution or the pool short-circuited, every row would come back a failure and the
    field comparisons below would report a rules regression that never happened.
    """

    def __init__(self, today):
        self.today = today
        self.calls = 0

    async def __call__(self, lead, action_type, reason, *, prompt=None, client=None, **_runtime):
        self.calls += 1
        for fixture in _fixture_leads(self.today):
            if f"- Agency: {fixture.agency_name}" in prompt:
                return _draft_for(fixture)
        raise AssertionError(f"stub could not identify the lead: {prompt[:200]!r}")


@override_settings(
    COPY_VERIFY_LEVEL=verify.LEVEL_STANDARD,
    OUTREACH_MAX_IN_FLIGHT=4,
    # Pinned off, not inherited. The agent path (MUS-29) reaches the provider through
    # `run_agent_lead` rather than `agenerate_copy`, so an operator with OUTREACH_AGENT_ENABLED=1 in
    # their environment would run this guard against a different phase 3 — and against a live
    # provider, since the stub above would never be reached. The composer is single-shot by
    # contract ("named non-goals"), so single-shot is what this pins.
    OUTREACH_AGENT_ENABLED=False,
)
class PlannerInvarianceTests(TestCase):
    """The regression guard: ``plan_outreach()`` did not move.

    A pure extraction is only pure if the caller it was extracted from now goes through the
    extracted code. Every other test here would pass against an implementation that reproduced the
    rules independently while ``plan_outreach()`` kept its own copy — and that codebase would drift
    the day someone fixed a rule in one of the two places.
    """

    @unittest.expectedFailure
    def test_plan_outreach_rows_equal_the_three_functions_composed_by_hand(self):
        """Every persisted field against all three extracted functions composed by hand over the
        same leads — not counts, and not a spot check."""
        from project.app.services.outreach import classify_lead, review_outcome, snapshot_for

        for fields in _fixture_fields(datetime.date.today()):
            Lead.objects.create(**fields)

        # `plan_outreach` fixes the run's date once, in phase 1, by calling `date.today()`, so the
        # hand composition must use the same value: read here and re-read after the run, because a
        # run straddling midnight would compare two different days — a fixture problem, not a bug.
        today = datetime.date.today()
        stub = _StubProvider(today)
        with mock.patch.object(outreach, "agenerate_copy", stub):
            planned = outreach.plan_outreach()
        self.assertEqual(today, datetime.date.today(), "the run straddled midnight")

        rows = {row.lead_id: row for row in planned}
        self.assertEqual(len(rows), 5)
        self.assertEqual(OutreachAction.objects.count(), 5)
        # Four leads carry a prompt; `lead_unmatched` must never reach phase 3.
        self.assertEqual(stub.calls, 4)

        open_keys = set()
        composed = {}
        for lead in Lead.objects.prefetch_related("events").order_by("id"):
            item = classify_lead(
                lead, today=today, suppressed=frozenset(), open_keys=frozenset(open_keys)
            )
            self.assertIsNotNone(item)
            # Phase 2 adds each key as it goes, so a later lead sharing one skips rather than
            # duplicating. Mirrored for faithfulness; these five keys are distinct.
            open_keys.add(item.dedupe_key)
            # Phase 3's contract restated as data: a lead with no prompt never reaches the provider
            # and comes back empty, and the text is normalized on the way out of the call, not by
            # phase 4.
            outcome = (
                outreach.CopyOutcome()
                if item.prompt is None
                else outreach.CopyOutcome(text=queue_copy.normalize_copy(_draft_for(lead)))
            )
            review = review_outcome(item, outcome, verify.LEVEL_STANDARD, today)
            snapshot = snapshot_for(item, review, level=verify.LEVEL_STANDARD, today=today)
            composed[lead.id] = (item, review, snapshot)

        for lead_id, (item, review, (rule_trace, verification)) in composed.items():
            with self.subTest(lead=lead_id):
                row = rows[lead_id]
                # classify_lead's four fields.
                self.assertEqual(row.priority, item.priority)
                self.assertEqual(row.action_type, item.action_type)
                self.assertEqual(row.reason, item.reason)
                self.assertEqual(row.dedupe_key, item.dedupe_key)
                # review_outcome's three.
                self.assertEqual(row.needs_human, review.needs_human)
                self.assertEqual(row.suggested_copy, review.suggested_copy)
                self.assertEqual(row.further_action, review.further_action)
                # snapshot_for's two. Without them the third extracted function is unpinned against
                # the planner, and the trace MUS-40 renders could diverge from the one
                # `plan_outreach` writes with no test noticing.
                self.assertEqual(
                    {k: v for k, v in row.rule_trace.items() if k != "generated_at"},
                    {k: v for k, v in rule_trace.items() if k != "generated_at"},
                )
                self.assertEqual(row.verification, verification)

        # The comparison is only worth anything if the run exercised a spread.
        self.assertEqual(len({row.action_type for row in planned}), 5)
        self.assertGreater(len({row.priority for row in planned}), 1)
        self.assertEqual({row.needs_human for row in planned}, {True, False})

    @unittest.expectedFailure
    def test_the_three_functions_touch_no_database_over_a_prefetched_lead(self):
        """Why ``tests_planner_perf``'s ``planner_queries(n)`` pins come out identical: phases 2, 4
        and 5 all walk ``lead.events``, and ``.all()`` on a prefetched instance is served from
        ``_prefetched_objects_cache`` while ``.filter()`` or ``.count()`` would silently bypass it
        and restore the N+1 — one query per lead per phase."""
        from project.app.services.outreach import classify_lead, review_outcome, snapshot_for

        for fields in _fixture_fields(datetime.date.today()):
            Lead.objects.create(**fields)
        today = datetime.date.today()
        lead = Lead.objects.prefetch_related("events").get(id="lead_nudge")

        with self.assertNumQueries(0):
            item = classify_lead(lead, today=today)
            review = review_outcome(
                item,
                outreach.CopyOutcome(text=_draft_for(lead)),
                verify.LEVEL_STANDARD,
                today,
            )
            snapshot_for(item, review, level=verify.LEVEL_STANDARD, today=today)

        self.assertIsNotNone(item.prompt)  # the prompt really was built in there
