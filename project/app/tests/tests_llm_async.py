"""Tests for the native async provider paths (MUS-46). An async client cached
across two asyncio.run() calls is bound to a dead loop — the two-sequential-runs
test is the regression lock, since a naive cache passes every single-run test."""

import asyncio
import os
import threading
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
        """Sentinel clients, not Mocks: a Mock's auto-attributes would fake "closed"."""
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
        # THE regression lock: the second run must NOT reuse the first's client.
        cache, built, _ = self._cache()

        async def use():
            return cache.get()

        first = asyncio.run(use())
        second = asyncio.run(use())

        self.assertEqual(len(built), 2)
        self.assertIsNot(first, second)

    def test_one_client_is_reused_within_a_single_loop(self):
        # The other half of the contract: caching must still cache.
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

    def test_a_client_from_another_loop_is_never_awaited(self):
        # Closing it would mean awaiting on a loop we are not on.
        cache, built, closed = self._cache()

        asyncio.run(_call(cache.get))
        asyncio.run(cache.aclose())

        self.assertEqual(len(built), 1)
        self.assertEqual(closed, [])

    def test_two_threads_running_their_own_loops_never_share_a_client(self):
        # One cache is shared process-wide; two threads each in asyncio.run()
        # are two live loops at once, and an unlocked check-then-store can hand
        # one thread the client built for the other's loop.
        cache, built, _ = self._cache()
        barrier = threading.Barrier(2)
        seen: dict[str, list[object]] = {}

        def worker(name):
            async def use():
                barrier.wait()  # maximise the overlap on get()
                return [cache.get() for _ in range(20)]

            seen[name] = asyncio.run(use())

        threads = [threading.Thread(target=worker, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Each thread must see one client for the whole of its own loop...
        for name, clients in seen.items():
            self.assertEqual(len(set(map(id, clients))), 1, f"{name} saw more than one client")
        # ...and the two threads must never have been handed the same one.
        self.assertIsNot(seen["a"][0], seen["b"][0])

    def test_aclose_does_not_yank_a_client_another_live_loop_is_using(self):
        # With TWO live loops, aclose() must leave the other thread's client
        # alone rather than clearing the cache under it.
        cache, built, closed = self._cache()
        holder = {}

        def worker():
            async def use():
                holder["client"] = cache.get()

            asyncio.run(use())

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        # A different loop calling aclose() must not close it *or* clear it.
        asyncio.run(cache.aclose())

        self.assertEqual(closed, [])
        # Private field on purpose: "did aclose() leave the cache alone?" has
        # no public observable.
        self.assertIs(cache._client, holder["client"])

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
        # Pinned to None explicitly: a Mock invents attributes, so
        # _check_credentials would otherwise pass on a truthy auto-attribute.
        client.auth_token = None
        client.credentials = None
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

    async def test_sdk_retries_are_off_where_we_own_the_budget_and_on_where_we_do_not(self):
        # llm/retry.py owns the async budget, so SDK retries are off there; the
        # sync path has no retries of ours, so the SDK's stay on.
        mock_cls, _ = self._patch_sdk(return_value=_text_response())
        await claude_mod.ClaudeClient(timeout_s=12.5).agenerate("p")
        self.assertEqual(mock_cls.call_args.kwargs["max_retries"], 0)
        self.assertEqual(mock_cls.call_args.kwargs["timeout"], 12.5)

        with mock.patch.object(claude_mod.anthropic, "Anthropic") as sync_cls:
            sync_cls.return_value.messages.create.return_value = _text_response()
            claude_mod.ClaudeClient(timeout_s=7.0).generate("p")
        self.assertNotIn("max_retries", sync_cls.call_args.kwargs)
        # Both paths tighten the timeout off the SDK's 600s default.
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
        client.credentials = None
        with self.assertRaises(errors.LLMAuthError):
            await claude_mod.ClaudeClient().agenerate("p")
        client.messages.create.assert_not_awaited()

    async def test_a_credentials_provider_counts_as_a_resolved_credential(self):
        # A credentials provider (profile-on-disk, workload identity) leaves
        # api_key and auth_token None; checking only those two static
        # mechanisms would reject a working deployment.
        _, client = self._patch_sdk(return_value=_text_response())
        client.api_key = None
        client.auth_token = None
        client.credentials = object()
        result = await claude_mod.ClaudeClient().agenerate("p")
        self.assertEqual(result.text, "Subject: Hi\n\nBody")

    async def test_acomplete_returns_text(self):
        self._patch_sdk(return_value=_text_response("Just the copy"))
        self.assertEqual(await claude_mod.ClaudeClient().acomplete("p"), "Just the copy")

    async def test_the_async_sdk_client_gets_the_resolved_key(self):
        mock_cls, _ = self._patch_sdk(return_value=_text_response())
        await claude_mod.ClaudeClient(api_key="db-key").agenerate("p")
        self.assertEqual(mock_cls.call_args.kwargs["api_key"], "db-key")

    async def test_no_resolved_key_hands_the_lookup_back_to_the_sdk(self):
        # api_key=None, not "": an empty string would be a real (invalid)
        # credential instead of falling back to ANTHROPIC_API_KEY.
        mock_cls, _ = self._patch_sdk(return_value=_text_response())
        await claude_mod.ClaudeClient().agenerate("p")
        self.assertIsNone(mock_cls.call_args.kwargs["api_key"])

    async def test_aclose_closes_the_underlying_sdk_client(self):
        _, client = self._patch_sdk(return_value=_text_response())
        client.close = mock.AsyncMock()
        claude = claude_mod.ClaudeClient()
        await claude.agenerate("p")
        await claude.aclose()
        client.close.assert_awaited_once()

    async def test_base_aclose_is_a_no_op_for_adapters_without_async_state(self):
        # aclose() must be safe on any adapter, sync ones included.
        class _Sync(claude_mod.LLMClient):
            provider_name = "stub"

            def generate(self, prompt, max_tokens=None, timeout=None):  # pragma: no cover - unused
                raise AssertionError

        self.assertIsNone(await _Sync(model="m").aclose())


# ---------------------------------------------------------------------------
# OpenAI-compatible async path
# ---------------------------------------------------------------------------


def _chat_body(content="Generated copy"):
    return {
        "model": "openai/gpt-oss-20b-0000",
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

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "env-key"})
    async def test_the_resolved_key_beats_the_env_var_on_the_async_path_too(self):
        _, client = self._patch_client(return_value=self._ok_response())
        await GroqClient(api_key="db-key").agenerate("a prompt")
        headers = client.post.await_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer db-key")

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    async def test_a_per_call_timeout_rides_on_the_request(self):
        # An override cannot change the client-level default without disturbing
        # other in-flight calls, so it travels with this one request.
        mock_cls, client = self._patch_client(return_value=self._ok_response())
        await GroqClient(timeout_s=60.0).agenerate("a prompt", timeout=4.0)
        self.assertEqual(mock_cls.call_args.kwargs["timeout"], 60.0)
        self.assertEqual(client.post.await_args.kwargs["timeout"], 4.0)

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    async def test_calls_genuinely_overlap(self):
        # If agenerate were thread-pooled or serialized on a lock, `in_flight`
        # would never exceed 1.
        in_flight = 0
        peak = 0
        release = asyncio.Event()

        async def slow_post(*args, **kwargs):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await release.wait()
            in_flight -= 1
            return self._ok_response()

        self._patch_client(side_effect=slow_post)
        client = GroqClient()
        tasks = [asyncio.create_task(client.agenerate("a prompt")) for _ in range(4)]
        await asyncio.sleep(0)  # let every task reach the await
        release.set()
        results = await asyncio.gather(*tasks)

        self.assertEqual(peak, 4)
        self.assertTrue(all(r.text == "Generated copy" for r in results))


class AdapterAcrossRunsTests(unittest.TestCase):
    """The dead-loop bug through a real adapter: pins the wiring, not the mechanism."""

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
