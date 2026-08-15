"""Component artifact: generate (MUS-47 component 9).

Stage 04 — the only stage that spends real money, and the only one that writes
``OutreachAction`` rows. Four properties that all fail *quietly* if they fail at
all: selection is the whole authorization and every skip is counted; a composer
draft is a planner draft (same prompt builder, both output gates,
``status="pending"`` on ``GET /api/queue/``, and the promotion to ``generated``
that makes ``close_run`` reachable); one dead provider call costs one lead; the
money number is measured rather than re-quoted from the estimate.

The fifth is the fail-closed gate. ``docs/plans/mus-47-run-composer.md`` takes
position (b) — *the verifier is not extended by this feature* — resting on
exactly three legs, one test each:

* ``suggestion["rationale"]`` never becomes a prompt input —
  ``test_no_model_derived_prose_reaches_the_generator``
* ``effective_priority`` is not in the copy prompt and is not added to it —
  ``test_the_effective_priority_never_reaches_the_copy_prompt``
* an accepted ``action_change`` moves only ``action_type``, and the verifier runs
  against the *changed* one —
  ``test_an_accepted_action_change_is_verified_against_the_new_action``

Delete any one and position (b) becomes an unsupported claim.

**The provider is never real here.** Every generating test patches
``services.llm._build_client`` — the single ``lru_cache``d constructor both
``get_llm_client()`` and ``build_client(provider)`` funnel through — so the patch
holds however stage 04 spells its import. A patch aimed at either wrapper would
bind nothing against a ``from … import``, which is why the one no-call assertion
below carries a positive control; ``tests_compose_lifecycle.py`` pins the same
seam from the other direction.

Red at skeleton: every test is ``@unittest.expectedFailure``, and every symbol
arriving with the generate PR — ``PlannerRun``, ``RunLead``, and everything in
``services/compose/generate.py`` — is imported *inside* a method or a helper, so
a missing name fails one test instead of killing collection.
"""

import datetime
import json
import re
import unittest
import uuid
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from project.app.models import Event, Lead, LLMModel, LLMProvider, OutreachAction
from project.app.services import actions, dedupe, llm, verify
from project.app.services.compose.decisions import ACCEPTED_ACTION_REASON
from project.app.services.llm import LLMBadRequestError, LLMClient, LLMResult
from project.app.services.outreach import (
    MAX_COPY_TOKENS,
    TRACE_SCHEMA_VERSION,
    determine_action,
    determine_priority,
    explain,
    failure_kind,
)
from project.app.tests_auth_utils import AuthenticatedAPITestCase

# The default fixture lead is `demo_completed` with no signup date — the one
# classification the rules reach without consulting the wall clock, so nothing
# here drifts overnight. No assertion hardcodes the verdict: expectations come
# from `determine_action` / `determine_priority`.
SCOPE = {"stage": "demo_completed"}

# The action an accepted `action_change` moves a row to. Asserted to differ from
# whatever the rules chose, so the test cannot pass vacuously.
CHANGED_ACTION = "nudge_usage"

# What an accepted `lower` moves `effective_priority` to.
LOWERED_PRIORITY = 3

# Overrides that make a prompt digit-scannable: every number `_build_copy_prompt`
# renders from the lead is pushed to 5..9, so no priority value (1, 2, 3) can be a
# standalone token for a reason unrelated to the leak under test. The template's
# own numbers are safe — "120 words" is one token and event dates are zero-padded.
# `make_lead`'s defaults would not do: 3 producers and a $1,000,000 book put a
# bare 1 and 3 in every prompt. CONTROL_DIGIT is present on purpose, as proof the
# scan can find a bare digit at all.
CONTROL_DIGIT = 7
PROMPT_DIGIT_SAFE = dict(
    num_producers=CONTROL_DIGIT,
    years_in_business=9,
    estimated_book_size_usd=5_000_000,
    quotes_created=6,
    quotes_submitted=5,
    deals_closed=8,
)

# A power user by the rules (past rule 1 because it has a signup date, then
# >= POWER_USER_DEALS closed and >= POWER_USER_SUBMISSIONS submitted) — the one
# classification `verify._check_offer` authorizes commercial promises for.
POWER_USER = dict(
    stage="active_trial",
    signed_up_date=datetime.date(2025, 3, 4),
    quotes_created=20,
    quotes_submitted=12,
    deals_closed=7,
)

# Copy pitching the OLD action — volume pricing, the reward action's whole point.
# Clean under `power_user_reward` (shape gate passes, `verify_copy` returns []),
# an `unauthorized_offer` under anything else. Numbers are spelled out so no
# amount, count or date check can fire and muddy which claim is being asserted.
REWARD_COPY = (
    "Subject: Volume pricing as your book grows\n"
    "\n"
    "Hi there,\n"
    "\n"
    "Your team has been one of the most active on Sure Lock this quarter, and agencies at "
    "that level usually qualify for volume pricing before they think to ask for it. The "
    "structure is simple: the more of your book you protect, the better the rate, and the "
    "paperwork is the same either way. I would rather walk you through what it would look "
    "like against your own numbers than send a rate card, because the interesting part is "
    "where the tiers land for an agency of your size rather than in the abstract.\n"
    "\n"
    "Would you have twenty minutes for a call this week?\n"
    "\n"
    "Best,\n"
    "Dana"
)

# Planted in an accepted suggestion's `rationale` and nowhere else — not in the
# notes, not in an event, not in `effective_reason`. Anything that finds it
# downstream found it by way of model-derived prose.
RATIONALE_CANARY = "ZQX-RATIONALE-CANARY-7741"

# The double's identity. "stub" is a registered provider name with deliberately
# no seeded catalog row, so the LLMModel below is unambiguously this module's.
PROVIDER = "stub"
MODEL = "stub-copy-1"
INPUT_PRICE_PER_MTOK = Decimal("3.0000")
OUTPUT_PRICE_PER_MTOK = Decimal("15.0000")
# Reported by the double on every successful call. Chosen so the arithmetic lands
# exactly on four decimal places and no rounding mode can be argued about:
# per lead, 1200/1e6 * 3 = 0.0036 in and 300/1e6 * 15 = 0.0045 out.
INPUT_TOKENS_PER_LEAD = 1200
OUTPUT_TOKENS_PER_LEAD = 300
USD_PER_LEAD = Decimal("0.0081")

# A well-formed ~110-word email that clears BOTH output gates against these
# fixtures — subject, one CTA, a legal word count, and no claim about the record
# for `verify_copy` to contradict. That is what makes the junk draft below a
# genuine contrast rather than two failures side by side.
WELL_SHAPED_COPY = (
    "Subject: A quick idea for finishing onboarding\n"
    "\n"
    "Hi there,\n"
    "\n"
    "Your team finished the demo a little while back, and the agencies that get the most out "
    "of Sure Lock tend to finish onboarding in the same week. It usually takes about fifteen "
    "minutes: one producer sets up the first quote, and the rest of the team follows the same "
    "path from there. I would rather walk you through it than write it all out here, because "
    "the useful part is seeing it against your own book of business rather than a generic "
    "example. Nothing about your setup looks unusual, so it should be quick.\n"
    "\n"
    "Would you have time for a short call this week?\n"
    "\n"
    "Best,\n"
    "Dana"
)

# What a hijacked generation looks like: no subject, preamble, no CTA, five
# words. Every shape check fails, which is the point.
HIJACKED_COPY = "Sure, here you go: hi."

# The lead's contact email lands verbatim in the *trusted* region of
# `_build_copy_prompt`, so it is the one field that identifies a prompt without
# the double being handed a lead object it must not hold.
_LEAD_IN_PROMPT = re.compile(r"\((lead_[a-z0-9_]+)@example\.test\)")


def lead_id_in(prompt):
    """Which lead this prompt was built for. Raised rather than asserted: a bare
    ``assert`` vanishes under ``-O``, and a double that silently stopped
    identifying its lead would let every per-lead assertion below check nothing.
    """
    match = _LEAD_IN_PROMPT.search(prompt)
    if match is None:
        raise AssertionError(f"the double could not identify the lead from: {prompt[:200]!r}")
    return match.group(1)


def make_lead(lead_id, **overrides):
    """A scoped lead carrying two events, shaped like tests_compose_lifecycle's.

    The events are load-bearing: they are what the untrusted block of the copy
    prompt renders, so every prompt assertion below is made against a prompt of
    realistic shape rather than one with an empty fenced region.
    """
    defaults = dict(
        id=lead_id,
        agency_name=f"Agency {lead_id}",
        contact_name=f"Contact {lead_id}",
        contact_email=f"{lead_id}@example.test",
        contact_phone="555-0100",
        state="CA",
        num_producers=3,
        years_in_business=5,
        estimated_book_size_usd=1_000_000,
        stage="demo_completed",
        signed_up_date=None,
        quotes_created=0,
        quotes_submitted=0,
        deals_closed=0,
        hubspot_notes="",
    )
    defaults.update(overrides)
    lead = Lead.objects.create(**defaults)
    for event_type in ("login", "quote_created"):
        Event.objects.create(lead=lead, type=event_type, timestamp=timezone.now(), meta={})
    return lead


def seed_catalog():
    """The MUS-32 catalog row stage 04 prices its actuals against. Prices live
    here and nowhere else — `estimate.record_actuals` must read them from this row
    rather than a constant — so a test that seeded no catalog would be asserting
    against numbers the component invented."""
    provider = LLMProvider.objects.create(
        key=PROVIDER,
        label="Test double",
        api_key_url="https://example.test/keys",
        api_key_label="Test key",
    )
    return LLMModel.objects.create(
        provider=provider,
        model_id=MODEL,
        label="Copy model",
        context_window=100_000,
        default_max_tokens=MAX_COPY_TOKENS,
        input_price_per_mtok_usd=INPUT_PRICE_PER_MTOK,
        output_price_per_mtok_usd=OUTPUT_PRICE_PER_MTOK,
    )


def make_run(**overrides):
    """A run parked at `classified` — the status stage 04 is reachable from."""
    from project.app.models import PlannerRun

    defaults = dict(
        scope=SCOPE,
        created_by="tester@example.test",
        status=PlannerRun.STATUS_CLASSIFIED,
    )
    defaults.update(overrides)
    return PlannerRun.objects.create(**defaults)


def make_run_lead(
    run,
    lead,
    *,
    selected=False,
    already_queued=False,
    effective_action=None,
    effective_priority=None,
    suggestion=None,
):
    """One classified row, optionally carrying an accepted suggestion.

    Written straight through the ORM rather than through `classify_run` /
    `accept_suggestion` — those belong to components 4 and 8, and stage 04 must be
    testable against the row *shape* the contract freezes. When
    ``effective_action`` moves the row, `effective_reason` is rebuilt from
    :data:`ACCEPTED_ACTION_REASON` exactly as `decisions.accept_suggestion` must,
    never from ``suggestion["rationale"]`` (which would make the canary a
    tautology).
    """
    from project.app.models import RunLead

    rules_priority = determine_priority(lead)
    rules_action, rules_reason = determine_action(lead)
    action = effective_action or rules_action
    accepted = suggestion is not None
    return RunLead.objects.create(
        run=run,
        lead=lead,
        rules_priority=rules_priority,
        rules_action=rules_action,
        rules_reason=rules_reason,
        rule_trace=explain(lead),
        # The key IS the identity of the recommendation, so it comes from the
        # *effective* action: an accepted action_change changed which one this is.
        dedupe_key=dedupe.dedupe_key(lead.id, action),
        effective_priority=rules_priority if effective_priority is None else effective_priority,
        effective_action=action,
        effective_reason=(
            rules_reason
            if action == rules_action
            else ACCEPTED_ACTION_REASON.format(old=rules_action, new=action)
        ),
        already_queued=already_queued,
        selected=selected,
        suggestion=suggestion or {},
        suggestion_state=(RunLead.SUGGESTION_ACCEPTED if accepted else RunLead.SUGGESTION_NONE),
        suggestion_decided_at=timezone.now() if accepted else None,
        suggestion_decided_by="reviewer@example.test" if accepted else "",
    )


def open_action_for(lead):
    """A pending OutreachAction already holding this lead's dedupe slot. Keyed off
    `determine_action` rather than a hardcoded string, so the fixture cannot drift
    away from the recommendation it is meant to collide with."""
    action_type, reason = determine_action(lead)
    return OutreachAction.objects.create(
        lead=lead,
        priority=determine_priority(lead),
        action_type=action_type,
        reason=reason,
        suggested_copy=WELL_SHAPED_COPY,
        status=OutreachAction.STATUS_PENDING,
        dedupe_key=dedupe.dedupe_key(lead.id, action_type),
    )


class RecordingClient(LLMClient):
    """A real ``LLMClient`` that records every prompt it is handed.

    A genuine subclass rather than a ``Mock``, for the reason tests_planner_async
    gives: a Mock answers every attribute identically, so it could not tell
    ``agenerate`` from ``generate``. The synchronous twin raises, so a stage 04
    that collapsed the bounded pool into a serial loop fails loudly.
    """

    provider_name = PROVIDER

    def __init__(self, *, default_text=WELL_SHAPED_COPY, by_lead=None, fail_for=()):
        super().__init__(model=MODEL, default_max_tokens=MAX_COPY_TOKENS)
        self.default_text = default_text
        self.by_lead = dict(by_lead or {})
        self.fail_for = frozenset(fail_for)
        self.prompts = []
        self.closed = 0

    def generate(self, prompt, max_tokens=None, timeout=None):
        raise AssertionError("stage 04 must take the async path, exactly as the planner does")

    async def agenerate(self, prompt, max_tokens=None, timeout=None):
        self.prompts.append(prompt)
        lead_id = lead_id_in(prompt)
        if lead_id in self.fail_for:
            # Non-retryable on purpose: a 429 would exercise the planner's backoff
            # schedule instead of stage 04's failure bookkeeping.
            raise LLMBadRequestError("the provider rejected this request", provider=PROVIDER)
        return LLMResult(
            text=self.by_lead.get(lead_id, self.default_text),
            provider=PROVIDER,
            model=MODEL,
            input_tokens=INPUT_TOKENS_PER_LEAD,
            output_tokens=OUTPUT_TOKENS_PER_LEAD,
        )

    async def aclose(self):
        self.closed += 1

    def prompted_leads(self):
        return {lead_id_in(prompt) for prompt in self.prompts}

    def prompt_for(self, lead_id):
        for prompt in self.prompts:
            if lead_id_in(prompt) == lead_id:
                return prompt
        return None


def patched_client(client):
    """``services.llm._build_client`` hands back ``client``: the single
    ``lru_cache``d constructor both ``get_llm_client()`` and
    ``build_client(provider)`` funnel through, so the patch holds whichever
    factory stage 04 reaches for and however it spells the import. The same seam
    ``tests_compose_lifecycle`` asserts classify never touches."""
    return mock.patch("project.app.services.llm._build_client", return_value=client)


def persisted_text(action):
    """Every free-text and JSON field of a row, as one searchable blob. Used by
    the canary test: naming the fields individually would let the next column
    added to `OutreachAction` become an unexamined leak channel."""
    return json.dumps(
        {
            "reason": action.reason,
            "suggested_copy": action.suggested_copy,
            "further_action": action.further_action,
            "rule_trace": action.rule_trace,
            "verification": action.verification,
        }
    )


class SelectLeadsTests(TestCase):
    """`select_leads` — the bulk toggle behind the selection table's header
    checkbox, which is the only place forty rows change state in one gesture."""

    @unittest.expectedFailure
    def test_select_leads_toggles_only_the_named_rows_and_reports_the_count(self):
        """Selecting is scoped to the ids named and reports how many rows it set.

        The count is what the FE reconciles its optimistic state against, so it
        must come from the service rather than from the length of the list sent —
        the two differ the moment an id misses. Deselection is the same call with
        ``selected=False``: two code paths for one toggle is two places for the
        count to disagree.
        """
        from project.app.services.compose import generate

        run = make_run()
        rows = {
            lead_id: make_run_lead(run, make_lead(lead_id))
            for lead_id in ("lead_001", "lead_002", "lead_003")
        }

        changed = generate.select_leads(run, ["lead_001", "lead_002"], True)

        self.assertEqual(changed, 2)
        for lead_id, row in rows.items():
            row.refresh_from_db()
            with self.subTest(lead=lead_id):
                self.assertEqual(row.selected, lead_id in ("lead_001", "lead_002"))

        self.assertEqual(generate.select_leads(run, ["lead_001"], False), 1)
        rows["lead_001"].refresh_from_db()
        rows["lead_002"].refresh_from_db()
        self.assertFalse(rows["lead_001"].selected)
        self.assertTrue(rows["lead_002"].selected)  # untouched by the deselect

    @unittest.expectedFailure
    def test_select_leads_ignores_ids_that_do_not_belong_to_the_run(self):
        """An id outside the run is skipped, not an error — and never reaches
        another run's rows.

        A stale tab holding an id from a re-classified scope is the ordinary way
        an unknown id arrives, and 400-ing the batch over one would fail a
        forty-row select. The cross-run half is sharper: a `select_leads` that
        filtered on `lead_id__in=...` and forgot `run` passes every single-run
        test ever written while ticking rows in a completed run.
        """
        from project.app.models import PlannerRun
        from project.app.services.compose import generate

        run = make_run()
        shared = make_lead("lead_001")
        mine = make_run_lead(run, shared)
        # A second run is only legal once it is terminal: NULL sentinel, status in
        # TERMINAL_STATUSES, which is what the check constraint demands.
        other_run = PlannerRun.objects.create(
            scope=SCOPE,
            created_by="earlier@example.test",
            status=PlannerRun.STATUS_COMPLETED,
            active_sentinel=None,
        )
        theirs = make_run_lead(other_run, shared)

        changed = generate.select_leads(run, ["lead_001", "lead_404"], True)

        self.assertEqual(changed, 1)  # the miss is skipped, not counted, not raised
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertTrue(mine.selected)
        self.assertFalse(theirs.selected)  # the other run's row is not this run's business


class GenerateForSelectionTests(TestCase):
    """`generate_for_selection` — the stage that spends money and writes drafts."""

    def generate(self, run, client, *, provider=PROVIDER, model=MODEL):
        from project.app.services.compose import generate as generate_service

        with patched_client(client):
            return generate_service.generate_for_selection(
                run, provider=provider, model=model, actor="tester@example.test"
            )

    @unittest.expectedFailure
    def test_only_selected_rows_are_generated_for_and_each_links_its_row(self):
        """Selection is the authorization, and the link is the receipt.

        Pointing the `RunLead` at the row it produced is the half that is easy to
        skip: it is what lets the table show "drafted" and what lets a second
        `generate` call tell a lead it already drafted from one it has not.
        Without it the only join back is `(trace_run_id, lead)`, which works right
        up until a lead is generated for under two runs. The unselected lead is
        the control: no row and no link, or "selected" means nothing.
        """
        seed_catalog()
        run = make_run()
        chosen = [
            make_run_lead(run, make_lead(lead_id), selected=True)
            for lead_id in ("lead_001", "lead_002")
        ]
        passed_over = make_run_lead(run, make_lead("lead_003"), selected=False)
        client = RecordingClient()

        result = self.generate(run, client)

        self.assertEqual(result["generated"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["skipped"], 0)
        # Exactly the chosen leads were asked about: asserting on rows alone would
        # admit generating for all three and discarding one — twice the bill.
        self.assertEqual(client.prompted_leads(), {"lead_001", "lead_002"})
        self.assertEqual(
            set(OutreachAction.objects.values_list("lead_id", flat=True)), {"lead_001", "lead_002"}
        )
        for row in chosen:
            row.refresh_from_db()
            with self.subTest(lead=row.lead_id):
                self.assertIsNotNone(row.generated_action_id)
                self.assertEqual(row.generated_action.lead_id, row.lead_id)
                self.assertEqual(row.generation_error, "")
        passed_over.refresh_from_db()
        self.assertIsNone(passed_over.generated_action_id)

    @unittest.expectedFailure
    def test_an_already_queued_row_is_skipped_even_when_selected(self):
        """An open recommendation sharing the dedupe key wins over a tick.

        Classify marks the row rather than hiding it — so a lead does not vanish
        from a scope of forty — which means "selected" and "already queued"
        genuinely co-occur and the second has to win, or two drafts for one
        recommendation land in the same finite queue. Skipped is *counted*: "1
        generated" out of two ticked rows and nothing else is indistinguishable
        from a bug.
        """
        seed_catalog()
        run = make_run()
        taken = make_lead("lead_001")
        open_action_for(taken)
        blocked = make_run_lead(run, taken, selected=True, already_queued=True)
        free = make_run_lead(run, make_lead("lead_002"), selected=True)
        client = RecordingClient()

        result = self.generate(run, client)

        self.assertEqual(result["generated"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["failed"], 0)
        # No prompt was built for the blocked lead: the skip sits ahead of the
        # provider call, so it costs nothing rather than a discarded call.
        self.assertEqual(client.prompted_leads(), {"lead_002"})
        # Still exactly the one row that was already there.
        self.assertEqual(OutreachAction.objects.filter(lead_id="lead_001").count(), 1)
        blocked.refresh_from_db()
        free.refresh_from_db()
        self.assertIsNone(blocked.generated_action_id)
        self.assertIsNotNone(free.generated_action_id)

    @unittest.expectedFailure
    def test_a_generated_row_carries_the_runs_stamp_and_both_output_gates(self):
        """A composer draft is indistinguishable from a planner draft.

        That equivalence is the whole reason the composer needs no new verifier:
        `status="pending"`, the run's `trace_run_id`, the schema-v1 rule trace and
        a verification report describing exactly the copy on the row — each read
        by the triage queue, the trace view or the approve gate, so a missing one
        renders wrong or approves unchecked. Two leads deliberately, one clean and
        one hijacked-looking: asserting only the clean one proves the row was
        written, not that the gates *ran*. That also pins the counting rule — a
        flagged draft is a generated draft, and `failed` counts provider failures.

        The status assertion is the one nothing else in the feature makes.
        `completed` is reachable only from `generated`, so a stage 04 that leaves
        the run at `classified` puts `close_run` out of reach end to end — which
        is why `tests_compose_lifecycle` fakes the status with a raw `.update()`.
        """
        from project.app.models import PlannerRun

        seed_catalog()
        run = make_run()
        clean = make_run_lead(run, make_lead("lead_001"), selected=True)
        junk = make_run_lead(run, make_lead("lead_002"), selected=True)
        client = RecordingClient(by_lead={"lead_002": HIJACKED_COPY})

        result = self.generate(run, client)

        self.assertEqual(result["generated"], 2)
        self.assertEqual(result["failed"], 0)
        run.refresh_from_db()
        self.assertEqual(run.status, PlannerRun.STATUS_GENERATED)
        # Minted at generate, not at create, and UUID-shaped because that is what
        # the planner stamps — the trace view joins the two on equal terms.
        self.assertNotEqual(run.trace_run_id, "")
        uuid.UUID(run.trace_run_id)

        clean.refresh_from_db()
        junk.refresh_from_db()
        for row in (clean, junk):
            action = row.generated_action
            with self.subTest(lead=row.lead_id):
                self.assertEqual(action.status, OutreachAction.STATUS_PENDING)
                self.assertEqual(action.trace_run_id, run.trace_run_id)
                self.assertEqual(action.dedupe_key, row.dedupe_key)
                self.assertEqual(action.action_type, row.effective_action)
                self.assertEqual(action.priority, row.effective_priority)
                self.assertEqual(action.reason, row.effective_reason)
                # MUS-42's envelope stored whole, which is what lets MUS-40's
                # RuleTrace component render a composer row with no changes.
                self.assertEqual(action.rule_trace["version"], TRACE_SCHEMA_VERSION)
                self.assertTrue(action.rule_trace["priority"]["signals"])
                # `verification` always describes `effective_copy` (section 9.2);
                # built against other bytes, `can_approve` answers about copy
                # nobody is looking at.
                self.assertEqual(action.verification["version"], verify.VERIFICATION_SCHEMA_VERSION)
                self.assertEqual(action.verification["copy"], action.suggested_copy)

        self.assertEqual(clean.generated_action.suggested_copy, WELL_SHAPED_COPY)
        self.assertFalse(clean.generated_action.needs_human)
        self.assertEqual(clean.generated_action.further_action, "")
        self.assertTrue(clean.generated_action.verification["can_approve"])

        # The hijacked draft is kept for reference and routed to a human with the
        # reason spelled out — the planner's behaviour, through the composer path.
        self.assertEqual(junk.generated_action.suggested_copy, HIJACKED_COPY)
        self.assertTrue(junk.generated_action.needs_human)
        self.assertIn("Shape check failed", junk.generated_action.further_action)
        self.assertFalse(junk.generated_action.verification["can_approve"])

    @unittest.expectedFailure
    def test_one_leads_provider_failure_costs_only_that_lead(self):
        """A dead call is one lead's problem, and the lead stays retryable.

        Stage 04 is the expensive one, so "the run died at lead nine of forty" is
        unacceptable: the eight already paid for must land. The failed lead keeps
        `selected=True` and records `generation_error` so the retry is a click,
        and writes **no** `OutreachAction` — a row with no copy would report a
        provider outage as reviewer work.
        """
        seed_catalog()
        run = make_run()
        survivor = make_run_lead(run, make_lead("lead_001"), selected=True)
        casualty = make_run_lead(run, make_lead("lead_002"), selected=True)
        client = RecordingClient(fail_for={"lead_002"})

        result = self.generate(run, client)

        self.assertEqual(result["generated"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(OutreachAction.objects.count(), 1)

        survivor.refresh_from_db()
        casualty.refresh_from_db()
        self.assertIsNotNone(survivor.generated_action_id)
        self.assertEqual(survivor.generation_error, "")
        self.assertIsNone(casualty.generated_action_id)
        # From the taxonomy rather than hardcoded, so a provider adapter inventing
        # a subclass tomorrow inherits the right label instead of breaking this.
        self.assertEqual(casualty.generation_error, failure_kind(LLMBadRequestError("rejected")))
        self.assertTrue(casualty.selected)  # still ticked: the retry is a click

    @unittest.expectedFailure
    def test_no_model_derived_prose_reaches_the_generator(self):
        """THE fail-closed-gate test. `suggestion["rationale"]` is model prose
        derived from attacker-reachable notes, and it must reach neither a prompt
        nor a row.

        The plan takes position (b) — `services/verify.py` is *not* extended by
        this feature — and the only reason that is defensible is structural: the
        generator's inputs are the same first-party facts the planner has always
        used, plus an integer and an enum a human accepted. Prose is not among
        them. If this test is deleted, position (b) becomes an unsupported claim
        and the fail-closed rate has nothing holding it.

        The row here is an accepted `raise`, so the suggestion genuinely
        influenced the run — `effective_priority` moved and the draft carries it —
        which is exactly what makes the leak plausible enough to be worth pinning.
        Three assertions guard against a vacuous pass: the canary is still on the
        `RunLead` afterwards, a prompt really was built, and that prompt really is
        this lead's.
        """
        seed_catalog()
        run = make_run()
        lead = make_lead("lead_001")
        row = make_run_lead(
            run,
            lead,
            selected=True,
            effective_priority=1,
            suggestion={
                "suggestion": "raise",
                "proposed_priority": 1,
                "rationale": f"The notes say {RATIONALE_CANARY} and this agency is ready now.",
                "evidence": [{"source": "note", "quote": "ready now"}],
            },
        )
        client = RecordingClient()

        result = self.generate(run, client)

        self.assertEqual(result["generated"], 1)
        # Not vacuous: a prompt was built, it was this lead's, and the canary is
        # still sitting on the row the UI renders it from.
        self.assertEqual(len(client.prompts), 1)
        prompt = client.prompt_for("lead_001")
        self.assertIsNotNone(prompt)
        row.refresh_from_db()
        self.assertIn(RATIONALE_CANARY, row.suggestion["rationale"])

        # The commitment itself.
        self.assertNotIn(RATIONALE_CANARY, prompt)
        action = row.generated_action
        self.assertNotIn(RATIONALE_CANARY, persisted_text(action))
        self.assertFalse(OutreachAction.objects.filter(reason__contains=RATIONALE_CANARY).exists())
        # The sanctioned channel still worked: the accepted priority is on the
        # draft. The suggestion changed the run through an integer a human
        # approved, and through nothing else.
        self.assertEqual(action.priority, 1)

    @unittest.expectedFailure
    def test_the_effective_priority_never_reaches_the_copy_prompt(self):
        """Gate commitment 2: `effective_priority` is not in the copy prompt today
        and is not added to it.

        The plan cites this as a reason the verifier needs no extension, and until
        now it was prose. It matters because "tell the model this is a priority 1"
        is the most natural thing anyone would ever add to `_build_copy_prompt`,
        and it would make an accepted, model-*proposed* number a live prompt
        input: a lever anyone who can write notes reaches through stage 03.

        **What makes it fail.** The word "priority" in the prompt in any casing —
        every plausible rendering labels it ("Priority: 3", "P3 lead") — or either
        priority, the accepted one or the rules' own, as a standalone number.
        **What stops it passing vacuously.** A bare digit proves nothing in a
        prompt full of them, so the fixture pushes every number the template
        renders from the lead up to 5..9 (`PROMPT_DIGIT_SAFE`), leaving 1, 2 and 3
        unreachable except by a leak — and the same regex over the same prompt is
        required to *find* `CONTROL_DIGIT`, so a scan that came back silent
        because it was scanning nothing fails on that line.
        """
        seed_catalog()
        run = make_run()
        lead = make_lead("lead_001", **PROMPT_DIGIT_SAFE)
        row = make_run_lead(
            run,
            lead,
            selected=True,
            effective_priority=LOWERED_PRIORITY,
            suggestion={
                "suggestion": "lower",
                "proposed_priority": LOWERED_PRIORITY,
                "proposed_action": "",
                "rationale": "The notes read like a research call, not a buying signal.",
                "evidence": [{"source": "note", "quote": "just looking"}],
            },
        )
        # Fixture guard: the row has to have genuinely moved, or "the effective
        # priority is absent" is a claim about the rules' priority by accident.
        self.assertNotEqual(row.rules_priority, LOWERED_PRIORITY)
        client = RecordingClient()

        result = self.generate(run, client)

        self.assertEqual(result["generated"], 1)
        prompt = client.prompt_for("lead_001")
        self.assertIsNotNone(prompt)
        # Positive control on the scan itself, before anything is concluded from
        # its silence: a bare digit that IS in the prompt is found by this regex.
        self.assertIn(f"{CONTROL_DIGIT} producers", prompt)
        self.assertRegex(prompt, rf"\b{CONTROL_DIGIT}\b")

        # The commitment.
        self.assertNotIn("priority", prompt.casefold())
        self.assertNotRegex(prompt, rf"\b{LOWERED_PRIORITY}\b")
        self.assertNotRegex(prompt, rf"\b{row.rules_priority}\b")
        # And the sanctioned channel still carried it: the accepted integer is on
        # the draft, which is the whole of how a suggestion reaches the queue.
        row.refresh_from_db()
        self.assertEqual(row.generated_action.priority, LOWERED_PRIORITY)

    @unittest.expectedFailure
    def test_an_accepted_action_change_generates_for_the_new_action(self):
        """An accepted `action_change` changes what gets written, not just what
        gets displayed. The suggestion may move exactly one thing into the prompt
        — the action type, a first-party enum — and the copy must be written for
        the *new* action or the acceptance was cosmetic: the operator would
        approve an email pitching the recommendation they rejected. The reason
        travels with it, rebuilt from `ACCEPTED_ACTION_REASON`, and so does the
        dedupe key. The test below pins the other half: the verifier saw it too.
        """
        seed_catalog()
        run = make_run()
        lead = make_lead("lead_001")
        row = make_run_lead(
            run,
            lead,
            selected=True,
            effective_action=CHANGED_ACTION,
            suggestion={
                "suggestion": "action_change",
                "proposed_action": CHANGED_ACTION,
                "rationale": "They are already live; onboarding is not the ask.",
                "evidence": [{"source": "event", "quote": "quote_created"}],
            },
        )
        # Fixture guard: if the rules ever choose CHANGED_ACTION on their own,
        # every assertion below would pass while testing nothing.
        self.assertNotEqual(row.rules_action, CHANGED_ACTION)
        client = RecordingClient()

        self.generate(run, client)

        prompt = client.prompt_for("lead_001")
        # `_build_copy_prompt` renders the action on its own "Planned action:"
        # line, so both directions assert against production strings.
        self.assertIn(f"Planned action: {CHANGED_ACTION}", prompt)
        self.assertNotIn(f"Planned action: {row.rules_action}", prompt)

        row.refresh_from_db()
        action = row.generated_action
        self.assertEqual(action.action_type, CHANGED_ACTION)
        self.assertEqual(action.dedupe_key, dedupe.dedupe_key("lead_001", CHANGED_ACTION))
        self.assertEqual(
            action.reason, ACCEPTED_ACTION_REASON.format(old=row.rules_action, new=CHANGED_ACTION)
        )
        # The rules' verdict is untouched by any of this. It is written once, at
        # classify, and stage 04 is not an exception to that.
        self.assertEqual(row.rules_action, determine_action(lead)[0])

    @unittest.expectedFailure
    def test_an_accepted_action_change_is_verified_against_the_new_action(self):
        """Gate commitment 3: the swapped `action_type` is a first-party enum
        `verify._check_offer` already covers — and it is the *changed* one the
        verifier runs against.

        The plan's reason for not extending the verifier only holds if the report
        on the row was built with the new action type, and `_check_offer` is where
        that difference is observable: the only check whose verdict depends on
        `action_type`, authorizing commercial promises for `power_user_reward` and
        nothing else. So the lead is a genuine power user, the accepted change
        moves it to `nudge_usage`, and the provider returns copy pitching the OLD
        action — volume pricing. Clean under `power_user_reward`; an
        `unauthorized_offer` under `nudge_usage`, a `BLOCKING_KIND` that drops
        `can_approve` on its own.

        **What makes it fail.** Handing the verifier `rules_action` (or the
        suggestion's, or a default): the claim is absent, `can_approve` is true,
        and a reviewer can send an email promising a discount nobody authorized.
        **What stops it passing vacuously.** The claim carries `expected` — the
        action type the check ran under — so the assertion names the new action
        rather than counting claims, and the control below runs the real
        `verify_copy` over the same copy under the old action and requires an
        empty list, so the flag can only have come from the swap.
        """
        seed_catalog()
        run = make_run()
        lead = make_lead("lead_001", **POWER_USER)
        row = make_run_lead(
            run,
            lead,
            selected=True,
            effective_action=CHANGED_ACTION,
            suggestion={
                "suggestion": "action_change",
                "proposed_priority": None,
                "proposed_action": CHANGED_ACTION,
                "rationale": "They are quoting steadily; the reward pitch is premature.",
                "evidence": [{"source": "event", "quote": "quote_created"}],
            },
        )
        # Fixture guards. The rules must have said `power_user_reward` — the one
        # action that authorizes an offer — or the two verdicts never differ.
        self.assertEqual(row.rules_action, actions.POWER_USER_REWARD)
        self.assertNotEqual(row.rules_action, CHANGED_ACTION)
        # The control: this exact copy is clean under the action the rules chose.
        self.assertEqual(verify.verify_copy(lead, REWARD_COPY, row.rules_action), [])
        client = RecordingClient(by_lead={"lead_001": REWARD_COPY})

        self.generate(run, client)

        row.refresh_from_db()
        action = row.generated_action
        self.assertEqual(action.action_type, CHANGED_ACTION)
        offers = [c for c in action.verification["claims"] if c["kind"] == "unauthorized_offer"]
        self.assertEqual(len(offers), 1)
        # `expected` is the action type `_check_offer` was handed. This is the
        # assertion: the verifier ran against the accepted action, not the rules'.
        self.assertEqual(offers[0]["field"], "action_type")
        self.assertEqual(offers[0]["expected"], CHANGED_ACTION)
        self.assertIs(offers[0]["verified"], False)
        # ...and the fail-closed consequences the reviewer actually meets.
        self.assertFalse(action.verification["can_approve"])
        self.assertTrue(action.needs_human)
        self.assertIn("Grounding check failed", action.further_action)

    @unittest.expectedFailure
    def test_the_run_records_what_generation_actually_cost(self):
        """The actual is measured, never the estimate re-quoted.

        Writing the estimate into `generate_cost_actual_usd` would make every
        estimate look perfect forever, and the gap between the two is the only
        thing that makes estimates worth showing. The arithmetic is the assertion
        — two leads at 1200 in / 300 out, priced from the catalog row at $3 and
        $15 per million, is 2 x (0.0036 + 0.0045) = $0.0162 — in `Decimal`
        throughout, with the provider and model recorded beside it.
        """
        seed_catalog()
        run = make_run()
        for lead_id in ("lead_001", "lead_002"):
            make_run_lead(run, make_lead(lead_id), selected=True)
        client = RecordingClient()

        result = self.generate(run, client)

        expected = USD_PER_LEAD * 2
        self.assertIsInstance(result["actual_usd"], Decimal)
        self.assertEqual(result["actual_usd"], expected)
        run.refresh_from_db()
        self.assertEqual(run.generate_cost_actual_usd, expected)
        self.assertEqual(run.generate_provider, PROVIDER)
        self.assertEqual(run.generate_model, MODEL)

    @unittest.expectedFailure
    def test_generating_with_nothing_selected_is_a_defined_no_op(self):
        """Zero selected rows is an answer, not a crash and not a spend.

        Reachable, not contrived: the operator clears the selection, then hits
        Generate because a stale tab still shows the button enabled. It returns
        the same shape as any other run so the FE has one branch, constructs no
        client (an empty run would otherwise fail loudly on a misconfigured
        provider it had no reason to touch), and leaves the run where it was —
        `completed` is reachable only from `generated`, so a no-op that promoted
        the run would make an empty run closeable.
        """
        from project.app.models import PlannerRun
        from project.app.services.compose import generate

        seed_catalog()
        run = make_run()
        make_run_lead(run, make_lead("lead_001"), selected=False)

        with mock.patch("project.app.services.llm._build_client") as build_client:
            result = generate.generate_for_selection(
                run, provider=PROVIDER, model=MODEL, actor="tester@example.test"
            )
            build_client.assert_not_called()

            # Positive control: `assert_not_called` above is satisfied for free by
            # a patch that bound nothing, so prove this one is live first.
            llm.get_llm_client()
            self.assertEqual(build_client.call_count, 1)

        # The full dict, so the key set is pinned too: the FE reads all four and
        # a missing one renders as `undefined` rather than as an error.
        self.assertEqual(
            result, {"generated": 0, "failed": 0, "skipped": 0, "actual_usd": Decimal("0")}
        )
        self.assertEqual(OutreachAction.objects.count(), 0)
        run.refresh_from_db()
        self.assertEqual(run.status, PlannerRun.STATUS_CLASSIFIED)


class GenerateEndpointTests(AuthenticatedAPITestCase):
    """The HTTP surface. Paths are written out rather than reversed: the contract
    freezes the URLs, not the view names, so a test that reversed a name would
    pass against a route the frontend cannot reach."""

    def _post(self, path, payload=None):
        return self.client.post(
            path, payload if payload is not None else {}, content_type="application/json"
        )

    @unittest.expectedFailure
    def test_select_endpoint_bulk_toggles_and_returns_the_count(self):
        """`{lead_ids, selected}` in, `{"selected": N}` out. One request for the
        whole batch, because the alternative is forty requests from a header
        checkbox, and the count comes back because it is the only way the FE
        learns that one of the ids it sent was stale."""
        run = make_run()
        for lead_id in ("lead_001", "lead_002", "lead_003"):
            make_run_lead(run, make_lead(lead_id))

        resp = self._post(
            f"/api/runs/{run.pk}/select/",
            {"lead_ids": ["lead_001", "lead_002"], "selected": True},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["selected"], 2)
        self.assertEqual(
            set(run.run_leads.filter(selected=True).values_list("lead_id", flat=True)),
            {"lead_001", "lead_002"},
        )

    @unittest.expectedFailure
    def test_generate_endpoint_returns_the_counts_and_the_drafts_reach_the_queue(self):
        """Driven end to end, because the queue is where a draft becomes real.

        `GET /api/queue/` is the reviewer's entire surface and `status="pending"`
        is the only thing that puts a row on it, so asserting the row exists in
        the database would pass against a draft written as `snoozed` while the
        operator watched the composer produce nothing. The response carries the
        counts the run summary renders, so the FE needs no second round trip.
        """
        seed_catalog()
        run = make_run()
        make_run_lead(run, make_lead("lead_001"), selected=True)
        make_run_lead(run, make_lead("lead_002"), selected=False)
        client = RecordingClient()

        with patched_client(client):
            resp = self._post(
                f"/api/runs/{run.pk}/generate/", {"provider": PROVIDER, "model": MODEL}
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["generated"], 1)
        self.assertEqual(resp.data["failed"], 0)
        self.assertEqual(resp.data["skipped"], 0)
        # Money crosses the wire as a JSON *string*: `REST_FRAMEWORK` does not set
        # COERCE_DECIMAL_TO_STRING, so DRF's default applies and the FE parses with
        # `Number(...)` at the edge. A float here would be a silent precision
        # change, which is why the type is pinned and not just the value.
        self.assertIsInstance(resp.json()["actual_usd"], str)
        self.assertEqual(resp.json()["actual_usd"], str(USD_PER_LEAD))

        queue = self.client.get("/api/queue/")
        self.assertEqual(queue.status_code, 200)
        queued = {item["lead"]["id"]: item for item in queue.data["items"]}
        self.assertIn("lead_001", queued)
        self.assertNotIn("lead_002", queued)  # never selected, never drafted
        self.assertEqual(queued["lead_001"]["suggested_copy"], WELL_SHAPED_COPY)

    @unittest.expectedFailure
    def test_select_and_generate_are_401_when_anonymous(self):
        """401 exactly, not 403 — MUS-38's route guard redirects on 401 and does
        nothing on 403. Worth its own test on this endpoint above all others:
        `generate` is the one route where an unauthenticated POST spends money."""
        run = make_run()
        make_run_lead(run, make_lead("lead_001"), selected=True)
        self.client.logout()

        for path in (f"/api/runs/{run.pk}/select/", f"/api/runs/{run.pk}/generate/"):
            with self.subTest(path=path):
                self.assertEqual(self._post(path).status_code, 401)
        # Nothing was generated on the way to being refused.
        self.assertEqual(OutreachAction.objects.count(), 0)
