"""Component artifact: loop (MUS-29).

Planted red by the skeleton PR — every test carries ``@unittest.expectedFailure``
and opens with a capability assertion (new models resolved lazily off
``app_models``; ``genai.tool_span`` probed with hasattr), so a stripped-marker
failure is an AssertionError or NotImplementedError. The loop component PR
strips the markers and takes this module to zero.
"""

import asyncio
import unittest

from django.test import TestCase

from project.app import models as app_models  # AgentLeadRun/AgentStep: lazy until Task 3 lands
from project.app.models import Lead
from project.app.services.agent import loop as agent_loop
from project.app.services.agent import state, tools
from project.app.services.llm.base import FINISH_STOP, FINISH_TOOL_CALLS, LLMClient, LLMResult
from project.app.services.llm.chat_types import ToolCallRequest
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

    @unittest.expectedFailure
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

    @unittest.expectedFailure
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

    @unittest.expectedFailure
    def test_step_budget_forces_a_toolless_final_call(self):
        self.assertTrue(hasattr(app_models, "AgentStep"))  # red at skeleton
        script = [_tool_result()] * 5 + [_final("Subject: F\n\nDone.")]
        outcome, client, pk = self._run(script)
        self.assertEqual(outcome.draft_text, "Subject: F\n\nDone.")
        self.assertEqual(len(client.chat_calls), 6)  # OUTREACH_AGENT_MAX_STEPS
        self.assertEqual(client.chat_calls[-1]["tools"], ())  # forced final: no tools offered


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

    @unittest.expectedFailure
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

    @unittest.expectedFailure
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
    @unittest.expectedFailure
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
