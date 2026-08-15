"""Component artifact: loop (MUS-29).

Pins the bounded tool-calling loop and its event-sourced checkpoint: a gapless
persisted step trace per lead, resume-after-final replay with zero re-billed
provider calls, budget exhaustion forcing one toolless final call, epoch-CAS
claim semantics (fresh runs claimable, terminal runs refused, a lost claim
surfaces as ``AgentClaimLost`` with nothing written), and the ``execute_tool``
span that carries content hashes but never payloads.
"""

import asyncio

from django.test import TestCase, override_settings

from project.app import models as app_models  # AgentLeadRun/AgentStep: lazy until Task 3 lands
from project.app.models import Lead
from project.app.services.agent import loop as agent_loop
from project.app.services.agent import state, tools
from project.app.services.llm.base import FINISH_STOP, FINISH_TOOL_CALLS, LLMClient, LLMResult
from project.app.services.llm.chat_types import ToolCallRequest
from project.app.services.llm.errors import LLMAuthError, LLMRateLimitError
from project.app.services.llm.runtime import get_planner_runtime
from project.app.services.telemetry import genai, semconv

from .tests_telemetry_support import RecordingTestCase, spans_named


def _tool_result(name="get_lead_history"):
    return LLMResult(
        text="",
        provider="fake",
        model="m",
        finish_reason=FINISH_TOOL_CALLS,
        raw_finish_reason="tool_use",
        tool_calls=(ToolCallRequest(id="c1", name=name, arguments={}),),
    )


def _final(text="Subject: Hi\n\nBody."):
    return LLMResult(
        text=text,
        provider="fake",
        model="m",
        finish_reason=FINISH_STOP,
        raw_finish_reason="end_turn",
    )


class _FakeChatClient(LLMClient):
    provider_name = "fake"

    def __init__(self, script):
        super().__init__(model="fake-model", default_max_tokens=1)
        self.script, self.chat_calls = list(script), []

    def generate(self, prompt, max_tokens=None, timeout=None):
        raise AssertionError("agent loop must not take the blocking path")

    async def agenerate_chat(self, messages, *, tools=(), max_tokens=None, timeout=None):
        self.chat_calls.append({"messages": list(messages), "tools": tuple(tools)})
        return self.script.pop(0)


class _RefusingChatClient(LLMClient):
    """Fails every call with one error, and counts how often it was asked."""

    provider_name = "groq"

    def __init__(self, error):
        super().__init__(model="fake-model", default_max_tokens=1)
        self.error, self.chat_calls = error, []

    def generate(self, prompt, max_tokens=None, timeout=None):
        raise AssertionError("agent loop must not take the blocking path")

    async def agenerate_chat(self, messages, *, tools=(), max_tokens=None, timeout=None):
        self.chat_calls.append({"messages": list(messages), "tools": tuple(tools)})
        raise self.error


class RunAgentLeadTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.lead = Lead.objects.create(
            id="lead_lp1",
            agency_name="A",
            contact_name="C",
            contact_email="c@a.com",
            contact_phone="1",
            state="TX",
            num_producers=3,
            years_in_business=4,
            estimated_book_size_usd=1,
            stage="active_trial",
        )

    def _run(self, script, prior=()):
        pks = state.create_lead_runs("run-loop-1", [self.lead.id])
        client = _FakeChatClient(script)
        ctx = tools.build_tool_context(self.lead, (), (), (), None)
        outcome = asyncio.run(
            agent_loop.run_agent_lead(
                prompt="PROMPT",
                lead_run_pk=pks[self.lead.id],
                prior_steps=prior or state.load_prior_steps(pks[self.lead.id]),
                context=ctx,
                client=client,
                runtime=get_planner_runtime(),
                checkpoint=state.Checkpoint(),
            )
        )
        return outcome, client, pks[self.lead.id]

    def test_tool_call_then_final_persists_a_gapless_trace(self):
        self.assertTrue(hasattr(app_models, "AgentStep"))  # red at skeleton
        outcome, client, pk = self._run([_tool_result(), _final()])
        self.assertEqual(outcome.draft_text, "Subject: Hi\n\nBody.")
        kinds = list(
            app_models.AgentStep.objects.filter(lead_run_id=pk)
            .order_by("seq")
            .values_list("kind", "seq")
        )
        self.assertEqual(
            kinds, [("llm_call", 1), ("tool_result", 2), ("llm_call", 3), ("final", 4)]
        )
        self.assertEqual(app_models.AgentLeadRun.objects.get(pk=pk).status, "done")

    def test_resume_after_final_replays_without_any_provider_call(self):
        self.assertTrue(hasattr(app_models, "AgentLeadRun"))  # red at skeleton
        _, _, pk = self._run([_tool_result(), _final()])
        app_models.AgentLeadRun.objects.filter(pk=pk).update(status="drafting")  # pre-crash claim
        client = _FakeChatClient([])
        outcome = asyncio.run(
            agent_loop.run_agent_lead(
                prompt="PROMPT",
                lead_run_pk=pk,
                prior_steps=state.load_prior_steps(pk),
                context=tools.build_tool_context(self.lead, (), (), (), None),
                client=client,
                runtime=get_planner_runtime(),
                checkpoint=state.Checkpoint(),
            )
        )
        self.assertEqual(outcome.draft_text, "Subject: Hi\n\nBody.")
        self.assertEqual(client.chat_calls, [])  # zero re-billed calls

    def test_step_budget_forces_a_toolless_final_call(self):
        self.assertTrue(hasattr(app_models, "AgentStep"))  # red at skeleton
        script = [_tool_result()] * 5 + [_final("Subject: F\n\nDone.")]
        outcome, client, pk = self._run(script)
        self.assertEqual(outcome.draft_text, "Subject: F\n\nDone.")
        self.assertEqual(len(client.chat_calls), 6)  # OUTREACH_AGENT_MAX_STEPS
        self.assertEqual(client.chat_calls[-1]["tools"], ())  # forced final: no tools offered


@override_settings(
    OUTREACH_MAX_ATTEMPTS=3, OUTREACH_INITIAL_BACKOFF_S=0.0, OUTREACH_MAX_BACKOFF_S=0.0
)
class AgentFailureAccountingTests(TestCase):
    """What the reviewer's failure sentence is built from.

    ``_describe_failure`` renders "gave up after {attempts} attempt(s) over
    {elapsed}s", and those two numbers are the whole difference between a row a
    reviewer reads as noise and one they read as work. The single-shot path
    counts them in ``agenerate_copy``; the agent path has to count them here,
    because ``AgentOutcome`` is the only thing that crosses back to phase 3.
    """

    @classmethod
    def setUpTestData(cls):
        cls.lead = Lead.objects.create(
            id="lead_lp3",
            agency_name="C",
            contact_name="C",
            contact_email="c@c.com",
            contact_phone="1",
            state="TX",
            num_producers=3,
            years_in_business=4,
            estimated_book_size_usd=1,
            stage="active_trial",
        )

    def _run(self, client):
        pks = state.create_lead_runs("run-loop-acct", [self.lead.id])
        return asyncio.run(
            agent_loop.run_agent_lead(
                prompt="PROMPT",
                lead_run_pk=pks[self.lead.id],
                prior_steps=(),
                context=tools.build_tool_context(self.lead, (), (), (), None),
                client=client,
                runtime=get_planner_runtime(),
                checkpoint=state.Checkpoint(),
            )
        )

    def test_a_retried_failure_reports_every_attempt_it_spent(self):
        client = _RefusingChatClient(LLMRateLimitError("slow down", provider="groq"))
        outcome = self._run(client)

        self.assertIsInstance(outcome.error, LLMRateLimitError)
        # The budget was spent, so the sentence must say so. Reporting 0 here
        # tells a reviewer the run never tried.
        self.assertEqual(len(client.chat_calls), 3)  # OUTREACH_MAX_ATTEMPTS
        self.assertEqual(outcome.attempts, 3)
        self.assertGreater(outcome.elapsed_s, 0.0)

    def test_a_failure_that_was_not_retried_reports_its_single_attempt(self):
        """One attempt is still one, not zero: "gave up after 0 attempts" and
        "gave up after 1 attempt" describe different bugs to whoever reads it."""
        client = _RefusingChatClient(LLMAuthError("bad key", provider="groq"))
        outcome = self._run(client)

        self.assertIsInstance(outcome.error, LLMAuthError)
        self.assertEqual(len(client.chat_calls), 1)
        self.assertEqual(outcome.attempts, 1)


class CheckpointClaimTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.lead = Lead.objects.create(
            id="lead_lp2",
            agency_name="B",
            contact_name="C",
            contact_email="c@b.com",
            contact_phone="1",
            state="TX",
            num_producers=3,
            years_in_business=4,
            estimated_book_size_usd=1,
            stage="active_trial",
        )

    def test_claim_succeeds_on_fresh_runs_and_refuses_terminal_ones(self):
        """Instance-symmetric: either Checkpoint claims a fresh run, and neither
        can claim a run that already reached a terminal status — cover both
        orders. (The same-epoch two-CAS single-winner race is pinned at the ORM
        level in tests_agent_loop_agent_models.)"""
        self.assertTrue(hasattr(app_models, "AgentLeadRun"))  # red at skeleton
        for order in ("ab", "ba"):
            with self.subTest(order=order):
                run = app_models.AgentLeadRun.objects.create(
                    lead=self.lead, trace_run_id=f"run-claim-{order}"
                )
                a, b = state.Checkpoint(), state.Checkpoint()
                first, second = (a, b) if order == "ab" else (b, a)
                self.assertTrue(asyncio.run(first.claim(run.pk)))
                app_models.AgentLeadRun.objects.filter(pk=run.pk).update(status="done")
                self.assertFalse(asyncio.run(second.claim(run.pk)))

    def test_lost_claim_surfaces_as_agent_claim_lost_with_no_provider_calls(self):
        self.assertTrue(hasattr(app_models, "AgentLeadRun"))  # red at skeleton
        run = app_models.AgentLeadRun.objects.create(
            lead=self.lead, trace_run_id="run-claim-lost", status="done"
        )
        client = _FakeChatClient([])
        outcome = asyncio.run(
            agent_loop.run_agent_lead(
                prompt="PROMPT",
                lead_run_pk=run.pk,
                prior_steps=(),
                context=tools.build_tool_context(self.lead, (), (), (), None),
                client=client,
                runtime=get_planner_runtime(),
                checkpoint=state.Checkpoint(),
            )
        )
        self.assertIsInstance(outcome.error, state.AgentClaimLost)
        self.assertEqual(client.chat_calls, [])
        self.assertEqual(app_models.AgentStep.objects.filter(lead_run=run).count(), 0)


class ToolSpanTests(RecordingTestCase):
    def test_execute_tool_span_carries_hashes_and_no_payload(self):
        self.assertTrue(hasattr(genai, "tool_span"))  # red at skeleton
        self.assertTrue(hasattr(semconv, "OPERATION_EXECUTE_TOOL"))
        args_hash = genai.sha256_of('{"limit": 5}')
        result_hash = genai.sha256_of("SECRET-RESULT-TEXT")
        with genai.tool_span("get_lead_history", args_sha256=args_hash) as set_result_sha256:
            set_result_sha256(result_hash)
        (span,) = spans_named("execute_tool get_lead_history")
        self.assertEqual(span.attributes[semconv.GEN_AI_OPERATION_NAME], "execute_tool")
        self.assertEqual(span.attributes[semconv.GEN_AI_TOOL_NAME], "get_lead_history")
        self.assertIn(args_hash, span.attributes.values())
        self.assertIn(result_hash, span.attributes.values())
        for value in span.attributes.values():
            self.assertNotIn("SECRET-RESULT-TEXT", str(value))  # refs only, never payloads
