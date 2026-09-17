"""Tests for structured outputs on the OpenAI-compatible adapter."""

import asyncio
import os
import unittest
from unittest import mock

from pydantic import BaseModel

from project.app.services.llm import errors, structured
from project.app.services.llm.base import LLMClient
from project.app.services.llm.chat_types import Message
from project.app.services.llm.groq import GroqClient


class CalendarEvent(BaseModel):
    name: str
    date: str
    participants: list[str]


class Invite(BaseModel):
    event: CalendarEvent
    note: str | None


EVENT_JSON = '{"name": "Science fair", "date": "Friday", "participants": ["Alice", "Bob"]}'


def _body(content, **extra):
    return {
        "model": "openai/gpt-oss-20b-0000",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        **extra,
    }


class SchemaBuildingTests(unittest.TestCase):
    def test_the_request_field_names_the_model_and_carries_its_schema(self):
        block = structured.response_format_for(CalendarEvent)

        self.assertEqual(block["type"], "json_schema")
        self.assertEqual(block["json_schema"]["name"], "CalendarEvent")
        self.assertIs(block["json_schema"]["strict"], True)
        self.assertEqual(
            sorted(block["json_schema"]["schema"]["properties"]),
            ["date", "name", "participants"],
        )

    def test_strict_mode_can_be_turned_off_for_a_provider_that_rejects_it(self):
        block = structured.response_format_for(CalendarEvent, strict=False)
        self.assertIs(block["json_schema"]["strict"], False)

    def test_every_object_in_the_schema_refuses_unlisted_keys(self):
        schema = structured.response_format_for(Invite)["json_schema"]["schema"]

        self.assertIs(schema["additionalProperties"], False)
        self.assertIs(schema["$defs"]["CalendarEvent"]["additionalProperties"], False)

    def test_every_property_is_required_even_when_the_model_defaults_it(self):
        schema = structured.response_format_for(Invite)["json_schema"]["schema"]

        self.assertEqual(sorted(schema["required"]), ["event", "note"])
        self.assertEqual(
            sorted(schema["$defs"]["CalendarEvent"]["required"]),
            ["date", "name", "participants"],
        )

    def test_a_schema_without_objects_survives_the_walk_unchanged(self):
        self.assertEqual(structured.strict_schema({"type": "string"}), {"type": "string"})


class _StructuredCall:
    """One mocked chat-completions call through generate_structured."""

    def _post(self, content=EVENT_JSON, body=None):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = body if body is not None else _body(content)
        return response

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def _call(self, input, schema_model=CalendarEvent, content=EVENT_JSON, body=None, **kwargs):
        with mock.patch("project.app.services.llm.openai_compatible.httpx.post") as post:
            post.return_value = self._post(content, body)
            result = GroqClient(model="some-model").generate_structured(
                input, schema_model, **kwargs
            )
        return result, post.call_args


class StructuredRequestTests(_StructuredCall, unittest.TestCase):
    def test_a_prompt_string_is_sent_as_one_user_turn_with_the_schema(self):
        _, call = self._call("Extract the event information.")

        sent = call.kwargs["json"]
        self.assertEqual(
            sent["messages"], [{"role": "user", "content": "Extract the event information."}]
        )
        self.assertEqual(sent["response_format"], structured.response_format_for(CalendarEvent))

    def test_a_transcript_is_sent_turn_for_turn_with_the_schema(self):
        _, call = self._call(
            [
                Message(role="system", content="Extract the event information."),
                Message(
                    role="user", content="Alice and Bob are going to a science fair on Friday."
                ),
            ]
        )

        sent = call.kwargs["json"]
        self.assertEqual(
            sent["messages"],
            [
                {"role": "system", "content": "Extract the event information."},
                {
                    "role": "user",
                    "content": "Alice and Bob are going to a science fair on Friday.",
                },
            ],
        )
        self.assertEqual(sent["response_format"]["json_schema"]["name"], "CalendarEvent")

    def test_an_ordinary_call_still_asks_for_no_particular_format(self):
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}):
            with mock.patch("project.app.services.llm.openai_compatible.httpx.post") as post:
                post.return_value = self._post("Generated copy")
                GroqClient(model="some-model").complete("a prompt")

        self.assertNotIn("response_format", post.call_args.kwargs["json"])

    def test_max_tokens_and_timeout_reach_the_request_as_they_do_elsewhere(self):
        _, call = self._call("p", max_tokens=42, timeout=4.0)

        self.assertEqual(call.kwargs["json"]["max_tokens"], 42)
        self.assertEqual(call.kwargs["timeout"], 4.0)


class StructuredResponseTests(_StructuredCall, unittest.TestCase):
    def test_the_completion_comes_back_as_a_validated_model_instance(self):
        result, _ = self._call("p")

        self.assertIsInstance(result.parsed, CalendarEvent)
        self.assertEqual(result.parsed.name, "Science fair")
        self.assertEqual(result.parsed.participants, ["Alice", "Bob"])

    def test_the_underlying_result_keeps_its_usage_and_finish_reason(self):
        result, _ = self._call(
            "p",
            body=_body(EVENT_JSON, usage={"prompt_tokens": 900, "completion_tokens": 120}),
        )

        self.assertEqual(result.result.input_tokens, 900)
        self.assertEqual(result.result.output_tokens, 120)
        self.assertEqual(result.result.finish_reason, "stop")
        self.assertEqual(result.result.text, EVENT_JSON)

    def test_a_completion_that_is_not_json_is_a_malformed_response(self):
        with self.assertRaises(errors.LLMMalformedResponseError) as ctx:
            self._call("p", content="Sorry, I can't do that.")

        self.assertIn("CalendarEvent", str(ctx.exception))

    def test_json_that_does_not_fit_the_schema_is_a_malformed_response(self):
        with self.assertRaises(errors.LLMMalformedResponseError):
            self._call("p", content='{"name": "Science fair"}')

    def test_a_malformed_response_names_the_provider(self):
        with self.assertRaises(errors.LLMMalformedResponseError) as ctx:
            self._call("p", content="not json")

        self.assertEqual(ctx.exception.provider, "groq")


class StructuredAsyncTests(unittest.IsolatedAsyncioTestCase):
    def _patch_client(self, content=EVENT_JSON):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = _body(content)
        patcher = mock.patch("project.app.services.llm.openai_compatible.httpx.AsyncClient")
        mock_cls = patcher.start()
        self.addCleanup(patcher.stop)
        client = mock_cls.return_value
        client.post = mock.AsyncMock(return_value=response)
        client.aclose = mock.AsyncMock()
        return client

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    async def test_the_async_path_asks_for_and_parses_the_same_schema(self):
        client = self._patch_client()

        result = await GroqClient(model="some-model").agenerate_structured(
            "Extract the event information.", CalendarEvent
        )

        self.assertEqual(result.parsed.date, "Friday")
        sent = client.post.await_args.kwargs["json"]
        self.assertEqual(sent["response_format"]["json_schema"]["name"], "CalendarEvent")

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    async def test_a_bad_async_completion_is_a_malformed_response(self):
        self._patch_client(content="not json")

        with self.assertRaises(errors.LLMMalformedResponseError):
            await GroqClient().agenerate_structured("p", CalendarEvent)


class UnsupportedProviderTests(unittest.TestCase):
    class TextOnlyClient(LLMClient):
        provider_name = "text-only"

        def generate(self, prompt, max_tokens=None, timeout=None):
            raise NotImplementedError

    def test_an_adapter_without_structured_outputs_says_which_one(self):
        with self.assertRaises(NotImplementedError) as ctx:
            self.TextOnlyClient(model="m").generate_structured("p", CalendarEvent)

        self.assertIn("TextOnlyClient", str(ctx.exception))

    def test_the_async_seam_says_so_too(self):
        with self.assertRaises(NotImplementedError):
            asyncio.run(self.TextOnlyClient(model="m").agenerate_structured("p", CalendarEvent))
