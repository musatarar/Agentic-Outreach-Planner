"""Adapter tool-call parsing: the shapes no existing test constructs (MUS-66) --
blank arguments, parallel calls, unreadable entries, and the tool-result fold."""

import asyncio
from unittest import mock

from django.test import TestCase

from project.app import models as app_models
from project.app.models import Lead
from project.app.services.agent import loop as agent_loop
from project.app.services.agent import state, tools
from project.app.services.llm import claude as claude_mod
from project.app.services.llm import openai_compatible as oa_mod
from project.app.services.llm.base import FINISH_TOOL_CALLS, LLMClient, LLMResult
from project.app.services.llm.chat_types import Message, ToolCallRequest, ToolSpec
from project.app.services.llm.errors import LLMEmptyCompletionError, LLMMalformedResponseError
from project.app.services.llm.runtime import get_planner_runtime

HISTORY_TOOL = ToolSpec(
    name="get_lead_history",
    description="d",
    parameters={"type": "object", "properties": {}},
)


class _Obj:
    """Attribute-style stand-in for an SDK model (the Claude adapter reads both)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _usage():
    return _Obj(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=None,
        cache_creation_input_tokens=None,
    )


def _oa_body(tool_calls, *, content=None, finish_reason="tool_calls"):
    """A chat-completions body carrying ``tool_calls`` verbatim."""
    return {
        "choices": [
            {
                "message": {"content": content, "tool_calls": tool_calls},
                "finish_reason": finish_reason,
            }
        ],
        "model": "m",
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }


def _oa_entry(call_id="call_1", name="get_lead_history", arguments="{}"):
    """One ``tool_calls`` entry; pass ``arguments=None`` to omit the key."""
    function = {"name": name}
    if arguments is not None:
        function["arguments"] = arguments
    if name is None:
        del function["name"]
    entry = {"id": call_id, "type": "function", "function": function}
    if call_id is None:
        del entry["id"]
    return entry


class OpenAIBlankArgumentsTests(TestCase):
    """A zero-argument call is a call, however the server spells it."""

    def _client(self):
        client = oa_mod.OpenAICompatibleClient.__new__(oa_mod.OpenAICompatibleClient)
        client.model = "m"
        client.provider_name = "groq"
        return client

    def _one_call(self, arguments):
        body = _oa_body([_oa_entry(arguments=arguments)])
        return self._client()._build_result(body, 0.1).tool_calls

    def test_empty_string_arguments_are_an_empty_object(self):
        calls = self._one_call("")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "get_lead_history")
        self.assertEqual(dict(calls[0].arguments), {})

    def test_whitespace_only_arguments_are_an_empty_object(self):
        self.assertEqual(dict(self._one_call("  \n\t ")[0].arguments), {})

    def test_absent_arguments_key_is_an_empty_object(self):
        self.assertEqual(dict(self._one_call(None)[0].arguments), {})

    def test_json_null_arguments_are_an_empty_object(self):
        self.assertEqual(dict(self._one_call("null")[0].arguments), {})

    def test_real_arguments_still_parse(self):
        self.assertEqual(dict(self._one_call('{"limit": 5}')[0].arguments), {"limit": 5})


class OpenAIMultiCallTests(TestCase):
    """Parallel calls in one response parse, in order."""

    def _client(self):
        client = oa_mod.OpenAICompatibleClient.__new__(oa_mod.OpenAICompatibleClient)
        client.model = "m"
        client.provider_name = "groq"
        return client

    def test_parallel_calls_all_survive_in_order(self):
        body = _oa_body(
            [
                _oa_entry(call_id="call_1", name="get_lead_history", arguments=""),
                _oa_entry(call_id="call_2", name="get_product_details", arguments="{}"),
                _oa_entry(call_id="call_3", name="check_ae_calendar", arguments='{"days": 7}'),
            ]
        )
        calls = self._client()._build_result(body, 0.1).tool_calls
        self.assertEqual([c.id for c in calls], ["call_1", "call_2", "call_3"])
        self.assertEqual(
            [c.name for c in calls],
            ["get_lead_history", "get_product_details", "check_ae_calendar"],
        )
        self.assertEqual([dict(c.arguments) for c in calls], [{}, {}, {"days": 7}])

    def test_tool_call_turn_carrying_text_keeps_both(self):
        body = _oa_body(
            [_oa_entry()],
            content="Let me check the lead history first.",
        )
        result = self._client()._build_result(body, 0.1)
        self.assertEqual(result.text, "Let me check the lead history first.")
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.finish_reason, FINISH_TOOL_CALLS)


class OpenAIDroppedEntryTests(TestCase):
    """An unreadable entry must be an error, not a silent omission."""

    def _client(self):
        client = oa_mod.OpenAICompatibleClient.__new__(oa_mod.OpenAICompatibleClient)
        client.model = "m"
        client.provider_name = "groq"
        return client

    def _assert_raises_structural(self, body):
        with self.assertRaises(LLMMalformedResponseError) as caught:
            self._client()._build_result(body, 0.1)
        # Structural breakage stays non-retryable: a re-read won't parse either.
        self.assertNotIsInstance(caught.exception, LLMEmptyCompletionError)
        self.assertFalse(caught.exception.retryable)
        return caught.exception

    def test_entry_without_an_id_raises(self):
        self._assert_raises_structural(_oa_body([_oa_entry(call_id=None)]))

    def test_entry_without_a_name_raises(self):
        self._assert_raises_structural(_oa_body([_oa_entry(name=None)]))

    def test_entry_whose_arguments_are_not_an_object_raises(self):
        self._assert_raises_structural(_oa_body([_oa_entry(arguments="[1, 2]")]))

    def test_one_unreadable_entry_among_good_ones_raises(self):
        """One bad entry fails the batch rather than executing the rest."""
        body = _oa_body(
            [
                _oa_entry(call_id="call_1"),
                _oa_entry(call_id=None, name="get_product_details"),
                _oa_entry(call_id="call_3", name="check_ae_calendar"),
            ]
        )
        self._assert_raises_structural(body)


class ClaudeToolUseBlockTests(TestCase):
    """The same parsing contract on the Anthropic side."""

    def _client_returning(self, response):
        async def fake_create(**kwargs):
            return response

        patcher = mock.patch.object(claude_mod.anthropic, "AsyncAnthropic")
        cls_ = patcher.start()
        self.addCleanup(patcher.stop)
        cls_.return_value.messages.create = fake_create
        cls_.return_value.api_key = "k"
        cls_.return_value.auth_token = None
        return claude_mod.ClaudeClient(api_key="k")

    def _result_for(self, blocks):
        client = self._client_returning(
            _Obj(content=blocks, stop_reason="tool_use", model="claude-sonnet-4-6", usage=_usage())
        )
        return asyncio.run(
            client.agenerate_chat([Message(role="user", content="hi")], tools=(HISTORY_TOOL,))
        )

    def _assert_raises_structural(self, blocks):
        with self.assertRaises(LLMMalformedResponseError) as caught:
            self._result_for(blocks)
        self.assertNotIsInstance(caught.exception, LLMEmptyCompletionError)
        self.assertFalse(caught.exception.retryable)

    def test_parallel_tool_use_blocks_all_survive_in_order(self):
        result = self._result_for(
            [
                _Obj(type="text", text="Gathering context."),
                _Obj(type="tool_use", id="toolu_1", name="get_lead_history", input={}),
                _Obj(type="tool_use", id="toolu_2", name="check_ae_calendar", input={"days": 7}),
            ]
        )
        self.assertEqual([c.id for c in result.tool_calls], ["toolu_1", "toolu_2"])
        self.assertEqual([dict(c.arguments) for c in result.tool_calls], [{}, {"days": 7}])
        self.assertEqual(result.text, "Gathering context.")

    def test_block_without_an_input_is_a_zero_argument_call(self):
        result = self._result_for(
            [_Obj(type="tool_use", id="toolu_1", name="get_lead_history", input=None)]
        )
        self.assertEqual(dict(result.tool_calls[0].arguments), {})

    def test_block_without_an_id_raises(self):
        self._assert_raises_structural(
            [_Obj(type="tool_use", id=None, name="get_lead_history", input={})]
        )

    def test_block_without_a_name_raises(self):
        self._assert_raises_structural([_Obj(type="tool_use", id="toolu_1", name="", input={})])

    def test_block_whose_input_is_not_an_object_raises(self):
        self._assert_raises_structural(
            [_Obj(type="tool_use", id="toolu_1", name="get_lead_history", input=[1, 2])]
        )


class ClaudeToolResultFoldTests(TestCase):
    """Anthropic's contract: parallel tool results ride in one user message."""

    def _kwargs_for(self, messages):
        client = claude_mod.ClaudeClient.__new__(claude_mod.ClaudeClient)
        client.model = "m"
        client.default_max_tokens = 100
        return client._chat_request_kwargs(messages, (), None, None)

    def test_consecutive_tool_results_become_one_user_message(self):
        wire = self._kwargs_for(
            [
                Message(role="user", content="hi"),
                Message(
                    role="assistant",
                    tool_calls=(
                        ToolCallRequest(id="toolu_1", name="get_lead_history", arguments={}),
                        ToolCallRequest(id="toolu_2", name="check_ae_calendar", arguments={}),
                    ),
                ),
                Message(role="tool_result", tool_call_id="toolu_1", content="history"),
                Message(role="tool_result", tool_call_id="toolu_2", content="calendar"),
            ]
        )["messages"]
        self.assertEqual([m["role"] for m in wire], ["user", "assistant", "user"])
        blocks = wire[-1]["content"]
        self.assertEqual([b["type"] for b in blocks], ["tool_result", "tool_result"])
        self.assertEqual([b["tool_use_id"] for b in blocks], ["toolu_1", "toolu_2"])
        self.assertEqual([b["content"] for b in blocks], ["history", "calendar"])

    def test_tool_results_split_by_another_turn_stay_separate(self):
        wire = self._kwargs_for(
            [
                Message(role="user", content="hi"),
                Message(role="tool_result", tool_call_id="toolu_1", content="history"),
                Message(role="assistant", content="thinking"),
                Message(role="tool_result", tool_call_id="toolu_2", content="calendar"),
            ]
        )["messages"]
        self.assertEqual([m["role"] for m in wire], ["user", "user", "assistant", "user"])
        self.assertEqual(len(wire[1]["content"]), 1)
        self.assertEqual(len(wire[3]["content"]), 1)


def _narrating_tool_call_turn():
    """A tool-call turn whose text is the model narrating its way to the call."""
    return LLMResult(
        text="<function=get_lead_history{}></function> Subject: Checking in",
        provider="fake",
        model="m",
        finish_reason=FINISH_TOOL_CALLS,
        raw_finish_reason="tool_calls",
        tool_calls=(ToolCallRequest(id="c9", name="get_lead_history", arguments={}),),
    )


class _FakeChatClient(LLMClient):
    """Scripted chat client (each agent test module carries its own copy)."""

    provider_name = "fake"

    def __init__(self, script):
        super().__init__(model="fake-model", default_max_tokens=1)
        self.script, self.chat_calls = list(script), []

    def generate(self, prompt, max_tokens=None, timeout=None):
        raise AssertionError("agent loop must not take the blocking path")

    async def agenerate_chat(self, messages, *, tools=(), max_tokens=None, timeout=None):
        self.chat_calls.append({"messages": list(messages), "tools": tuple(tools)})
        return self.script.pop(0)


class LoopFinishReasonTests(TestCase):
    """A tool-call turn's text must never become the final draft, including on
    the forced-final turn where tools are not offered."""

    @classmethod
    def setUpTestData(cls):
        cls.lead = Lead.objects.create(
            id="lead_mus66",
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

    def _run(self, script):
        pks = state.create_lead_runs("run-mus66", [self.lead.id])
        client = _FakeChatClient(script)
        outcome = asyncio.run(
            agent_loop.run_agent_lead(
                prompt="PROMPT",
                lead_run_pk=pks[self.lead.id],
                prior_steps=state.load_prior_steps(pks[self.lead.id]),
                context=tools.build_tool_context(self.lead, (), (), (), None),
                client=client,
                runtime=get_planner_runtime(),
                checkpoint=state.Checkpoint(),
            )
        )
        return outcome, client, pks[self.lead.id]

    def test_narration_on_a_tool_call_turn_never_becomes_the_draft(self):
        tool_turn = LLMResult(
            text="",
            provider="fake",
            model="m",
            finish_reason=FINISH_TOOL_CALLS,
            raw_finish_reason="tool_calls",
            tool_calls=(ToolCallRequest(id="c1", name="get_lead_history", arguments={}),),
        )
        # Five tool turns spend the step budget; the sixth is the forced final.
        outcome, client, pk = self._run([tool_turn] * 5 + [_narrating_tool_call_turn()])
        self.assertEqual(client.chat_calls[-1]["tools"], ())  # forced final, no tools offered
        self.assertEqual(outcome.draft_text, "")
        self.assertFalse(
            app_models.AgentStep.objects.filter(lead_run_id=pk, kind="final").exists(),
            "the model's narration was persisted as the run's final draft",
        )
        self.assertEqual(app_models.AgentLeadRun.objects.get(pk=pk).status, "exhausted")
