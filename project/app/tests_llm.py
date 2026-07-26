"""Tests for the provider-agnostic LLM layer (project/app/services/llm/)."""

import os
import unittest
from unittest import mock

from project.app.services import llm
from project.app.services.llm import claude as claude_mod
from project.app.services.llm import config
from project.app.services.llm.groq import GroqClient


# ---------------------------------------------------------------------------
# Claude adapter (anthropic SDK mocked)
# ---------------------------------------------------------------------------


class ClaudeClientTests(unittest.TestCase):
    def _mock_response(self, *blocks):
        response = mock.Mock()
        response.content = list(blocks)
        return response

    def _block(self, block_type, text=""):
        block = mock.Mock()
        block.type = block_type
        block.text = text
        return block

    def test_complete_passes_model_and_max_tokens(self):
        with mock.patch.object(claude_mod.anthropic, "Anthropic") as mock_cls:
            client = mock_cls.return_value
            client.messages.create.return_value = self._mock_response(
                self._block("text", "Hello there")
            )
            result = claude_mod.ClaudeClient().complete("a prompt", max_tokens=500)

        self.assertEqual(result, "Hello there")
        kwargs = client.messages.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "claude-sonnet-4-6")
        self.assertEqual(kwargs["max_tokens"], 500)
        self.assertEqual(kwargs["messages"], [{"role": "user", "content": "a prompt"}])

    def test_complete_joins_only_text_blocks(self):
        with mock.patch.object(claude_mod.anthropic, "Anthropic") as mock_cls:
            client = mock_cls.return_value
            client.messages.create.return_value = self._mock_response(
                self._block("thinking", "internal"),
                self._block("text", "Subject: Hi\n\nBody"),
            )
            result = claude_mod.ClaudeClient().complete("p")

        self.assertEqual(result, "Subject: Hi\n\nBody")

    def test_complete_falls_back_to_default_max_tokens(self):
        with mock.patch.object(claude_mod.anthropic, "Anthropic") as mock_cls:
            client = mock_cls.return_value
            client.messages.create.return_value = self._mock_response(
                self._block("text", "x")
            )
            claude_mod.ClaudeClient(default_max_tokens=123).complete("p")

        self.assertEqual(client.messages.create.call_args.kwargs["max_tokens"], 123)


# ---------------------------------------------------------------------------
# OpenAI-compatible adapter (httpx mocked) -- exercised via GroqClient
# ---------------------------------------------------------------------------


class OpenAICompatibleClientTests(unittest.TestCase):
    def _mock_post(self, content="Generated copy"):
        response = mock.Mock()
        response.json.return_value = {"choices": [{"message": {"content": content}}]}
        response.raise_for_status.return_value = None
        return response

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_complete_posts_chat_completion_and_returns_content(self):
        with mock.patch(
            "project.app.services.llm.openai_compatible.httpx.post"
        ) as post:
            post.return_value = self._mock_post("Generated copy")
            result = GroqClient(model="some-model").complete("a prompt", max_tokens=42)

        self.assertEqual(result, "Generated copy")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(kwargs["json"]["model"], "some-model")
        self.assertEqual(kwargs["json"]["max_tokens"], 42)
        self.assertEqual(
            kwargs["json"]["messages"], [{"role": "user", "content": "a prompt"}]
        )

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_complete_raises_when_api_key_missing(self):
        with self.assertRaises(RuntimeError) as ctx:
            GroqClient().complete("a prompt")
        self.assertIn("GROQ_API_KEY", str(ctx.exception))


# ---------------------------------------------------------------------------
# Factory (provider selection from config)
# ---------------------------------------------------------------------------


class GetLLMClientTests(unittest.TestCase):
    def setUp(self):
        llm._build_client.cache_clear()

    def tearDown(self):
        llm._build_client.cache_clear()

    def test_selects_provider_class_and_applies_config(self):
        with (
            mock.patch.object(config, "get_provider", return_value="groq"),
            mock.patch.object(
                config,
                "get_provider_config",
                return_value={"model": "configured-model", "max_tokens": 256},
            ),
        ):
            client = llm.get_llm_client()

        self.assertIsInstance(client, GroqClient)
        self.assertEqual(client.model, "configured-model")
        self.assertEqual(client.default_max_tokens, 256)

    def test_unknown_provider_raises(self):
        with (
            mock.patch.object(config, "get_provider", return_value="bogus"),
            mock.patch.object(config, "get_provider_config", return_value={}),
        ):
            with self.assertRaises(ValueError) as ctx:
                llm.get_llm_client()
        self.assertIn("bogus", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
