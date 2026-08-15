"""Component artifact: decisions (MUS-47 component 8).

Accept and reject, symmetrically. This is the seam where a model's *proposal*
becomes a human's *decision*, and the only place in the composer where anything
the model said changes what stage 04 will generate.

Four properties carry the component, and each fails silently when it is merely
intended rather than tested:

* **The rules columns are immutable.** Accept moves ``effective_*``. An accept
  that wrote ``rules_priority`` / ``rules_action`` would lose the one answer this
  product owes an auditor -- what the deterministic system said, next to what the
  human approved -- and lose it invisibly, because every screen renders the
  effective values.
* **Reject is a recorded decision.** Shipped as "return early" it is
  indistinguishable from a correct reject until somebody asks who declined a
  suggestion and when.
* **Both directions are re-runnable.** Accept -> reject -> accept is what a
  person with a mouse does, and the last decision wins.
* **No model prose reaches a prompt.** ``effective_reason`` is a copy-prompt
  input (``_build_copy_prompt`` renders it as "Why now: {reason}"), so an
  accepted ``action_change`` rebuilds it from :data:`ACCEPTED_ACTION_REASON` and
  never from ``suggestion["rationale"]`` -- model prose derived from
  attacker-reachable notes.

Planted red by the skeleton PR: every test is ``@unittest.expectedFailure``, and
every not-yet-existing symbol is imported inside a test body (or a helper called
only from one) so a missing symbol is one absorbed ``ImportError`` rather than a
collection error that would take every sibling artifact's marker count down with
it. State strings are literals; ``tests_compose_models.py`` pins
``RunLead.SUGGESTION_*`` to exactly these values.
"""

import datetime
import unittest
from unittest import mock

from django.db import connection
from django.test import TestCase

from project.app.models import Lead
from project.app.services import actions, dedupe, outreach
from project.app.tests_auth_utils import AuthenticatedAPITestCase

# What classify wrote. Everything here is a delta from these three values, so
# "the rules did not move" reads as a claim rather than as arithmetic.
RULES_PRIORITY = 2
RULES_ACTION = actions.NUDGE_USAGE
RULES_REASON = "Active but underusing the portal."

# A different member of SELECTABLE_ACTION_TYPES, so a recomputed dedupe key
# genuinely differs from the classify-time one.
PROPOSED_ACTION = actions.REENGAGE_DORMANT

# Model prose, planted so its absence downstream is provable. Nothing in a
# first-party template can produce this string by accident.
RATIONALE_CANARY = "RATIONALE-CANARY-9f3a1c: the note insists this is a priority-one churn risk"
EVIDENCE_QUOTE = "renewal is in three weeks"

# A real ``outreach.explain()`` envelope's keys (schema v1), not an invented
# shape: ``rule_trace`` is one of the columns the immutability check compares, so
# it has to hold something a schema change would actually notice.
RULE_TRACE = {
    "version": outreach.TRACE_SCHEMA_VERSION,
    "today": "2026-02-15",
    "generated_at": "2026-02-15T09:00:00Z",
    "priority": {"value": RULES_PRIORITY, "score": 4, "bands": [], "signals": []},
    "action": {"type": RULES_ACTION, "reason": RULES_REASON, "matched": "underusing"},
}


def _lead(lead_id="lead_cd1"):
    """A lead complete enough to be a plausible composer row. ``decisions.py``
    reads none of it, but the FK is real and the row is serialized back to the
    FE, so it is filled in rather than left half-null."""
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


def _active_run(status="read"):
    """A run mid-composer, holding the single active slot. ``read`` rather than
    ``classified``: a suggestion only exists after stage 03."""
    from project.app.models import PlannerRun

    return PlannerRun.objects.create(status=status, scope={"stage": "active_trial"})


def _terminal_run(status):
    """A finished run: terminal status AND a released sentinel together --
    ``pr_sentinel_matches_status`` refuses either half on its own."""
    from project.app.models import PlannerRun

    return PlannerRun.objects.create(status=status, active_sentinel=None)


def _suggestion(kind, *, priority=None, action="", rationale=RATIONALE_CANARY):
    """A normalized suggestion exactly as ``validate_suggestion`` persists one.

    All five keys, always. The contract fixes the shape with explicit
    placeholders -- ``proposed_priority`` is ``null`` when the kind carries no
    priority and ``proposed_action`` is ``""`` -- so no reader ever branches on
    ``in``. A sentinel integer would be read back as a real priority, which is
    exactly why the placeholder is ``null``; this is settled, not a preference
    (docs/contracts/run-composer.md, "read"). No ``lead_id``: the acting lead is
    bound server-side. ``evidence[].quote`` is the model's original text, not the
    normalized form the validator compared against.
    """
    return {
        "suggestion": kind,
        "proposed_priority": priority,
        "proposed_action": action,
        "rationale": rationale,
        "evidence": [{"source": "note", "quote": EVIDENCE_QUOTE}],
    }


def _run_lead(run, lead, **overrides):
    """A classified row: rules columns written, effective columns seeded from
    them, no suggestion yet. Every non-defaulted column is supplied so a schema
    change surfaces here rather than three assertions later."""
    from project.app.models import RunLead

    fields = {
        "rules_priority": RULES_PRIORITY,
        "rules_action": RULES_ACTION,
        "rules_reason": RULES_REASON,
        "rule_trace": RULE_TRACE,
        "dedupe_key": dedupe.dedupe_key(lead.id, RULES_ACTION),
        "effective_priority": RULES_PRIORITY,
        "effective_action": RULES_ACTION,
        "effective_reason": RULES_REASON,
        "suggestion": {},
        "suggestion_state": "none",
    }
    fields.update(overrides)
    return RunLead.objects.create(run=run, lead=lead, **fields)


def _proposed(run, lead, suggestion, **overrides):
    """A row the read stage left a live proposal on: ``effective_*`` still equals
    ``rules_*`` (nothing applies until a human says so)."""
    return _run_lead(run, lead, suggestion=suggestion, suggestion_state="proposed", **overrides)


def _rules_snapshot(row):
    """The rules columns as the database holds them, read back through
    ``.values()``.

    Load-bearing: an accept that wrote ``rules_priority`` and handed back a stale
    object would satisfy any assertion made against the Python attribute. Only a
    fresh read proves the stored bytes did not move.
    """
    from project.app.models import RunLead

    return (
        RunLead.objects.filter(pk=row.pk)
        .values("rules_priority", "rules_action", "rules_reason", "rule_trace")
        .get()
    )


def _decision_snapshot(row):
    """Every column a decision may touch, read fresh from the database."""
    from project.app.models import RunLead

    return (
        RunLead.objects.filter(pk=row.pk)
        .values(
            "effective_priority",
            "effective_action",
            "effective_reason",
            "dedupe_key",
            "suggestion",
            "suggestion_state",
            "suggestion_decided_at",
            "suggestion_decided_by",
        )
        .get()
    )


class _InjectedFailure(RuntimeError):
    """Raised from the wrapper below. Deliberately not a ``DatabaseError``, so
    Django neither rewrites it nor marks the outer transaction unusable."""


class _FailAfterFirstWrite:
    """A ``connection.execute_wrapper`` that lets the first ``UPDATE`` of
    ``table`` reach the database and then raises, wherever the caller was headed
    next.

    Hooked at the SQL layer rather than at a Python symbol so it fires for a
    ``.save()``, a ``.update()`` or a ``bulk_update()`` alike -- the component is
    free to pick any of them.
    """

    def __init__(self, table):
        self.table = table
        self.writes = 0

    def __call__(self, execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        statement = (sql or "").lstrip()
        if statement.upper().startswith("UPDATE") and self.table in statement:
            self.writes += 1
            if self.writes == 1:
                raise _InjectedFailure("injected failure, mid-decision")
        return result


class AcceptSuggestionTests(TestCase):
    """``decisions.accept_suggestion`` -- the write that gives a model proposal
    effect, and the invariants that keep it from giving it authority."""

    @unittest.expectedFailure
    def test_accept_moves_the_effective_priority_and_stamps_the_decision(self):
        """The plain case, a ``raise`` from P2 to P1: the effective priority
        moves, the state becomes ``accepted``, and actor plus timestamp are
        recorded -- "who agreed, and when" is why this is a decision rather than a
        setting. ``effective_action``, ``effective_reason`` and ``dedupe_key`` are
        asserted *unchanged*, because a priority move does not change what is
        being recommended.
        """
        from project.app.services.compose import decisions

        lead = _lead()
        run = _active_run()
        row = _proposed(run, lead, _suggestion("raise", priority=1))

        returned = decisions.accept_suggestion(run, lead.id, actor="reviewer@example.com")

        self.assertEqual(returned.pk, row.pk)
        stored = _decision_snapshot(row)
        self.assertEqual(stored["effective_priority"], 1)
        self.assertEqual(stored["suggestion_state"], "accepted")
        self.assertIsNotNone(stored["suggestion_decided_at"])
        self.assertEqual(stored["suggestion_decided_by"], "reviewer@example.com")
        # Untouched by a priority-only accept:
        self.assertEqual(stored["effective_action"], RULES_ACTION)
        self.assertEqual(stored["effective_reason"], RULES_REASON)
        self.assertEqual(stored["dedupe_key"], dedupe.dedupe_key(lead.id, RULES_ACTION))
        # The proposal survives verbatim, all five keys and their placeholders
        # intact -- the card re-renders from this and nothing branches on `in`.
        self.assertEqual(
            stored["suggestion"],
            {
                "suggestion": "raise",
                "proposed_priority": 1,
                "proposed_action": "",
                "rationale": RATIONALE_CANARY,
                "evidence": [{"source": "note", "quote": EVIDENCE_QUOTE}],
            },
        )
        # The returned object is the decided row, not a pre-decision copy.
        self.assertEqual(returned.effective_priority, 1)
        self.assertEqual(returned.suggestion_state, "accepted")

    @unittest.expectedFailure
    def test_accept_never_writes_the_rules_columns(self):
        """Organizing principle #1, asserted at the database.

        Both kinds are exercised: a ``lower`` touches only the priority, an
        ``action_change`` rewrites the action, the reason and the dedupe key --
        and the second is the one it is tempting to implement by moving the rules
        columns and letting ``effective_*`` follow. The comparison is between two
        fresh ``.values()`` reads, so it is a claim about stored bytes rather than
        about an ORM instance that may never have been refreshed.
        """
        from project.app.services.compose import decisions

        cases = {
            "lower": _suggestion("lower", priority=3),
            "action_change": _suggestion("action_change", action=PROPOSED_ACTION),
        }
        for index, (kind, suggestion) in enumerate(cases.items()):
            with self.subTest(kind=kind):
                lead = _lead(f"lead_imm{index}")
                run = _active_run()
                row = _proposed(run, lead, suggestion)
                before = _rules_snapshot(row)

                decisions.accept_suggestion(run, lead.id, actor="reviewer@example.com")

                self.assertEqual(_rules_snapshot(row), before)
                # ...and the accept did something, so the equality above is
                # immutability rather than a no-op.
                self.assertEqual(_decision_snapshot(row)["suggestion_state"], "accepted")
                # Retire the run: pr_one_active_run admits exactly one, so the
                # next subtest cannot open its own until this one lets go.
                run.status, run.active_sentinel = "completed", None
                run.save(update_fields=["status", "active_sentinel"])

    @unittest.expectedFailure
    def test_an_accepted_action_change_rekeys_the_recommendation(self):
        """The identity of the recommendation changed, so the dedupe key changes
        with it.

        ``dedupe_key`` is what "dismiss is permanent" and "don't double-send" are
        both built on. Keeping the classify-time key on a row whose action is now
        ``reengage_dormant`` would let a previously dismissed ``nudge_usage``
        suppress an action nobody dismissed, and would collide the generated
        ``OutreachAction`` with an open item for a different recommendation. The
        expected key is computed from ``dedupe.dedupe_key`` against the *new*
        action; read off the row it would only prove the row equals itself.
        """
        from project.app.services.compose import decisions

        lead = _lead()
        run = _active_run()
        row = _proposed(run, lead, _suggestion("action_change", action=PROPOSED_ACTION))
        old_key = dedupe.dedupe_key(lead.id, RULES_ACTION)

        decisions.accept_suggestion(run, lead.id, actor="reviewer@example.com")

        stored = _decision_snapshot(row)
        self.assertEqual(stored["effective_action"], PROPOSED_ACTION)
        self.assertEqual(stored["dedupe_key"], dedupe.dedupe_key(lead.id, PROPOSED_ACTION))
        self.assertNotEqual(stored["dedupe_key"], old_key)
        # An action_change carries proposed_priority: null -- the placeholder, not
        # a sentinel -- and moves no priority. The two axes are independent, and
        # inferring one from the other applies a change the reviewer never saw.
        self.assertIsNone(stored["suggestion"]["proposed_priority"])
        self.assertEqual(stored["effective_priority"], RULES_PRIORITY)

    @unittest.expectedFailure
    def test_an_accepted_action_change_rebuilds_the_reason_from_the_first_party_template(self):
        """The fail-closed gate's third commitment.

        ``effective_reason`` becomes ``OutreachAction.reason``, which
        ``_build_copy_prompt`` renders verbatim as "Why now: {reason}" inside the
        *trusted instruction region*. ``suggestion["rationale"]`` is model prose
        written after reading attacker-reachable ``hubspot_notes``. Letting the
        second become the first opens a path from a CRM note into the trusted half
        of a prompt -- the exact channel the untrusted pipeline exists to close,
        bypassed by a copy assignment. So the reason is rebuilt from
        ``ACCEPTED_ACTION_REASON`` and the canary is asserted absent from every
        column a downstream stage reads, while still present on the row -- which
        is what makes the absence deliberate rather than a lucky deletion.
        """
        from project.app.services.compose import decisions

        lead = _lead()
        run = _active_run()
        row = _proposed(run, lead, _suggestion("action_change", action=PROPOSED_ACTION))

        decisions.accept_suggestion(run, lead.id, actor="reviewer@example.com")

        stored = _decision_snapshot(row)
        self.assertEqual(
            stored["effective_reason"],
            decisions.ACCEPTED_ACTION_REASON.format(old=RULES_ACTION, new=PROPOSED_ACTION),
        )
        # Both action names are in there, so the audit line says what changed.
        self.assertIn(RULES_ACTION, stored["effective_reason"])
        self.assertIn(PROPOSED_ACTION, stored["effective_reason"])
        self.assertNotIn(RATIONALE_CANARY, stored["effective_reason"])
        self.assertNotIn(RATIONALE_CANARY, stored["effective_action"])
        self.assertNotIn(RATIONALE_CANARY, _rules_snapshot(row)["rules_reason"])
        self.assertEqual(stored["suggestion"]["rationale"], RATIONALE_CANARY)


class RejectSuggestionTests(TestCase):
    """``decisions.reject_suggestion`` -- the outcome easiest to ship as a no-op,
    and the one an operator uses most."""

    @unittest.expectedFailure
    def test_reject_restores_the_effective_columns_to_the_rules_verdict(self):
        """Reject after accept has real work to do.

        The row has an ``action_change`` already applied, so all four of
        ``effective_priority``, ``effective_action``, ``effective_reason`` and
        ``dedupe_key`` sit at model-derived values. Every one goes back to what
        classify decided -- the dedupe key included, because a row still carrying
        the proposed action's key would generate against a recommendation the
        reviewer just declined. A reject that only flipped ``suggestion_state``
        leaves the change in force while the UI says "rejected": wrong behavior,
        confidently labelled.
        """
        from project.app.services.compose import decisions

        lead = _lead()
        run = _active_run()
        row = _proposed(run, lead, _suggestion("action_change", action=PROPOSED_ACTION))
        decisions.accept_suggestion(run, lead.id, actor="reviewer@example.com")
        self.assertEqual(_decision_snapshot(row)["effective_action"], PROPOSED_ACTION)

        returned = decisions.reject_suggestion(run, lead.id, actor="second@example.com")

        self.assertEqual(returned.pk, row.pk)
        stored = _decision_snapshot(row)
        self.assertEqual(stored["effective_priority"], RULES_PRIORITY)
        self.assertEqual(stored["effective_action"], RULES_ACTION)
        self.assertEqual(stored["effective_reason"], RULES_REASON)
        self.assertEqual(stored["dedupe_key"], dedupe.dedupe_key(lead.id, RULES_ACTION))
        self.assertEqual(stored["suggestion_state"], "rejected")
        self.assertEqual(stored["suggestion_decided_by"], "second@example.com")
        self.assertEqual(_rules_snapshot(row)["rules_action"], RULES_ACTION)

    @unittest.expectedFailure
    def test_reject_on_an_untouched_row_is_still_a_recorded_decision(self):
        """The case where "return early" would look correct.

        Rejecting a freshly proposed suggestion changes no effective value, so the
        only visible effect is the record -- and that record is the point: a
        rejected row and a row nobody looked at must not be the same row, or
        "which suggestions did the team turn down?" has no answer and the read
        stage's value can never be measured. The proposal is retained rather than
        blanked, for the same reason.
        """
        from project.app.services.compose import decisions

        lead = _lead()
        run = _active_run()
        row = _proposed(run, lead, _suggestion("raise", priority=1))
        undecided = _proposed(run, _lead("lead_cd2"), _suggestion("raise", priority=1))

        decisions.reject_suggestion(run, lead.id, actor="reviewer@example.com")

        stored = _decision_snapshot(row)
        self.assertEqual(stored["suggestion_state"], "rejected")
        self.assertIsNotNone(stored["suggestion_decided_at"])
        self.assertEqual(stored["suggestion_decided_by"], "reviewer@example.com")
        self.assertEqual(stored["suggestion"]["rationale"], RATIONALE_CANARY)
        self.assertEqual(stored["effective_priority"], RULES_PRIORITY)
        # The neighbour proves the two states are distinct and that a decision on
        # one lead did not sweep the run.
        neighbour = _decision_snapshot(undecided)
        self.assertEqual(neighbour["suggestion_state"], "proposed")
        self.assertIsNone(neighbour["suggestion_decided_at"])
        self.assertEqual(neighbour["suggestion_decided_by"], "")


class DecisionSymmetryTests(TestCase):
    """Neither outcome is terminal, and neither is privileged."""

    @unittest.expectedFailure
    def test_accept_reject_accept_is_legal_and_the_last_decision_wins(self):
        """The property is stronger than "the calls return without raising": the
        effective columns after the *second* accept must be identical to what they
        were after the first. An implementation that computed the accept as a
        delta from the current row -- rather than from the rules verdict plus the
        proposal -- drifts here silently, re-lowering an already-lowered priority.
        The cycle runs on an ``action_change`` so the dedupe key makes the round
        trip too; it is the value most likely to be recomputed from the wrong side.
        """
        from project.app.services.compose import decisions

        lead = _lead()
        run = _active_run()
        row = _proposed(run, lead, _suggestion("action_change", action=PROPOSED_ACTION))

        decisions.accept_suggestion(run, lead.id, actor="first@example.com")
        after_first = _decision_snapshot(row)
        decisions.reject_suggestion(run, lead.id, actor="second@example.com")
        after_reject = _decision_snapshot(row)
        decisions.accept_suggestion(run, lead.id, actor="third@example.com")
        after_final = _decision_snapshot(row)

        # The middle of the cycle really did undo the first decision.
        self.assertEqual(after_reject["effective_action"], RULES_ACTION)
        self.assertEqual(after_reject["dedupe_key"], dedupe.dedupe_key(lead.id, RULES_ACTION))

        for column in ("effective_priority", "effective_action", "effective_reason", "dedupe_key"):
            with self.subTest(column=column):
                self.assertEqual(after_final[column], after_first[column])
        self.assertEqual(after_final["suggestion_state"], "accepted")
        # Last writer wins, on both halves of the stamp.
        self.assertEqual(after_final["suggestion_decided_by"], "third@example.com")
        self.assertGreaterEqual(
            after_final["suggestion_decided_at"], after_first["suggestion_decided_at"]
        )
        self.assertEqual(_rules_snapshot(row)["rules_action"], RULES_ACTION)


class DecisionRefusalTests(TestCase):
    """The two ways a decision must be refused, and the guarantee that a refused
    or interrupted decision writes nothing at all."""

    @unittest.expectedFailure
    def test_deciding_a_lead_with_no_suggestion_raises_and_writes_nothing(self):
        """``suggestion_state == "none"`` means no proposal exists -- the read
        never reached this lead, its provider call failed, or
        ``validate_suggestion`` discarded what came back. A silent success would
        stamp an actor and a timestamp onto a decision nobody made, and would
        report a decided row to a UI counting outstanding suggestions.

        ``ValueError`` is the pinned base, following ``ScopeError`` in the same
        contract: the component may name a narrower class, but the view needs one
        thing to catch and map to its 409. Both entry points are covered --
        rejecting a suggestion that does not exist is exactly as meaningless as
        accepting one.
        """
        from project.app.services.compose import decisions

        lead = _lead()
        run = _active_run()
        row = _run_lead(run, lead)  # classified, never read -> state "none"
        before = _decision_snapshot(row)

        for name in ("accept_suggestion", "reject_suggestion"):
            with self.subTest(entry_point=name):
                with self.assertRaises(ValueError):
                    getattr(decisions, name)(run, lead.id, actor="reviewer@example.com")
                self.assertEqual(_decision_snapshot(row), before)
                self.assertEqual(_decision_snapshot(row)["suggestion_decided_by"], "")

    @unittest.expectedFailure
    def test_a_decision_on_a_terminal_run_is_refused(self):
        """Both terminal statuses are pinned, because they arrive by different
        routes: ``completed`` is a run that shipped, ``discarded`` one that was
        abandoned, and only one of the two is likely to be remembered.

        The realistic trigger is a stale browser tab still showing Accept and
        Reject. The expensive failure is accepting anyway: the run's
        ``OutreachAction`` rows have already been created and reviewed, so a late
        ``effective_action`` change edits the provenance of shipped work.
        ``runs.InvalidRunTransition`` is reused rather than a new exception
        invented -- the view already maps it to a 409.
        """
        from project.app.services.compose import decisions, runs

        for index, status in enumerate(("completed", "discarded")):
            with self.subTest(status=status):
                lead = _lead(f"lead_term{index}")
                run = _terminal_run(status)
                row = _proposed(run, lead, _suggestion("raise", priority=1))
                before = _decision_snapshot(row)

                with self.assertRaises(runs.InvalidRunTransition):
                    decisions.accept_suggestion(run, lead.id, actor="reviewer@example.com")
                with self.assertRaises(runs.InvalidRunTransition):
                    decisions.reject_suggestion(run, lead.id, actor="reviewer@example.com")

                self.assertEqual(_decision_snapshot(row), before)

    @unittest.expectedFailure
    def test_a_failure_part_way_through_a_decision_persists_nothing(self):
        """The decision is one transaction, proven the only way that can fail: by
        breaking it.

        A half-applied decision is the worst row this schema can hold -- state
        ``accepted`` over unmoved effective columns renders as accepted in the UI
        while stage 04 generates the rules verdict, and nothing reports the
        disagreement. So the first ``RunLead`` write is allowed to reach the
        database and the call is then blown up from underneath. Wrapped in
        ``atomic()`` the savepoint unwinds and the row is untouched; unwrapped,
        whatever landed stays landed and every assertion below fails.

        Counting savepoint spans instead would prove nothing: ``TestCase`` opens
        its own savepoint around the test body, so "a savepoint before the write
        and a release after it" holds whether or not the service has a transaction
        of its own, and a single-``UPDATE`` implementation makes "no release
        between the first and last write" vacuously true.
        """
        from project.app.models import RunLead
        from project.app.services.compose import decisions

        lead = _lead()
        run = _active_run()
        row = _proposed(run, lead, _suggestion("action_change", action=PROPOSED_ACTION))
        before, rules_before = _decision_snapshot(row), _rules_snapshot(row)

        injector = _FailAfterFirstWrite(RunLead._meta.db_table)
        with connection.execute_wrapper(injector), self.assertRaises(_InjectedFailure):
            decisions.accept_suggestion(run, lead.id, actor="reviewer@example.com")

        # Positive control: the injection is only meaningful if it fired, and it
        # only fires once a decision write has actually reached the database.
        self.assertEqual(injector.writes, 1)
        self.assertEqual(_decision_snapshot(row), before)
        self.assertEqual(_rules_snapshot(row), rules_before)


class DecisionEndpointTests(AuthenticatedAPITestCase):
    """``POST /api/runs/{id}/suggestions/{lead_id}/accept/`` and ``/reject/``.

    Both outcomes are driven over HTTP, because a decision the API cannot express
    is a decision the product does not have. Paths are written out rather than
    reversed: the contract freezes the URLs, not the view names. Error bodies are
    ``{"code", "detail"}`` -- ``client.ts`` reads ``code`` as the machine slug it
    branches on, so an envelope keyed ``error`` is one the FE cannot use.
    """

    def _accept(self, run_id, lead_id, payload=None):
        return self.client.post(
            f"/api/runs/{run_id}/suggestions/{lead_id}/accept/",
            payload if payload is not None else {},
            content_type="application/json",
        )

    def _reject(self, run_id, lead_id, payload=None):
        return self.client.post(
            f"/api/runs/{run_id}/suggestions/{lead_id}/reject/",
            payload if payload is not None else {},
            content_type="application/json",
        )

    @unittest.expectedFailure
    def test_accept_endpoint_returns_the_decided_row_attributed_to_the_session(self):
        """200 with the ``RunLead`` the card re-renders from.

        The response carries both halves of the delta -- the rules verdict and the
        effective one -- because the UI shows "P2 -> P1 . accepted"; returning only
        the effective values would make the audit line unrenderable without a
        second round trip. ``suggestion_decided_by`` comes off the session: the
        body supplies a different actor and it is ignored, because attribution a
        client can set is worth nothing to whoever reads it back.
        """
        from project.app.models import PlannerRun, RunLead

        lead = _lead()
        run = PlannerRun.objects.create(status="read", scope={})
        row = _proposed(run, lead, _suggestion("raise", priority=1))

        resp = self._accept(run.pk, lead.id, {"actor": "forged@evil.example"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["lead_id"], lead.id)
        self.assertEqual(resp.data["suggestion_state"], "accepted")
        self.assertEqual(resp.data["effective_priority"], 1)
        self.assertEqual(resp.data["rules_priority"], RULES_PRIORITY)
        self.assertEqual(resp.data["rules_action"], RULES_ACTION)
        stored = RunLead.objects.get(pk=row.pk)
        self.assertEqual(stored.suggestion_decided_by, self.TEST_EMAIL)
        self.assertIsNotNone(stored.suggestion_decided_at)
        self.assertEqual(stored.rules_priority, RULES_PRIORITY)

    @unittest.expectedFailure
    def test_reject_endpoint_returns_the_restored_row_attributed_to_the_session(self):
        """The other outcome, driven the same way and asserted just as hard.
        Without its own endpoint the only way to decline a suggestion is to leave
        it undecided, collapsing "declined" and "not yet looked at" into one state
        for every downstream reader."""
        from project.app.models import PlannerRun, RunLead

        lead = _lead()
        run = PlannerRun.objects.create(status="read", scope={})
        row = _proposed(run, lead, _suggestion("action_change", action=PROPOSED_ACTION))
        self.assertEqual(self._accept(run.pk, lead.id).status_code, 200)

        resp = self._reject(run.pk, lead.id, {"actor": "forged@evil.example"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["suggestion_state"], "rejected")
        self.assertEqual(resp.data["effective_action"], RULES_ACTION)
        self.assertEqual(resp.data["effective_priority"], RULES_PRIORITY)
        stored = RunLead.objects.get(pk=row.pk)
        self.assertEqual(stored.suggestion_decided_by, self.TEST_EMAIL)
        self.assertEqual(stored.effective_reason, RULES_REASON)
        self.assertEqual(stored.dedupe_key, dedupe.dedupe_key(lead.id, RULES_ACTION))

    @unittest.expectedFailure
    def test_deciding_without_a_suggestion_is_a_409_that_changes_nothing(self):
        """Pinned at 409, not 400: the request is well formed and the lead is real
        -- it is the row's *state* that forbids the decision, the same distinction
        the queue draws with ``unverified_claims``. The slug is pinned too, since
        the FE branches on it to say "this suggestion is gone, reload" rather than
        "your request was malformed"."""
        from project.app.models import PlannerRun

        lead = _lead()
        run = PlannerRun.objects.create(status="read", scope={})
        row = _run_lead(run, lead)  # no suggestion was ever proposed
        before = _decision_snapshot(row)

        for label, post in (("accept", self._accept), ("reject", self._reject)):
            with self.subTest(endpoint=label):
                resp = post(run.pk, lead.id)
                self.assertEqual(resp.status_code, 409)
                self.assertEqual(resp.data["code"], "no_suggestion")
                self.assertTrue(resp.data["detail"])
                self.assertEqual(_decision_snapshot(row), before)

    @unittest.expectedFailure
    def test_deciding_on_a_terminal_run_is_a_409_that_changes_nothing(self):
        """The stale-tab case over HTTP, in the shape the FE already handles for
        every other refused stage call on a finished run. Both terminal statuses
        are covered here as well: a guard written against ``completed`` alone
        leaves the discard path open."""
        from project.app.models import PlannerRun

        for index, status in enumerate(("completed", "discarded")):
            with self.subTest(status=status):
                lead = _lead(f"lead_ep{index}")
                run = PlannerRun.objects.create(status=status, active_sentinel=None)
                row = _proposed(run, lead, _suggestion("raise", priority=1))
                before = _decision_snapshot(row)

                accept = self._accept(run.pk, lead.id)
                reject = self._reject(run.pk, lead.id)

                for resp in (accept, reject):
                    self.assertEqual(resp.status_code, 409)
                    self.assertEqual(resp.data["code"], "invalid_transition")
                self.assertEqual(_decision_snapshot(row), before)

    @unittest.expectedFailure
    def test_a_lead_outside_this_run_is_a_404_and_never_reaches_another_run(self):
        """The URL names a run *and* a lead, so the lookup is scoped to both.

        A lead id with no row is the ordinary 404. The one that matters is the
        second: a lead with a live proposal in a *different* run, addressed
        through this run's URL. A view looking the row up by ``lead_id`` alone
        would happily decide a foreign run's suggestion, undoing at the routing
        layer the "one row per lead per run" separation the schema enforces.
        """
        from project.app.models import PlannerRun

        other_run = PlannerRun.objects.create(status="completed", active_sentinel=None)
        foreign_lead = _lead("lead_foreign")
        foreign_row = _proposed(other_run, foreign_lead, _suggestion("raise", priority=1))
        before = _decision_snapshot(foreign_row)
        run = PlannerRun.objects.create(status="read", scope={})

        for lead_id in ("lead_does_not_exist", foreign_lead.id):
            with self.subTest(lead_id=lead_id):
                resp = self._accept(run.pk, lead_id)
                self.assertEqual(resp.status_code, 404)
                self.assertEqual(resp.data["code"], "not_found")
        self.assertEqual(_decision_snapshot(foreign_row), before)

    @unittest.expectedFailure
    def test_decision_endpoints_are_401_when_anonymous_and_write_nothing(self):
        """401 exactly, not 403 -- MUS-38's route guard redirects on 401 and does
        nothing on 403, so the two are not interchangeable.

        Worth pinning here specifically: an accepted ``action_change`` rewrites
        what stage 04 will generate and send, so this is an unauthenticated write
        with downstream cost. The service is patched to prove the rejection lands
        in front of it. The signed-in call at the end is the positive control: a
        patch that bound nothing -- because the view did ``from ... import
        accept_suggestion`` -- would satisfy ``assert_not_called`` by construction,
        making the test one that cannot fail.
        """
        from project.app.models import PlannerRun

        lead = _lead()
        run = PlannerRun.objects.create(status="read", scope={})
        row = _proposed(run, lead, _suggestion("raise", priority=1))
        before = _decision_snapshot(row)

        target = "project.app.services.compose.decisions"
        with (
            mock.patch(f"{target}.accept_suggestion", return_value=row) as accept,
            mock.patch(f"{target}.reject_suggestion", return_value=row) as reject,
        ):
            self.client.logout()
            self.assertEqual(self._accept(run.pk, lead.id).status_code, 401)
            self.assertEqual(self._reject(run.pk, lead.id).status_code, 401)
            accept.assert_not_called()
            reject.assert_not_called()

            self.client.force_login(self.user)
            self.assertEqual(self._accept(run.pk, lead.id).status_code, 200)
            self.assertEqual(accept.call_count, 1)

        self.assertEqual(_decision_snapshot(row), before)
