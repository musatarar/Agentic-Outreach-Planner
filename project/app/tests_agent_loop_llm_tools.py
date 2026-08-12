"""Component artifact: llm_tools (MUS-29).

Planted red by the skeleton PR — every test carries ``@unittest.expectedFailure``
and opens with a capability assertion, so a stripped-marker failure is an
AssertionError (or a NotImplementedError from a stub), never an
AttributeError/TypeError. The llm_tools component PR strips the markers and takes
this module to zero; sibling artifacts stay untouched (docs/contracts/agent-loop.md).
"""

import asyncio
import unittest
from unittest import mock

from django.test import SimpleTestCase

from project.app.services.llm import claude as claude_mod
from project.app.services.llm import openai_compatible as oa_mod
from project.app.services.llm import stub as stub_mod
from project.app.services.llm.base import FINISH_TOOL_CALLS, LLMClient
from project.app.services.llm.chat_types import Message, ToolSpec
from project.app.services.llm.errors import LLMMalformedResponseError

HISTORY_TOOL = ToolSpec(
    name="get_lead_history",
    description="d",
    parameters={"type": "object", "properties": {}},
)


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _usage():
    return _Obj(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=None,
        cache_creation_input_tokens=None,
    )


def _tool_use_response():
    return _Obj(
        content=[_Obj(type="tool_use", id="toolu_01", name="get_lead_history", input={"limit": 5})],
        stop_reason="tool_use",
        model="claude-sonnet-4-6",
        usage=_usage(),
    )


def _mixed_response():
    return _Obj(
        content=[
            _Obj(type="text", text="Let me check the history first."),
            _Obj(type="tool_use", id="toolu_02", name="get_lead_history", input={}),
        ],
        stop_reason="tool_use",
        model="claude-sonnet-4-6",
        usage=_usage(),
    )


def _empty_response():
    return _Obj(content=[], stop_reason="end_turn", model="claude-sonnet-4-6", usage=_usage())


class ClaudeToolCallTests(SimpleTestCase):
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

    @unittest.expectedFailure
    def test_pure_tool_use_response_parses_instead_of_raising(self):
        self.assertTrue(hasattr(claude_mod.ClaudeClient, "agenerate_chat"))
        client = self._client_returning(_tool_use_response())
        result = asyncio.run(
            client.agenerate_chat([Message(role="user", content="hi")], tools=(HISTORY_TOOL,))
        )
        self.assertEqual(result.text, "")
        self.assertEqual(result.finish_reason, FINISH_TOOL_CALLS)
        self.assertEqual(result.tool_calls[0].name, "get_lead_history")
        self.assertEqual(dict(result.tool_calls[0].arguments), {"limit": 5})

    @unittest.expectedFailure
    def test_mixed_text_and_tool_use_yields_both(self):
        self.assertTrue(hasattr(claude_mod.ClaudeClient, "agenerate_chat"))
        client = self._client_returning(_mixed_response())
        result = asyncio.run(
            client.agenerate_chat([Message(role="user", content="hi")], tools=(HISTORY_TOOL,))
        )
        self.assertEqual(result.text, "Let me check the history first.")
        self.assertEqual(result.tool_calls[0].id, "toolu_02")
        self.assertEqual(result.finish_reason, FINISH_TOOL_CALLS)

    @unittest.expectedFailure
    def test_no_text_no_tools_still_raises_malformed(self):
        self.assertTrue(hasattr(claude_mod.ClaudeClient, "agenerate_chat"))
        client = self._client_returning(_empty_response())
        with self.assertRaises(LLMMalformedResponseError):
            asyncio.run(client.agenerate_chat([Message(role="user", content="hi")]))


class BaseChatInterfaceTests(SimpleTestCase):
    @unittest.expectedFailure
    def test_agenerate_chat_default_raises_not_implemented(self):
        self.assertTrue(hasattr(LLMClient, "agenerate_chat"))

        class Bare(LLMClient):
            provider_name = "bare"

            def generate(self, prompt, max_tokens=None, timeout=None):
                raise AssertionError("unused")

        with self.assertRaises(NotImplementedError):
            asyncio.run(Bare(model="m").agenerate_chat([Message(role="user", content="x")]))


class OpenAICompatibleToolCallTests(SimpleTestCase):
    def _bare_client(self):
        client = oa_mod.OpenAICompatibleClient.__new__(oa_mod.OpenAICompatibleClient)
        client.model = "m"
        client.provider_name = "groq"
        return client

    @unittest.expectedFailure
    def test_null_content_with_tool_calls_parses(self):
        self.assertTrue(hasattr(oa_mod.OpenAICompatibleClient, "agenerate_chat"))
        body = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "get_lead_history",
                                    "arguments": '{"limit": 5}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "model": "m",
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }
        result = self._bare_client()._build_result(body, 0.1)
        self.assertEqual(result.text, "")
        self.assertEqual(result.tool_calls[0].arguments, {"limit": 5})
        self.assertEqual(result.finish_reason, FINISH_TOOL_CALLS)

    @unittest.expectedFailure
    def test_no_text_no_tools_still_raises_malformed(self):
        self.assertTrue(hasattr(oa_mod.OpenAICompatibleClient, "agenerate_chat"))
        body = {
            "choices": [{"message": {"content": None}, "finish_reason": "stop"}],
            "model": "m",
            "usage": {"prompt_tokens": 1, "completion_tokens": 0},
        }
        with self.assertRaises(LLMMalformedResponseError):
            self._bare_client()._build_result(body, 0.1)

    @unittest.expectedFailure
    def test_malformed_tool_arguments_json_raises(self):
        self.assertTrue(hasattr(oa_mod.OpenAICompatibleClient, "agenerate_chat"))
        body = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "get_lead_history", "arguments": "{not json"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "model": "m",
            "usage": {"prompt_tokens": 1, "completion_tokens": 0},
        }
        with self.assertRaises(LLMMalformedResponseError):
            self._bare_client()._build_result(body, 0.1)


class StubChatScriptTests(SimpleTestCase):
    """The stub is stateless: its scripted behavior derives from the message
    list, not instance state, because clients are lru_cache'd singletons."""

    def _client(self):
        with mock.patch.dict("os.environ", {stub_mod.ALLOW_ENV_VAR: "1"}):
            return stub_mod.StubClient(latency_mean_s=0.0, latency_stddev_s=0.0, seed=7)

    @unittest.expectedFailure
    def test_stub_sequence_is_deterministic_and_stateless(self):
        self.assertTrue(hasattr(stub_mod.StubClient, "agenerate_chat"))
        client = self._client()
        opening = [Message(role="user", content="- Agency: SYNTH-001")]
        first = asyncio.run(client.agenerate_chat(opening, tools=(HISTORY_TOOL,)))
        self.assertEqual(first.finish_reason, FINISH_TOOL_CALLS)
        self.assertEqual(first.tool_calls[0].name, "get_lead_history")

        followup = opening + [
            Message(role="assistant", tool_calls=first.tool_calls),
            Message(role="tool_result", tool_call_id=first.tool_calls[0].id, content="{}"),
        ]
        second = asyncio.run(client.agenerate_chat(followup, tools=(HISTORY_TOOL,)))
        self.assertEqual(second.tool_calls, ())
        self.assertNotEqual(second.text, "")

        # Stateless: replaying the opening conversation replays the tool call.
        again = asyncio.run(client.agenerate_chat(opening, tools=(HISTORY_TOOL,)))
        self.assertEqual(again.tool_calls[0].name, "get_lead_history")

    @unittest.expectedFailure
    def test_stub_without_tools_stays_a_plain_completion(self):
        self.assertTrue(hasattr(stub_mod.StubClient, "agenerate_chat"))
        client = self._client()
        result = asyncio.run(
            client.agenerate_chat([Message(role="user", content="- Agency: SYNTH-001")])
        )
        self.assertEqual(result.tool_calls, ())
        self.assertNotEqual(result.text, "")
