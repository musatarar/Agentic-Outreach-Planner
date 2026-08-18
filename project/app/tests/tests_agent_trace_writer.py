"""Component artifact: the agent loop's ProviderTrace writer (MUS-72).

Pins the call grain — one audit row per ``llm_call`` step, minted in the step's
own transaction — and the default-off content capture. Helpers duplicate
tests_agent_loop_loop.py's because component modules do not import each other.
"""

import asyncio
from datetime import datetime
from datetime import timezone as dt_timezone
from unittest import mock

from django.test import TestCase, override_settings

from evals import redteam_payloads
from project.app.models import (
    AgentLeadRun,
    AgentStep,
    Event,
    Lead,
    LLMConfiguration,
    LLMModel,
    LLMProvider,
    OutreachAction,
    ProviderTrace,
    ProviderTraceContent,
)
from project.app.services import outreach, sanitize
from project.app.services.agent import loop as agent_loop
from project.app.services.agent import state, tools
from project.app.services.llm.base import FINISH_STOP, FINISH_TOOL_CALLS, LLMClient, LLMResult
from project.app.services.llm.chat_types import ToolCallRequest
from project.app.services.llm.runtime import get_planner_runtime

TRACE_RUN_ID = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"


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


def _seed_configuration():
    """An active LLM selection that disagrees with what the fake client returns."""
    provider = LLMProvider.objects.create(
        key="claude",
        label="Anthropic Claude",
        api_key_url="https://console.anthropic.com/settings/keys",
        api_key_label="Anthropic API key",
    )
    model = LLMModel.objects.create(
        provider=provider,
        model_id="configured-not-used",
        label="Configured",
        context_window=100_000,
        input_price_per_mtok_usd="1.00",
        output_price_per_mtok_usd="1.00",
    )
    LLMConfiguration.load(provider=provider, model=model, max_tokens=500)


class _FakeChatClient(LLMClient):
    provider_name = "fake"

    def __init__(self, script, before_call=None):
        super().__init__(model="fake-model", default_max_tokens=1)
        self.script, self.chat_calls = list(script), []
        self._before_call = before_call

    def generate(self, prompt, max_tokens=None, timeout=None):
        raise AssertionError("agent loop must not take the blocking path")

    async def agenerate_chat(self, messages, *, tools=(), max_tokens=None, timeout=None):
        if self._before_call is not None:
            self._before_call()
        self.chat_calls.append({"messages": list(messages), "tools": tuple(tools)})
        return self.script.pop(0)


class _TraceWriterCase(TestCase):
    """One lead, one run, the loop driven straight through ``asyncio``."""

    @classmethod
    def setUpTestData(cls):
        cls.lead = Lead.objects.create(
            id="lead_tw1",
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

    def _run(self, script, *, before_call=None):
        pks = state.create_lead_runs(TRACE_RUN_ID, [self.lead.id])
        pk = pks[self.lead.id]
        client = _FakeChatClient(script, before_call=before_call)
        outcome = asyncio.run(
            agent_loop.run_agent_lead(
                prompt="PROMPT",
                lead_run_pk=pk,
                prior_steps=state.load_prior_steps(pk),
                context=tools.build_tool_context(self.lead, (), (), (), None),
                client=client,
                runtime=get_planner_runtime(),
                checkpoint=state.Checkpoint(trace_run_id=TRACE_RUN_ID),
            )
        )
        return outcome, client, pk


class TraceMintTests(_TraceWriterCase):
    def test_one_llm_call_step_mints_one_linked_trace_snapshotting_the_result(self):
        """Provider/model come from the ``LLMResult``, not from ``LLMConfiguration``."""
        _seed_configuration()
        _, _, pk = self._run([_final()])

        trace = ProviderTrace.objects.get()
        self.assertEqual((trace.provider, trace.model_id), ("fake", "m"))
        self.assertEqual(trace.trace_run_id, TRACE_RUN_ID)
        step = AgentStep.objects.get(lead_run_id=pk, kind=state.KIND_LLM_CALL)
        self.assertEqual(step.provider_trace_id, trace.pk)

    def test_a_two_step_run_mints_two_traces_sharing_one_trace_run_id(self):
        """The call grain, end to end: two provider calls, two audit rows, one run."""
        _, client, pk = self._run([_tool_result(), _final()])

        self.assertEqual(len(client.chat_calls), 2)
        traces = list(ProviderTrace.objects.order_by("id"))
        self.assertEqual(len(traces), 2)
        self.assertEqual({trace.trace_run_id for trace in traces}, {TRACE_RUN_ID})
        linked = list(
            AgentStep.objects.filter(lead_run_id=pk, kind=state.KIND_LLM_CALL)
            .order_by("seq")
            .values_list("provider_trace_id", flat=True)
        )
        self.assertEqual(linked, [trace.pk for trace in traces])

    def test_tool_result_and_final_steps_keep_provider_trace_null(self):
        _, _, pk = self._run([_tool_result(), _final()])

        unlinked = AgentStep.objects.filter(
            lead_run_id=pk, kind__in=(state.KIND_TOOL_RESULT, state.KIND_FINAL)
        )
        self.assertEqual(unlinked.count(), 2)
        self.assertEqual([step.provider_trace_id for step in unlinked], [None, None])

    def test_resume_from_a_persisted_final_mints_zero_traces(self):
        """Zero provider calls must mean zero audit rows."""
        _, _, pk = self._run([_tool_result(), _final()])
        minted = ProviderTrace.objects.count()
        AgentLeadRun.objects.filter(pk=pk).update(status="drafting")  # pre-crash claim

        client = _FakeChatClient([])
        outcome = asyncio.run(
            agent_loop.run_agent_lead(
                prompt="PROMPT",
                lead_run_pk=pk,
                prior_steps=state.load_prior_steps(pk),
                context=tools.build_tool_context(self.lead, (), (), (), None),
                client=client,
                runtime=get_planner_runtime(),
                checkpoint=state.Checkpoint(trace_run_id=TRACE_RUN_ID),
            )
        )

        self.assertEqual(outcome.draft_text, "Subject: Hi\n\nBody.")
        self.assertEqual(client.chat_calls, [])
        self.assertEqual(ProviderTrace.objects.count(), minted)

    def test_a_lost_claim_leaves_no_trace_row(self):
        """The mint shares the step's transaction, so a rolled-back append writes no audit row."""
        stolen = {"done": False}

        def steal_the_claim():
            if not stolen["done"]:
                stolen["done"] = True
                AgentLeadRun.objects.filter(trace_run_id=TRACE_RUN_ID).update(claimed_by="rival")

        outcome, client, pk = self._run([_final()], before_call=steal_the_claim)

        self.assertIsInstance(outcome.error, state.AgentClaimLost)
        self.assertEqual(len(client.chat_calls), 1)  # the call happened; the write did not
        self.assertEqual(AgentStep.objects.filter(lead_run_id=pk).count(), 0)
        self.assertEqual(ProviderTrace.objects.count(), 0)


class TraceContentTests(_TraceWriterCase):
    def test_content_is_not_written_while_the_flag_is_off(self):
        """Request/response bytes are lead PII at rest: capturing them is an operator decision."""
        self.assertFalse(get_planner_runtime().trace_content_enabled)
        self._run([_final()])

        self.assertEqual(ProviderTrace.objects.count(), 1)
        self.assertEqual(ProviderTraceContent.objects.count(), 0)

    @override_settings(OUTREACH_TRACE_CONTENT_ENABLED=True)
    def test_the_flag_stores_the_bytes_actually_sent_and_the_answer(self):
        self._run([_final("Subject: Hi\n\nBody.")])

        content = ProviderTraceContent.objects.get()
        self.assertIn("PROMPT", content.request)
        self.assertIn(state.AGENT_ADDENDUM, content.request)
        self.assertEqual(content.response, "Subject: Hi\n\nBody.")
        self.assertEqual(content.reasoning, "")  # its own ticket

    @override_settings(OUTREACH_TRACE_CONTENT_ENABLED=True)
    def test_a_planted_injection_is_stored_in_its_already_neutralized_form(self):
        """Stored post-sanitization, post-``wrap_untrusted`` — never re-fed, never raw."""
        payload = redteam_payloads.payloads_for(redteam_payloads.DIRECT_OVERRIDE)[0]
        Event.objects.create(
            lead=self.lead,
            type="call_logged",
            timestamp=datetime(2026, 6, 1, tzinfo=dt_timezone.utc),
            meta={"notes": payload.injected_text},
        )
        self._run([_tool_result(), _final()])

        # The second call is the one that carried the tool result back.
        second = ProviderTraceContent.objects.order_by("trace_id").last()
        self.assertIn(sanitize.UNTRUSTED_OPEN, second.request)
        self.assertNotIn(payload.injected_text, second.request)


@override_settings(OUTREACH_AGENT_ENABLED=True, COPY_VERIFY_LEVEL="off")
class TraceRunIdWiringTests(TestCase):
    """The run id reaches the mint through the Checkpoint, from phase 2."""

    def test_a_planner_run_stamps_its_own_run_id_on_every_trace(self):
        Lead.objects.create(
            id="lead_000",
            agency_name="SYNTH-000",
            contact_name="Contact",
            contact_email="contact@example.com",
            contact_phone="555-0100",
            state="CA",
            num_producers=3,
            years_in_business=8,
            estimated_book_size_usd=1_000_000,
            stage="demo_completed",
            signed_up_date=None,
        )
        client = _FakeChatClient([_final()])
        with mock.patch.object(outreach, "get_llm_client", return_value=client):
            outreach.plan_outreach()

        run_id = AgentLeadRun.objects.values_list("trace_run_id", flat=True).get()
        self.assertTrue(run_id)
        self.assertEqual(OutreachAction.objects.filter(trace_run_id=run_id).count(), 1)
        self.assertEqual(
            list(ProviderTrace.objects.values_list("trace_run_id", flat=True)), [run_id]
        )
