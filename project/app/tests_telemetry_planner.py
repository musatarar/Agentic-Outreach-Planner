"""Tests for the run and lead spans, and for what they must never carry
(MUS-25, 25-c).

Two halves, and the second is the one that matters.

The first asserts the trace is *right*: the run span is named
``invoke_agent outreach_planner`` (the spec's template is
``invoke_agent {gen_ai.agent.name}``, not a bare ``invoke_agent``), lead spans
are its children, the six-state verifier outcome is reachable in all six states,
and the output reference resolves to the row the lead produced.

The second is :class:`SpanContentLeakTests`, written in the spirit of
``tests_redteam.py``: plant a canary string in the one field an outsider
controls — the lead's HubSpot notes — and in the generated copy, run a real
plan, then walk **every attribute value on every exported span** asserting the
canary appears nowhere. It is a red-team-shaped assertion rather than a
whitelist of keys because the failure it guards against is somebody adding a
well-meaning attribute later, not somebody deliberately emitting a prompt.
"""

import hashlib
from unittest import mock

from django.test import TestCase
from opentelemetry import trace

from project.app.models import Lead, OutreachAction
from project.app.services import outreach
from project.app.services.llm import LLMClient, LLMResult
from project.app.services.llm import runtime as llm_runtime
from project.app.services.outreach import plan_outreach
from project.app.services.telemetry import genai, semconv

from .tests_telemetry_support import RecordingMixin, spans_named

RUN_SPAN_NAME = "invoke_agent outreach_planner"

# Well-formed and grounded: passes both output gates, so a test that wants a
# clean lead gets one without having to assert around a verifier complaint.
GOOD_COPY = (
    "Subject: Let's finish setting up\n\n"
    "Hi Priya,\n\n"
    "Summit Risk Advisors has a strong $5M book, and I'd hate to see that "
    "momentum stall before your account is live. Getting fully set up takes "
    "about fifteen minutes, and once it's done your producers can start "
    "protecting premiums right away. I know the demo covered a lot, so I'm "
    "happy to walk your team through the final steps personally and answer "
    "anything that came up afterward. Would you have time for a quick call "
    "this week to wrap up onboarding?\n\n"
    "Best,\nThe Locked In team"
)

# Well-SHAPED but contradicts the record (4 deals closed, not 47), so the
# grounding gate fires alone. Built from GOOD_COPY on purpose: the two differ by
# one sentence, which is what isolates the grounding gate from the shape gate
# -- a short ungrounded email would trip both and the test would pass without
# distinguishing them.
UNGROUNDED_COPY = GOOD_COPY.replace("Hi Priya,", "Hi Priya,\n\nCongrats on your 47 closed deals!")

# No subject line, no CTA, obvious preamble -> shape gate fires. Grounded,
# though, so it isolates the shape gate from the grounding one.
MALFORMED_COPY = "Sure! Here is the email you asked me to write for this lead."


class _PlannerSpanTestCase(RecordingMixin, TestCase):
    """A real ``plan_outreach()`` run over one lead, with spans recorded.

    ``RecordingMixin`` rather than a copy of its two methods: the "clear the
    shared exporter in setUp" contract is inherited here exactly as it is in
    the database-free span tests.
    """

    def make_lead(self, **kwargs):
        # demo_completed + no signup -> complete_onboarding, which is
        # date-independent, so classification does not drift with the wall clock.
        defaults = dict(
            id="lead_ground",
            agency_name="Summit Risk Advisors",
            contact_name="Priya Nair",
            contact_email="priya.nair@summitrisk.com",
            contact_phone="555-0000",
            state="CO",
            num_producers=4,
            years_in_business=12,
            estimated_book_size_usd=5_000_000,
            stage="demo_completed",
            signed_up_date=None,
            deals_closed=4,
        )
        defaults.update(kwargs)
        return Lead.objects.create(**defaults)

    def plan(self, copy=GOOD_COPY, side_effect=None):
        # The planner's phase 3 goes through the async twin (MUS-26), so that
        # is the seam to stub — a plain ``return_value`` would hand the
        # planner an unawaitable. A raised ``LLMError`` lands in
        # ``_agenerate_for``'s ``except Exception`` and is reported as a
        # provider failure with its taxonomy class intact, which is exactly
        # what the error-path tests here assert on.
        target = "project.app.services.outreach.agenerate_copy"

        async def stub(*args, **kwargs):
            if side_effect is not None:
                raise side_effect
            return copy

        with mock.patch(target, stub):
            return plan_outreach()

    def run_span(self):
        (span,) = spans_named(RUN_SPAN_NAME)
        return span

    def lead_span(self):
        (span,) = spans_named(genai.LEAD_SPAN_NAME)
        return span


class RunSpanTests(_PlannerSpanTestCase):
    def test_the_run_span_is_named_after_the_agent(self):
        """The template is ``invoke_agent {gen_ai.agent.name}``. A bare
        ``invoke_agent`` is the easiest detail in the convention to get wrong,
        because it looks complete."""
        self.make_lead()
        self.plan()
        self.assertEqual(self.run_span().name, "invoke_agent outreach_planner")

    def test_the_run_span_is_internal_not_client(self):
        """Ours is the *internal* variant of the operation -- the agent runs in
        this process. CLIENT would claim we called an agent over the network."""
        self.make_lead()
        self.plan()
        self.assertEqual(self.run_span().kind, trace.SpanKind.INTERNAL)

    def test_the_run_span_attributes(self):
        self.make_lead()
        self.plan()
        attributes = self.run_span().attributes

        self.assertEqual(attributes[semconv.GEN_AI_OPERATION_NAME], "invoke_agent")
        self.assertEqual(attributes[semconv.GEN_AI_AGENT_NAME], "outreach_planner")
        self.assertEqual(attributes[semconv.LEAD_COUNT], 1)
        self.assertEqual(attributes[semconv.NEEDS_HUMAN_COUNT], 0)
        self.assertEqual(
            attributes[semconv.CONCURRENCY_MAX_IN_FLIGHT],
            llm_runtime.get_planner_runtime().max_in_flight,
        )
        self.assertEqual(attributes[semconv.VERIFY_LEVEL], "standard")
        self.assertEqual(attributes[semconv.OPENINFERENCE_SPAN_KIND], "AGENT")
        self.assertTrue(attributes[semconv.RUN_ID])

    def test_needs_human_is_counted_not_inferred(self):
        self.make_lead()
        self.plan(copy=UNGROUNDED_COPY)
        self.assertEqual(self.run_span().attributes[semconv.NEEDS_HUMAN_COUNT], 1)

    def test_the_run_id_is_the_one_stamped_on_every_row(self):
        """The join in both directions: a row found in the database names the
        run that produced it, and a run span names the rows it produced."""
        self.make_lead()
        self.plan()
        run_id = self.run_span().attributes[semconv.RUN_ID]
        self.assertEqual(OutreachAction.objects.get().trace_run_id, run_id)

    def test_a_run_with_no_provider_usage_reports_no_token_totals(self):
        """With ``agenerate_copy`` stubbed out no provider call happens, so no
        usage is ever reported and the run must say nothing rather than claim a
        confident zero. (The populated case runs the real phase 3 -- see
        ``PlannerProviderSpanTests``.)"""
        self.make_lead()
        self.plan()
        for key in (semconv.GEN_AI_USAGE_INPUT_TOKENS, semconv.GEN_AI_USAGE_OUTPUT_TOKENS):
            self.assertNotIn(key, self.run_span().attributes)

    def test_usage_reported_during_a_run_is_aggregated_onto_it(self):
        """Two provider calls under one run sum, rather than the last one
        winning. Reported through the ambient recorder, which is what lets
        MUS-26's ``asyncio.gather`` aggregate with no extra plumbing."""
        with genai.run_span(verify_level="standard", max_in_flight=8) as run:
            run.add_usage(910, 140)
            run.add_usage(1200, 260)
            run.finish(lead_count=2, needs_human_count=0)

        attributes = self.run_span().attributes
        self.assertEqual(attributes[semconv.GEN_AI_USAGE_INPUT_TOKENS], 2110)
        self.assertEqual(attributes[semconv.GEN_AI_USAGE_OUTPUT_TOKENS], 400)
        self.assertEqual(attributes[semconv.LLM_TOKEN_COUNT_TOTAL], 2510)

    def test_a_provider_call_inside_a_run_reports_its_usage_upward(self):
        """The wiring the aggregation actually relies on: the provider-call
        scope finds the run through a ``ContextVar`` and folds its counts in.
        ``asyncio.gather`` copies the current context into each task, so the
        binding survives MUS-26's concurrency with no change at either end."""
        from project.app.services.llm.base import LLMResult

        call = genai.ProviderCall(provider="groq", model="m")
        result = LLMResult(
            text="x", provider="groq", model="m", input_tokens=910, output_tokens=140
        )
        with genai.run_span(verify_level="standard", max_in_flight=1) as run:
            with genai.provider_call_scope(call)(0) as record:
                record(result)
            run.finish(lead_count=1, needs_human_count=0)

        self.assertEqual(self.run_span().attributes[semconv.GEN_AI_USAGE_INPUT_TOKENS], 910)

    def test_a_provider_that_reported_no_usage_contributes_nothing(self):
        """Not zero -- nothing. A run in which no provider reported usage must
        emit no usage attributes at all, rather than a confident total of 0."""
        from project.app.services.llm.base import LLMResult

        call = genai.ProviderCall(provider="groq", model="m")
        with genai.run_span(verify_level="standard", max_in_flight=1) as run:
            with genai.provider_call_scope(call)(0) as record:
                record(LLMResult(text="x", provider="groq", model="m"))
            run.finish(lead_count=1, needs_human_count=0)

        self.assertNotIn(semconv.GEN_AI_USAGE_INPUT_TOKENS, self.run_span().attributes)

    def test_finishing_a_run_twice_is_a_no_op(self):
        """Symmetric with ``LeadSpan.finish``. One call site today, but an
        asymmetry between two ``finish`` methods is an invitation to assume the
        wrong one is safe."""
        with genai.run_span(verify_level="standard", max_in_flight=1) as run:
            run.finish(lead_count=7, needs_human_count=1)
            run.finish(lead_count=99, needs_human_count=99)

        self.assertEqual(self.run_span().attributes[semconv.LEAD_COUNT], 7)

    def test_a_failed_run_records_the_error_type(self):
        with self.assertRaises(ValueError):
            with genai.run_span(verify_level="standard", max_in_flight=1):
                raise ValueError("boom")

        span = self.run_span()
        self.assertEqual(span.attributes[semconv.ERROR_TYPE], "ValueError")
        self.assertEqual(span.status.status_code, trace.StatusCode.ERROR)


class LeadSpanTests(_PlannerSpanTestCase):
    def test_lead_spans_are_children_of_the_run_span(self):
        self.make_lead()
        self.plan()
        self.assertEqual(
            self.lead_span().parent.span_id, self.run_span().get_span_context().span_id
        )

    def test_the_lead_span_carries_no_gen_ai_attributes(self):
        """``plan_lead`` is neither a model call nor an agent invocation. The
        conventions define nothing for it, and inventing keys in a namespace
        somebody else owns is how you collide with a future release that means
        something different by them."""
        self.make_lead()
        self.plan()
        gen_ai_keys = [k for k in self.lead_span().attributes if k.startswith("gen_ai.")]
        self.assertEqual(gen_ai_keys, [])

    def test_the_lead_span_attributes(self):
        self.make_lead()
        self.plan()
        span = self.lead_span()

        self.assertEqual(span.kind, trace.SpanKind.INTERNAL)
        self.assertEqual(span.attributes[semconv.LEAD_ID], "lead_ground")
        self.assertEqual(span.attributes[semconv.ACTION_TYPE], "complete_onboarding")
        self.assertEqual(span.attributes[semconv.ACTION_PRIORITY], 1)
        self.assertFalse(span.attributes[semconv.NEEDS_HUMAN])
        self.assertEqual(span.attributes[semconv.VERIFY_OUTCOME], semconv.VERIFY_PASS)
        self.assertEqual(span.attributes[semconv.VERIFY_VIOLATION_COUNT], 0)
        self.assertEqual(span.attributes[semconv.SHAPE_PROBLEM_COUNT], 0)
        self.assertEqual(span.attributes[semconv.OPENINFERENCE_SPAN_KIND], "CHAIN")

    def test_a_lead_span_closes_before_the_run_does(self):
        """The whole reason a lead span is started and ended by hand rather than
        held in a ``with`` for the life of the run. At concurrency 8 over 200
        leads, holding them open would give the lead processed at t=0 a
        45-second span and destroy the per-lead latency signal outright."""
        self.make_lead()
        self.plan()
        self.assertLessEqual(self.lead_span().end_time, self.run_span().end_time)

    def test_an_unfinished_lead_span_is_still_ended_when_the_run_escapes(self):
        """An unended span is never exported, so a lead caught by an escaping
        exception would simply vanish -- the worst possible failure mode for the
        thing you are looking at the trace to explain."""
        with self.assertRaises(ValueError):
            with genai.run_span(verify_level="standard", max_in_flight=1) as run:
                run.start_lead(lead_id="lead_007", action_type="nudge_usage", priority=2)
                raise ValueError("boom")

        span = self.lead_span()
        self.assertEqual(span.attributes[semconv.VERIFY_OUTCOME], semconv.VERIFY_NOT_GENERATED)
        self.assertEqual(span.attributes[semconv.FAILURE_KIND], "ValueError")
        self.assertEqual(span.status.status_code, trace.StatusCode.ERROR)

    def test_one_span_failing_to_end_does_not_take_the_others_with_it(self):
        """Unguarded, the teardown loop would stop at the first failure -- so
        the remaining leads would *also* never be exported, and the raise would
        replace the planner's real exception with a telemetry one. Both are
        exactly what the run owning its lead spans exists to prevent."""
        real_abandon = genai.LeadSpan.abandon
        calls = []

        def flaky_abandon(self, exc=None):
            calls.append(self)
            if len(calls) == 1:
                raise RuntimeError("a telemetry fault, not the planner's")
            real_abandon(self, exc)

        # Patched on the class, not the instance: LeadSpan uses __slots__, so
        # there is nowhere on an instance to hang a replacement -- which is
        # itself the point of the slots.
        with mock.patch.object(genai.LeadSpan, "abandon", flaky_abandon):
            with self.assertRaises(ValueError) as caught:
                with genai.run_span(verify_level="standard", max_in_flight=1) as run:
                    run.start_lead(lead_id="lead_001", action_type="nudge_usage", priority=2)
                    run.start_lead(lead_id="lead_002", action_type="nudge_usage", priority=2)
                    raise ValueError("the planner's actual failure")

        self.assertEqual(len(calls), 2, "the loop stopped at the first failure")

        # The planner's exception survives -- not demoted to __context__ behind
        # a telemetry one.
        self.assertEqual(str(caught.exception), "the planner's actual failure")
        # And the sibling span still reached the exporter.
        self.assertEqual(len(spans_named(genai.LEAD_SPAN_NAME)), 1)

    def test_finishing_twice_is_a_no_op(self):
        with genai.run_span(verify_level="standard", max_in_flight=1) as run:
            lead = run.start_lead(lead_id="lead_007", action_type="nudge_usage", priority=2)
            lead.finish(needs_human=False, outcome=semconv.VERIFY_PASS)
            lead.finish(needs_human=True, outcome=semconv.VERIFY_BOTH_FAILED)

        span = self.lead_span()
        self.assertFalse(span.attributes[semconv.NEEDS_HUMAN])
        self.assertEqual(span.attributes[semconv.VERIFY_OUTCOME], semconv.VERIFY_PASS)

    def test_attempts_are_counted_from_the_provider_spans(self):
        """Reported through the ambient lead recorder rather than passed down,
        so MUS-26's per-task provider calls count against the right lead with no
        extra plumbing at the call site."""
        call = genai.ProviderCall(provider="groq", model="m")
        with genai.run_span(verify_level="standard", max_in_flight=1) as run:
            lead = run.start_lead(lead_id="lead_007", action_type="nudge_usage", priority=2)
            with lead.active():
                scope = genai.provider_call_scope(call)
                for attempt in range(3):
                    with scope(attempt):
                        pass
            lead.finish(needs_human=False, outcome=semconv.VERIFY_PASS)

        self.assertEqual(self.lead_span().attributes[semconv.LLM_ATTEMPTS], 3)

    def test_provider_spans_are_children_of_the_lead_span(self):
        call = genai.ProviderCall(provider="groq", model="m")
        with genai.run_span(verify_level="standard", max_in_flight=1) as run:
            lead = run.start_lead(lead_id="lead_007", action_type="nudge_usage", priority=2)
            with lead.active():
                with genai.provider_call_scope(call)(0):
                    pass
            lead.finish(needs_human=False, outcome=semconv.VERIFY_PASS)

        (provider_span,) = spans_named("chat m")
        self.assertEqual(provider_span.parent.span_id, self.lead_span().get_span_context().span_id)


class _SpanReportingClient(LLMClient):
    """The narrowest real client the planner will drive end to end."""

    provider_name = "groq"

    def __init__(self, text=GOOD_COPY):
        super().__init__(model="span-model", default_max_tokens=500)
        self.text = text

    def generate(self, prompt, max_tokens=None, timeout=None):
        raise AssertionError("the planner must not take the blocking path")

    async def agenerate(self, prompt, max_tokens=None, timeout=None):
        return LLMResult(
            text=self.text,
            provider=self.provider_name,
            model=self.model,
            input_tokens=910,
            output_tokens=140,
        )


class PlannerProviderSpanTests(_PlannerSpanTestCase):
    """``plan_outreach()`` with nothing stubbed between it and the client.

    The regression these pin down: every piece of the chat-span machinery can
    pass its own unit tests while ``agenerate_copy`` forgets to hook
    ``provider_call_scope`` into the retry helper's ``attempt_scope`` seam --
    in which case a real run traces lead spans and *no provider spans at all*,
    and nothing above notices. Only a run through the genuine phase 3 can.
    """

    def plan_with_real_phase_3(self):
        with mock.patch(
            "project.app.services.outreach.get_llm_client",
            return_value=_SpanReportingClient(),
        ):
            return plan_outreach()

    def test_a_run_produces_a_chat_span_under_the_lead_span(self):
        self.make_lead()
        self.plan_with_real_phase_3()

        (chat_span,) = spans_named("chat span-model")
        self.assertEqual(chat_span.parent.span_id, self.lead_span().get_span_context().span_id)
        # 1-based on the span, deliberately -- "attempt 0" reads as "no attempt".
        self.assertEqual(chat_span.attributes[semconv.LLM_ATTEMPT], 1)
        self.assertEqual(self.lead_span().attributes[semconv.LLM_ATTEMPTS], 1)

    def test_provider_usage_reaches_the_run_span(self):
        self.make_lead()
        self.plan_with_real_phase_3()

        attributes = self.run_span().attributes
        self.assertEqual(attributes[semconv.GEN_AI_USAGE_INPUT_TOKENS], 910)
        self.assertEqual(attributes[semconv.GEN_AI_USAGE_OUTPUT_TOKENS], 140)


class VerifyOutcomeTests(_PlannerSpanTestCase):
    """All six states of ``outreach.verify.outcome``, and the pair that matters.

    ``skipped`` versus ``not_generated`` is the distinction that earns its keep:
    the first is a decision (no automated pattern matched, so copy was never
    requested), the second is a failure (copy was requested and none arrived).
    Collapsing them would make a provider outage look like a quiet week.
    """

    def test_the_enum_is_exactly_six_states(self):
        self.assertEqual(len(semconv.VERIFY_OUTCOMES), 6)
        self.assertEqual(len(set(semconv.VERIFY_OUTCOMES)), 6)

    def test_pass(self):
        self.make_lead()
        self.plan()
        self.assertEqual(self.lead_span().attributes[semconv.VERIFY_OUTCOME], semconv.VERIFY_PASS)

    def test_grounding_failed(self):
        self.make_lead()
        self.plan(copy=UNGROUNDED_COPY)
        span = self.lead_span()
        self.assertEqual(span.attributes[semconv.VERIFY_OUTCOME], semconv.VERIFY_GROUNDING_FAILED)
        self.assertGreater(span.attributes[semconv.VERIFY_VIOLATION_COUNT], 0)
        self.assertEqual(span.attributes[semconv.SHAPE_PROBLEM_COUNT], 0)

    def test_shape_failed(self):
        self.make_lead()
        self.plan(copy=MALFORMED_COPY)
        span = self.lead_span()
        self.assertEqual(span.attributes[semconv.VERIFY_OUTCOME], semconv.VERIFY_SHAPE_FAILED)
        self.assertGreater(span.attributes[semconv.SHAPE_PROBLEM_COUNT], 0)
        self.assertEqual(span.attributes[semconv.VERIFY_VIOLATION_COUNT], 0)

    def test_both_failed(self):
        self.make_lead()
        self.plan(copy="Sure! Congrats on your 47 closed deals.")
        span = self.lead_span()
        self.assertEqual(span.attributes[semconv.VERIFY_OUTCOME], semconv.VERIFY_BOTH_FAILED)
        self.assertGreater(span.attributes[semconv.SHAPE_PROBLEM_COUNT], 0)
        self.assertGreater(span.attributes[semconv.VERIFY_VIOLATION_COUNT], 0)

    def test_skipped(self):
        """No automated pattern matched -- a decision, and copy was never
        requested. `generate_copy` is not patched because it is never called."""
        self.make_lead(id="lead_unknown", stage="active_trial", signed_up_date=None)
        plan_outreach()
        span = self.lead_span()
        self.assertEqual(span.attributes[semconv.VERIFY_OUTCOME], semconv.VERIFY_SKIPPED)
        self.assertTrue(span.attributes[semconv.NEEDS_HUMAN])

    def test_not_generated(self):
        """Copy was requested and the provider call failed -- a failure, and it
        must not be reported as the same thing as ``skipped``."""
        from project.app.services.llm.errors import LLMRateLimitError

        self.make_lead()
        self.plan(side_effect=LLMRateLimitError("throttled", provider="groq"))
        span = self.lead_span()
        self.assertEqual(span.attributes[semconv.VERIFY_OUTCOME], semconv.VERIFY_NOT_GENERATED)
        self.assertEqual(span.attributes[semconv.FAILURE_KIND], "LLMRateLimitError")
        self.assertEqual(span.status.status_code, trace.StatusCode.ERROR)
        self.assertTrue(span.attributes[semconv.NEEDS_HUMAN])


class ContentReferenceTests(_PlannerSpanTestCase):
    """The trace records *which* record was read and written, and a digest of
    each -- never the text."""

    def test_the_input_reference_names_the_lead_and_digests_the_prompt(self):
        self.make_lead()
        self.plan()
        attributes = self.lead_span().attributes

        self.assertEqual(attributes[semconv.INPUT_REF], "lead:lead_ground")
        digest = attributes[semconv.INPUT_SHA256]
        self.assertEqual(len(digest), 64)
        self.assertNotEqual(digest, genai.sha256_of(""))

    def test_the_output_reference_resolves_to_the_row_it_named(self):
        """The point of ``trace_run_id``: the reference is knowable *before* the
        row exists, so the lead span closes on time, and it is still resolvable
        afterwards."""
        self.make_lead()
        self.plan()
        ref = self.lead_span().attributes[semconv.OUTPUT_REF]
        run_id, _, lead_id = ref.removeprefix("outreach_action:").partition(":")

        row = OutreachAction.objects.get(trace_run_id=run_id, lead_id=lead_id)
        self.assertEqual(row.suggested_copy, GOOD_COPY)

    def test_the_output_digest_matches_the_copy_that_was_stored(self):
        self.make_lead()
        self.plan()
        expected = hashlib.sha256(GOOD_COPY.encode("utf-8")).hexdigest()
        self.assertEqual(self.lead_span().attributes[semconv.OUTPUT_SHA256], expected)

    def test_a_lead_with_no_copy_still_gets_an_output_reference(self):
        """A row IS written for a skipped lead, and it is exactly the row an
        operator staring at that lead wants to open. The ref is a pointer; only
        the digest is a claim about content, so only the digest is absent."""
        self.make_lead(id="lead_unknown", stage="active_trial", signed_up_date=None)
        plan_outreach()
        attributes = self.lead_span().attributes

        ref = attributes[semconv.OUTPUT_REF]
        self.assertNotIn(semconv.OUTPUT_SHA256, attributes)
        run_id, _, lead_id = ref.removeprefix("outreach_action:").partition(":")
        self.assertTrue(
            OutreachAction.objects.filter(trace_run_id=run_id, lead_id=lead_id).exists()
        )

    def test_the_output_reference_is_unique_in_the_schema_not_only_in_practice(self):
        """``output_ref``'s docstring promises ``.get()`` resolves to one row.
        A promise a reader relies on should be enforced by the schema rather
        than by an invariant several modules away."""
        from django.db import IntegrityError

        self.make_lead()
        self.plan()
        row = OutreachAction.objects.get()

        with self.assertRaises(IntegrityError):
            OutreachAction.objects.create(
                lead=row.lead,
                priority=row.priority,
                action_type=row.action_type,
                reason=row.reason,
                trace_run_id=row.trace_run_id,
            )

    def test_a_run_id_too_long_for_the_column_is_refused_up_front(self):
        """Otherwise every span in the run closes green and phase 5 then dies on
        Postgres with `value too long` -- a failure several screens from its
        cause."""
        with self.assertRaises(ValueError):
            with genai.run_span(verify_level="standard", max_in_flight=1, run_id="x" * 37):
                pass


class SpanContentLeakTests(_PlannerSpanTestCase):
    """The PII test. Red-team shaped, in the spirit of ``tests_redteam.py``.

    A canary is planted in the one field an outsider controls -- the lead's
    HubSpot notes, the same surface MUS-23 hardens -- and in the generated copy.
    A real run follows. Then **every attribute value on every exported span** is
    walked, plus every span name, status description and event, asserting the
    canary appears nowhere.

    Deliberately a search rather than a whitelist of allowed keys. The failure
    this guards against is not somebody deciding to emit a prompt; it is
    somebody adding a helpful-looking attribute in six months without
    remembering that the prompt embeds a lead's notes.
    """

    NOTES_CANARY = "CANARY-NOTES-4f21c8-do-not-export"
    COPY_CANARY = "CANARY-COPY-9b07ae-do-not-export"

    # Every free-text field on a lead, each with its own canary. Restricting
    # the search to `hubspot_notes` would leave a future `outreach.lead.name`
    # or `outreach.contact` attribute sailing through all of these -- and this
    # suite is the acceptance evidence for the PII claim, so it has to cover
    # the class of leak rather than the one instance of it.
    FIELD_CANARIES = {
        "hubspot_notes": f"Internal note: {NOTES_CANARY}",
        "contact_name": "CANARY-NAME-2b81d4",
        "agency_name": "CANARY-AGENCY-77e0aa",
        "contact_email": "canary-email-5c3f19@example.com",
    }

    def _copy_with_canary(self):
        return GOOD_COPY.replace("Hi Priya,", f"Hi Priya, {self.COPY_CANARY}")

    def _plan_with_canaries(self):
        self.make_lead(**self.FIELD_CANARIES)
        self.plan(copy=self._copy_with_canary())

    def _every_recorded_string(self):
        """Every string a backend would receive, flattened.

        Names, attribute keys AND values, status descriptions, span links,
        resource attributes and event attributes -- because a leak that arrived
        via a span name, a link or the process resource would be exactly as bad
        as one via a span attribute, and checking only span attributes is how
        that gets missed.
        """
        strings = []

        def add(value):
            if isinstance(value, (list, tuple)):
                strings.extend(str(v) for v in value)
            else:
                strings.append(str(value))

        def add_attributes(attributes):
            for key, value in (attributes or {}).items():
                strings.append(str(key))
                add(value)

        for span in self.exporter.get_finished_spans():
            strings.append(span.name)
            if span.status.description:
                strings.append(span.status.description)
            add_attributes(span.attributes)
            add_attributes(getattr(span.resource, "attributes", None))
            for link in span.links or ():
                add_attributes(link.attributes)
            for event in span.events:
                strings.append(event.name)
                add_attributes(event.attributes)
        return strings

    def test_the_canaries_really_are_in_the_run(self):
        """Guards the guard. If a canary never reached the prompt or the row,
        the leak tests below would pass by doing nothing -- the failure mode
        every negative test has and the one nobody notices.

        Note it asserts the notes canary reached the **prompt**, not merely the
        database. If ``_build_untrusted_block`` stopped including
        ``hubspot_notes``, a database-only guard would still pass while
        ``test_no_span_carries_the_lead_notes`` became vacuous.
        """
        self._plan_with_canaries()

        lead = Lead.objects.get()
        prompt = outreach._build_copy_prompt(lead, "complete_onboarding", "why now")
        for value in self.FIELD_CANARIES.values():
            self.assertIn(value.split()[-1], prompt, f"{value} never reached the prompt")

        self.assertIn(self.COPY_CANARY, OutreachAction.objects.get().suggested_copy)
        self.assertTrue(self.exporter.get_finished_spans())

    def test_no_span_carries_any_free_text_lead_field(self):
        self._plan_with_canaries()
        recorded = self._every_recorded_string()
        for field, planted in self.FIELD_CANARIES.items():
            canary = planted.split()[-1]
            for value in recorded:
                self.assertNotIn(canary, value, f"{field} leaked via {value!r}")

    def test_no_span_carries_the_generated_copy(self):
        self._plan_with_canaries()
        for value in self._every_recorded_string():
            self.assertNotIn(self.COPY_CANARY, value)

    def test_the_walk_covers_a_provider_call_span_too(self):
        """The coverage claim, asserted rather than assumed.

        The planner's phase 3 still goes through the synchronous ``complete()``,
        so a real run produces only run and lead spans -- and the leak tests
        above would silently be checking two of the three span kinds. This
        drives ``provider_call_scope`` under a lead span with the canary in the
        provider's error message, which is the exact text the ``chat`` span's
        status description is at risk of carrying.
        """
        from project.app.services.llm.errors import LLMBadRequestError

        call = genai.ProviderCall(provider="groq", model="m")
        with genai.run_span(verify_level="standard", max_in_flight=1) as run:
            lead = run.start_lead(lead_id="lead_007", action_type="nudge_usage", priority=2)
            with lead.active():
                with self.assertRaises(LLMBadRequestError), genai.provider_call_scope(call)(0):
                    raise LLMBadRequestError(f"rejected: {self.NOTES_CANARY}", provider="groq")
            lead.finish(needs_human=True, outcome=semconv.VERIFY_NOT_GENERATED)

        names = {span.name for span in self.exporter.get_finished_spans()}
        self.assertIn("chat m", names)
        for value in self._every_recorded_string():
            self.assertNotIn(self.NOTES_CANARY, value)

    def test_the_openinference_content_carriers_are_absent_by_name(self):
        """Asserted by name as well as by value. ``llm.input_messages`` and
        ``llm.output_messages`` are OpenInference's prompt and completion
        fields, and this project dual-emits OpenInference attributes -- so they
        are the one pair a well-meaning "make Phoenix show more" change would
        reach for first."""
        self._plan_with_canaries()
        for span in self.exporter.get_finished_spans():
            for key in semconv.FORBIDDEN_CONTENT_KEYS:
                self.assertNotIn(key, span.attributes or {}, f"{span.name} emitted {key}")

    def test_a_provider_error_message_does_not_reach_the_trace(self):
        """A provider's error string is text we did not write, produced in
        response to a prompt built from lead data -- and some providers echo the
        request back in a 4xx body."""
        from project.app.services.llm.errors import LLMBadRequestError

        self.make_lead(**self.FIELD_CANARIES)
        self.plan(
            side_effect=LLMBadRequestError(f"rejected input: {self.NOTES_CANARY}", provider="groq")
        )

        # The reviewer still sees it -- via further_action, which is the app's
        # own database and not a third-party trace backend.
        self.assertIn(self.NOTES_CANARY, OutreachAction.objects.get().further_action)
        for value in self._every_recorded_string():
            self.assertNotIn(self.NOTES_CANARY, value)

    def test_the_digest_is_the_only_thing_derived_from_content(self):
        """Positive counterpart to the searches above: a digest of the copy IS
        present, which is what makes "was this the text?" answerable without the
        text."""
        self._plan_with_canaries()
        expected = hashlib.sha256(self._copy_with_canary().encode("utf-8")).hexdigest()
        self.assertIn(expected, self._every_recorded_string())
