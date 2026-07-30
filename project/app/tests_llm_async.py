"""Tests for the native async provider paths (MUS-46).

The first class here is the important one. `LoopBoundAsyncClient` exists to stop
a specific bug: an async HTTP client cached across two `asyncio.run()` calls is
bound to the first, now-closed, event loop. It is easy to write, produces an
error message that talks about transports rather than about caching, and — most
dangerously — a naive cache passes every single-run test. So the two-sequential-
runs test comes first and is written as a regression lock, not as coverage.
"""

import asyncio
import os
import unittest
from unittest import mock

import anthropic
import httpx

from project.app.services.llm import claude as claude_mod
from project.app.services.llm import errors
from project.app.services.llm.base import LoopBoundAsyncClient
from project.app.services.llm.groq import GroqClient

# ---------------------------------------------------------------------------
# the dead-loop bug
# ---------------------------------------------------------------------------


class LoopBoundAsyncClientTests(unittest.TestCase):
    def _cache(self):
        """A cache over trivial sentinel clients.

        Deliberately not Mocks: a Mock auto-creates any attribute you ask for,
        so `client.closed` would read truthy whether or not the closer ever ran
        -- and "was this client closed?" is precisely what these tests assert.
        """
        built: list[object] = []
        closed: list[object] = []

        def factory():
            client = object()
            built.append(client)
            return client

        async def closer(client):
            closed.append(client)

        return LoopBoundAsyncClient(factory=factory, closer=closer), built, closed

    def test_two_sequential_asyncio_runs_each_get_a_live_client(self):
        # THE regression lock. asyncio.run() creates and then closes a fresh
        # loop, so a client cached from the first run is bound to a dead one.
        # Both runs must succeed, and the second must NOT reuse the first's
        # client.
        cache, built, _ = self._cache()

        async def use():
            return cache.get()

        first = asyncio.run(use())
        second = asyncio.run(use())

        self.assertEqual(len(built), 2)
        self.assertIsNot(first, second)

    def test_one_client_is_reused_within_a_single_loop(self):
        # The other half of the contract: caching must still cache. A client
        # rebuilt per call would throw away connection reuse, which is most of
        # the reason to hold an AsyncClient at all.
        cache, built, _ = self._cache()

        async def use_twice():
            return cache.get(), cache.get()

        first, second = asyncio.run(use_twice())
        self.assertIs(first, second)
        self.assertEqual(len(built), 1)

    def test_aclose_releases_the_client_for_the_current_loop(self):
        cache, built, closed = self._cache()

        async def build_then_close():
            cache.get()
            await cache.aclose()

        asyncio.run(build_then_close())
        self.assertEqual(closed, built)

    def test_aclose_is_safe_when_nothing_was_ever_built(self):
        cache, built, closed = self._cache()
        asyncio.run(cache.aclose())
        self.assertEqual(built, [])
        self.assertEqual(closed, [])

    def test_a_client_from_a_dead_loop_is_dropped_not_awaited(self):
        # Closing it would mean awaiting on the loop that already went away --
        # the very cross-loop use this class exists to prevent. Its sockets went
        # down with that loop, so dropping the reference is the correct move.
        cache, built, closed = self._cache()

        asyncio.run(_call(cache.get))
        asyncio.run(cache.aclose())

        self.assertEqual(len(built), 1)
        self.assertEqual(closed, [])

    def test_get_outside_a_running_loop_is_a_loud_error(self):
        cache, _, _ = self._cache()
        with self.assertRaises(RuntimeError):
            cache.get()


async def _call(fn):
    return fn()


# ---------------------------------------------------------------------------
# Claude async path
# ---------------------------------------------------------------------------


def _text_response(text="Subject: Hi\n\nBody", **overrides):
    response = mock.Mock()
    block = mock.Mock()
    block.type = "text"
    block.text = text
    response.content = [block]
    response.usage = mock.Mock(
        input_tokens=100,
        output_tokens=20,
        cache_read_input_tokens=None,
        cache_creation_input_tokens=None,
    )
    response.model = "claude-sonnet-4-6-20260101"
    response.stop_reason = "end_turn"
    for key, value in overrides.items():
        setattr(response, key, value)
    return response


class ClaudeAsyncTests(unittest.IsolatedAsyncioTestCase):
    def _patch_sdk(self, **create):
        patcher = mock.patch.object(claude_mod.anthropic, "AsyncAnthropic")
        mock_cls = patcher.start()
        self.addCleanup(patcher.stop)
        client = mock_cls.return_value
        client.api_key = "test-key"
        client.auth_token = None
        client.messages.create = mock.AsyncMock(**create)
        return mock_cls, client

    async def test_agenerate_returns_a_full_result(self):
        _, client = self._patch_sdk(return_value=_text_response())
        result = await claude_mod.ClaudeClient(model="claude-requested").agenerate(
            "p", max_tokens=42
        )

        self.assertEqual(result.text, "Subject: Hi\n\nBody")
        self.assertEqual(result.provider, "claude")
        self.assertEqual(result.model, "claude-requested")
        self.assertEqual(result.response_model, "claude-sonnet-4-6-20260101")
        self.assertEqual(result.input_tokens, 100)
        self.assertEqual(result.output_tokens, 20)
        self.assertIsNotNone(result.latency_s)
        self.assertEqual(client.messages.create.await_args.kwargs["max_tokens"], 42)

    async def test_sdk_retries_are_disabled_on_both_clients(self):
        # The SDK retries twice by default. Left on, every one of our attempts
        # would hide two more HTTP calls: invisible to a per-attempt span,
        # double the effective budget, and applied to Claude only -- the
        # OpenAI-compatible adapters talk to httpx directly and never retried.
        mock_cls, _ = self._patch_sdk(return_value=_text_response())
        await claude_mod.ClaudeClient(timeout_s=12.5).agenerate("p")
        self.assertEqual(mock_cls.call_args.kwargs["max_retries"], 0)
        self.assertEqual(mock_cls.call_args.kwargs["timeout"], 12.5)

        with mock.patch.object(claude_mod.anthropic, "Anthropic") as sync_cls:
            sync_cls.return_value.messages.create.return_value = _text_response()
            claude_mod.ClaudeClient(timeout_s=7.0).generate("p")
        self.assertEqual(sync_cls.call_args.kwargs["max_retries"], 0)
        self.assertEqual(sync_cls.call_args.kwargs["timeout"], 7.0)

    async def test_async_failures_are_typed_and_timed(self):
        response = httpx.Response(
            429,
            headers={"retry-after": "3"},
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        )
        self._patch_sdk(
            side_effect=anthropic.RateLimitError("slow down", response=response, body=None)
        )
        with self.assertRaises(errors.LLMRateLimitError) as ctx:
            await claude_mod.ClaudeClient().agenerate("p")
        self.assertEqual(ctx.exception.retry_after, 3.0)
        self.assertIsNotNone(ctx.exception.latency_s)

    async def test_missing_credentials_fail_before_any_request(self):
        _, client = self._patch_sdk(return_value=_text_response())
        client.api_key = None
        client.auth_token = None
        with self.assertRaises(errors.LLMAuthError):
            await claude_mod.ClaudeClient().agenerate("p")
        client.messages.create.assert_not_awaited()

    async def test_acomplete_returns_text(self):
        self._patch_sdk(return_value=_text_response("Just the copy"))
        self.assertEqual(await claude_mod.ClaudeClient().acomplete("p"), "Just the copy")

    async def test_aclose_closes_the_underlying_sdk_client(self):
        _, client = self._patch_sdk(return_value=_text_response())
        client.close = mock.AsyncMock()
        claude = claude_mod.ClaudeClient()
        await claude.agenerate("p")
        await claude.aclose()
        client.close.assert_awaited_once()

    async def test_base_aclose_is_a_no_op_for_adapters_without_async_state(self):
        # LLMClient.aclose() has to be safe to call on any client, so a caller
        # holding the configured provider can close it in a finally without
        # asking which adapter it got.
        class _Sync(claude_mod.LLMClient):
            provider_name = "stub"

            def generate(self, prompt, max_tokens=None):  # pragma: no cover - unused
                raise AssertionError

        self.assertIsNone(await _Sync(model="m").aclose())


# ---------------------------------------------------------------------------
# OpenAI-compatible async path
# ---------------------------------------------------------------------------


def _chat_body(content="Generated copy"):
    return {
        "model": "llama-3.3-70b-versatile-0000",
        "usage": {"prompt_tokens": 900, "completion_tokens": 120},
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
    }


class OpenAICompatibleAsyncTests(unittest.IsolatedAsyncioTestCase):
    def _patch_client(self, **post):
        patcher = mock.patch("project.app.services.llm.openai_compatible.httpx.AsyncClient")
        mock_cls = patcher.start()
        self.addCleanup(patcher.stop)
        client = mock_cls.return_value
        client.post = mock.AsyncMock(**post)
        client.aclose = mock.AsyncMock()
        return mock_cls, client

    def _ok_response(self, body=None):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = body if body is not None else _chat_body()
        return response

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    async def test_agenerate_posts_and_returns_a_full_result(self):
        mock_cls, client = self._patch_client(return_value=self._ok_response())
        result = await GroqClient(model="some-model", timeout_s=9.0).agenerate("a prompt")

        self.assertEqual(result.text, "Generated copy")
        self.assertEqual(result.provider, "groq")
        self.assertEqual(result.input_tokens, 900)
        self.assertEqual(result.output_tokens, 120)
        self.assertIsNotNone(result.latency_s)

        # The timeout lives on the client, not on each request.
        self.assertEqual(mock_cls.call_args.kwargs["timeout"], 9.0)
        url, kwargs = client.post.await_args.args[0], client.post.await_args.kwargs
        self.assertEqual(url, "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertNotIn("timeout", kwargs)

    @mock.patch.dict(os.environ, {}, clear=True)
    async def test_missing_key_fails_before_a_client_is_built(self):
        mock_cls, client = self._patch_client(return_value=self._ok_response())
        with self.assertRaises(errors.LLMAuthError):
            await GroqClient().agenerate("a prompt")
        client.post.assert_not_awaited()

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    async def test_async_transport_failure_is_typed_and_timed(self):
        self._patch_client(side_effect=httpx.ConnectTimeout("too slow"))
        with self.assertRaises(errors.LLMTimeoutError) as ctx:
            await GroqClient().agenerate("a prompt")
        self.assertTrue(ctx.exception.retryable)
        self.assertIsNotNone(ctx.exception.latency_s)

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    async def test_aclose_closes_the_underlying_client(self):
        _, client = self._patch_client(return_value=self._ok_response())
        groq = GroqClient()
        await groq.agenerate("a prompt")
        await groq.aclose()
        client.aclose.assert_awaited_once()

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    async def test_acomplete_returns_text(self):
        self._patch_client(return_value=self._ok_response())
        self.assertEqual(await GroqClient().acomplete("a prompt"), "Generated copy")


class AdapterAcrossRunsTests(unittest.TestCase):
    """The dead-loop bug, exercised through a real adapter rather than the cache.

    LoopBoundAsyncClientTests pins the mechanism; this pins the wiring. A client
    that forgot to route through the cache would pass that class and fail here.
    """

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_one_adapter_serves_two_sequential_asyncio_runs(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = _chat_body()

        with mock.patch("project.app.services.llm.openai_compatible.httpx.AsyncClient") as mock_cls:
            mock_cls.side_effect = lambda **kwargs: mock.Mock(
                post=mock.AsyncMock(return_value=response), aclose=mock.AsyncMock()
            )
            client = GroqClient()
            first = asyncio.run(client.agenerate("a prompt"))
            second = asyncio.run(client.agenerate("a prompt"))

        self.assertEqual(first.text, "Generated copy")
        self.assertEqual(second.text, "Generated copy")
        # A fresh transport per loop is the whole point.
        self.assertEqual(mock_cls.call_count, 2)


if __name__ == "__main__":
    unittest.main()
