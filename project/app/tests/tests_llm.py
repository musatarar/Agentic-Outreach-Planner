"""Tests for the provider-agnostic LLM layer (project/app/services/llm/)."""

import asyncio
import dataclasses
import json
import os
import types
import unittest
from unittest import mock

import anthropic
import httpx

from project.app.services import llm
from project.app.services.llm import base, config, errors
from project.app.services.llm import claude as claude_mod
from project.app.services.llm import groq as groq_mod
from project.app.services.llm.chatgpt import ChatGPTClient
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
            client.messages.create.return_value = self._mock_response(self._block("text", "x"))
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
        with mock.patch("project.app.services.llm.openai_compatible.httpx.post") as post:
            post.return_value = self._mock_post("Generated copy")
            result = GroqClient(model="some-model").complete("a prompt", max_tokens=42)

        self.assertEqual(result, "Generated copy")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(kwargs["json"]["model"], "some-model")
        self.assertEqual(kwargs["json"]["max_tokens"], 42)
        self.assertEqual(kwargs["json"]["messages"], [{"role": "user", "content": "a prompt"}])

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_complete_raises_when_api_key_missing(self):
        with self.assertRaises(RuntimeError) as ctx:
            GroqClient().complete("a prompt")
        self.assertIn("GROQ_API_KEY", str(ctx.exception))

    def _sent_body(self, client):
        """The request body one ``complete`` call put on the wire."""
        with mock.patch("project.app.services.llm.openai_compatible.httpx.post") as post:
            post.return_value = self._mock_post()
            client.complete("a prompt")
        return post.call_args.kwargs["json"]

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_groqs_default_reasoning_model_asks_for_low_reasoning_effort(self):
        # Groq bills gpt-oss reasoning against max_tokens; at the default effort
        # the whole budget goes to reasoning and the content comes back empty.
        self.assertEqual(self._sent_body(GroqClient())["reasoning_effort"], "low")

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_a_groq_model_without_reasoning_is_sent_no_reasoning_effort(self):
        # Groq answers 400 for the parameter on a non-reasoning model.
        self.assertNotIn("reasoning_effort", self._sent_body(GroqClient(model="allam-2-7b")))

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_reasoning_effort_none_sends_nothing(self):
        self.assertNotIn("reasoning_effort", self._sent_body(GroqClient(reasoning_effort=None)))

    @mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    def test_reasoning_effort_does_not_leak_into_another_provider(self):
        self.assertNotIn("reasoning_effort", self._sent_body(ChatGPTClient()))


# ---------------------------------------------------------------------------
# Configuration resolution: LLM_PROVIDER / LLM_MODEL / the provider's key var
# ---------------------------------------------------------------------------


class ConfigResolutionTests(unittest.TestCase):
    """Nothing but the environment decides which provider runs, on what model,
    with which key -- and a value the app cannot serve says so."""

    def _env(self, **overrides):
        """A clean environment plus ``overrides`` -- no inherited provider key."""
        return mock.patch.dict(os.environ, overrides, clear=True)

    def test_the_default_provider_is_groq(self):
        with self._env():
            self.assertEqual(config.get_provider(), "groq")

    def test_a_blank_value_counts_as_unset(self):
        # What an untouched `.env` line and a compose `${VAR:-}` produce.
        for blank in ("", "   "):
            with self.subTest(blank=blank):
                with self._env(LLM_PROVIDER=blank, LLM_MODEL=blank):
                    self.assertEqual(config.get_provider(), "groq")
                    self.assertIsNone(config.get_model())

    def test_the_provider_is_read_from_the_environment(self):
        with self._env(LLM_PROVIDER="claude"):
            self.assertEqual(config.get_provider(), "claude")

    def test_surrounding_whitespace_is_forgiven(self):
        with self._env(LLM_PROVIDER=" claude ", LLM_MODEL=" some-model "):
            self.assertEqual(config.get_provider(), "claude")
            self.assertEqual(config.get_model(), "some-model")

    def test_an_unsupported_provider_names_the_variable_and_the_choices(self):
        with self._env(LLM_PROVIDER="bogus"):
            with self.assertRaises(ValueError) as ctx:
                config.get_provider()

        message = str(ctx.exception)
        self.assertIn("LLM_PROVIDER", message)
        self.assertIn("bogus", message)
        for choice in ("claude", "chatgpt", "deepseek", "groq"):
            self.assertIn(choice, message)

    def test_no_model_configured_means_the_adapters_own_default(self):
        with self._env(LLM_PROVIDER="groq"):
            self.assertIsNone(config.get_model())
            self.assertEqual(config.get_provider_config("groq"), {})

    def test_the_configured_model_belongs_to_the_active_provider_only(self):
        # A model id is provider-specific: it must not ride along onto another.
        with self._env(LLM_PROVIDER="groq", LLM_MODEL="some-groq-model"):
            self.assertEqual(config.get_provider_config("groq"), {"model": "some-groq-model"})
            self.assertEqual(config.get_provider_config("claude"), {})

    def test_the_key_comes_from_the_providers_own_variable(self):
        with self._env(LLM_PROVIDER="groq", GROQ_API_KEY="groq-key", OPENAI_API_KEY="other-key"):
            self.assertEqual(config.resolve_api_key(), "groq-key")
            self.assertEqual(config.resolve_api_key("chatgpt"), "other-key")
            self.assertIsNone(config.resolve_api_key("deepseek"))

    def test_claude_api_key_is_accepted_as_an_alias(self):
        with self._env(LLM_PROVIDER="claude", CLAUDE_API_KEY="legacy-key"):
            self.assertEqual(config.resolve_api_key(), "legacy-key")

    def test_the_canonical_anthropic_variable_wins_over_the_alias(self):
        with self._env(ANTHROPIC_API_KEY="canonical", CLAUDE_API_KEY="legacy"):
            self.assertEqual(config.resolve_api_key("claude"), "canonical")

    def test_a_missing_key_is_none_rather_than_an_error(self):
        # The adapter raises the auth error at call time, naming its own var.
        with self._env(LLM_PROVIDER="groq"):
            self.assertIsNone(config.resolve_api_key())


class GetLLMClientTests(unittest.TestCase):
    def setUp(self):
        llm._build_client.cache_clear()

    def tearDown(self):
        llm._build_client.cache_clear()

    @mock.patch.dict(os.environ, {"LLM_PROVIDER": "groq", "GROQ_API_KEY": "env-key"}, clear=True)
    def test_the_environment_selects_the_adapter_class_and_its_key(self):
        client = llm.get_llm_client()

        self.assertIsInstance(client, GroqClient)
        self.assertEqual(client.api_key, "env-key")

    @mock.patch.dict(os.environ, {"LLM_PROVIDER": "groq"}, clear=True)
    def test_without_a_configured_model_the_adapters_default_applies(self):
        self.assertEqual(llm.get_llm_client().model, groq_mod.DEFAULT_MODEL)

    @mock.patch.dict(
        os.environ, {"LLM_PROVIDER": "groq", "LLM_MODEL": "configured-model"}, clear=True
    )
    def test_a_configured_model_overrides_the_adapters_default(self):
        self.assertEqual(llm.get_llm_client().model, "configured-model")

    def test_an_environment_change_builds_a_fresh_client(self):
        # The cache is keyed on the whole (provider, model, key) tuple: keying
        # it on the provider alone kept serving a client for the old model.
        with mock.patch.dict(
            os.environ, {"LLM_PROVIDER": "groq", "LLM_MODEL": "model-a", "GROQ_API_KEY": "key-a"}
        ):
            first = llm.get_llm_client()
        with mock.patch.dict(
            os.environ, {"LLM_PROVIDER": "groq", "LLM_MODEL": "model-b", "GROQ_API_KEY": "key-b"}
        ):
            second = llm.get_llm_client()

        self.assertIsNot(second, first)
        self.assertEqual(first.model, "model-a")
        self.assertEqual(second.model, "model-b")
        self.assertEqual(second.api_key, "key-b")

    @mock.patch.dict(os.environ, {"LLM_PROVIDER": "bogus"}, clear=True)
    def test_an_unsupported_provider_never_reaches_a_client(self):
        with self.assertRaises(ValueError) as ctx:
            llm.get_llm_client()
        self.assertIn("bogus", str(ctx.exception))

    def test_build_client_rejects_a_name_with_no_adapter(self):
        # The eval harness names a provider explicitly; an unknown one raises
        # rather than silently generating with the configured provider.
        with self.assertRaises(ValueError) as ctx:
            llm.build_client("nonesuch")
        self.assertIn("nonesuch", str(ctx.exception))


# ---------------------------------------------------------------------------
# Resolved key + per-call timeout (on the F-b/F-c shape)
# ---------------------------------------------------------------------------


class ResolvedKeyAndTimeoutTests(unittest.TestCase):
    """Resolved key and per-call timeout survive complete()'s hop through
    generate() (async half in tests_llm_async.py)."""

    def _ok_post(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"choices": [{"message": {"content": "Generated copy"}}]}
        return response

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "env-key"})
    def test_the_resolved_key_beats_the_providers_env_var(self):
        with mock.patch("project.app.services.llm.openai_compatible.httpx.post") as post:
            post.return_value = self._ok_post()
            GroqClient(api_key="db-key").complete("a prompt")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer db-key")

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "env-key"})
    def test_the_env_var_is_the_fallback_when_nothing_was_resolved(self):
        with mock.patch("project.app.services.llm.openai_compatible.httpx.post") as post:
            post.return_value = self._ok_post()
            GroqClient().complete("a prompt")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer env-key")

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "env-key"})
    def test_a_per_call_timeout_overrides_the_adapters_default(self):
        with mock.patch("project.app.services.llm.openai_compatible.httpx.post") as post:
            post.return_value = self._ok_post()
            GroqClient(timeout_s=60.0).complete("a prompt", timeout=3.5)
        self.assertEqual(post.call_args.kwargs["timeout"], 3.5)

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "env-key"})
    def test_without_an_override_the_adapters_own_timeout_is_used(self):
        with mock.patch("project.app.services.llm.openai_compatible.httpx.post") as post:
            post.return_value = self._ok_post()
            GroqClient(timeout_s=11.0).complete("a prompt")
        self.assertEqual(post.call_args.kwargs["timeout"], 11.0)

    def test_claude_builds_its_sdk_client_once_with_the_resolved_key(self):
        with mock.patch.object(claude_mod.anthropic, "Anthropic") as mock_cls:
            client = mock_cls.return_value
            client.messages.create.return_value = mock.Mock(
                content=[mock.Mock(type="text", text="Hello")]
            )
            adapter = claude_mod.ClaudeClient(api_key="db-key")
            adapter.complete("p")
            adapter.complete("p")

        self.assertEqual(mock_cls.call_count, 1)  # built in __init__, not per call
        self.assertEqual(mock_cls.call_args.kwargs["api_key"], "db-key")

    def test_claude_carries_a_per_call_timeout_on_the_request_only_when_given(self):
        # Passing timeout unconditionally would override the client-level
        # timeout with None and restore the SDK's 600s default by accident.
        with mock.patch.object(claude_mod.anthropic, "Anthropic") as mock_cls:
            client = mock_cls.return_value
            client.messages.create.return_value = mock.Mock(
                content=[mock.Mock(type="text", text="Hello")]
            )
            adapter = claude_mod.ClaudeClient(api_key="db-key", timeout_s=60.0)
            adapter.complete("p")
            self.assertNotIn("timeout", client.messages.create.call_args.kwargs)
            adapter.complete("p", timeout=2.5)
            self.assertEqual(client.messages.create.call_args.kwargs["timeout"], 2.5)


# ---------------------------------------------------------------------------
# Error taxonomy -- pure mapping functions, no network, no mocking
# ---------------------------------------------------------------------------


def _httpx_response(status_code, headers=None, content=None):
    """A real httpx.Response, not a Mock — the mappers read fields a Mock would fake."""
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    return httpx.Response(
        status_code, headers=headers or {}, request=request, content=content or b""
    )


def _anthropic_status_error(cls, status_code, headers=None):
    """Build an anthropic APIStatusError subclass the way the SDK does."""
    response = _httpx_response(status_code, headers)
    return cls("boom", response=response, body=None)


# One table, every surface that turns a provider's HTTP status into our
# taxonomy: (status_code, expected LLMError subclass, retryable, the anthropic
# SDK class that carries that status -- None where the SDK names none).
# `retryable` is spelled per row on purpose, not derived from the class.
STATUS_TABLE = (
    (400, errors.LLMBadRequestError, False, anthropic.BadRequestError),
    (401, errors.LLMAuthError, False, anthropic.AuthenticationError),
    (403, errors.LLMAuthError, False, anthropic.PermissionDeniedError),
    (404, errors.LLMBadRequestError, False, anthropic.NotFoundError),
    (408, errors.LLMTimeoutError, True, None),
    (409, errors.LLMBadRequestError, False, anthropic.ConflictError),
    (413, errors.LLMBadRequestError, False, anthropic.RequestTooLargeError),
    (422, errors.LLMBadRequestError, False, anthropic.UnprocessableEntityError),
    (425, errors.LLMTransientError, True, None),
    (429, errors.LLMRateLimitError, True, anthropic.RateLimitError),
    (500, errors.LLMTransientError, True, anthropic.InternalServerError),
    (502, errors.LLMTransientError, True, None),
    (503, errors.LLMTransientError, True, None),
    (529, errors.LLMTransientError, True, anthropic.OverloadedError),
)

RETRYABLE_CLASSES = (
    errors.LLMRateLimitError,
    errors.LLMTimeoutError,
    errors.LLMTransientError,
)


def _httpx_status_error(status_code, headers=None):
    response = _httpx_response(status_code, headers)
    return httpx.HTTPStatusError("boom", request=response.request, response=response)


class ErrorTaxonomyTests(unittest.TestCase):
    """Shape of the taxonomy itself, independent of any mapping."""

    def test_llm_error_is_a_runtime_error(self):
        # Keeps every existing `assertRaises(RuntimeError)` caller working.
        self.assertTrue(issubclass(errors.LLMError, RuntimeError))

    def test_retryability_is_declared_per_class(self):
        self.assertFalse(errors.LLMError.retryable)
        for cls in RETRYABLE_CLASSES:
            self.assertTrue(cls.retryable, cls.__name__)
        for cls in (
            errors.LLMAuthError,
            errors.LLMBadRequestError,
            errors.LLMMalformedResponseError,
        ):
            self.assertFalse(cls.retryable, cls.__name__)

    def test_carries_provider_status_and_cause(self):
        cause = ValueError("underlying")
        exc = errors.LLMTransientError(
            "boom", provider="groq", status_code=503, retry_after=1.5, cause=cause
        )
        self.assertEqual(str(exc), "boom")
        self.assertEqual(exc.provider, "groq")
        self.assertEqual(exc.status_code, 503)
        self.assertEqual(exc.retry_after, 1.5)
        self.assertIs(exc.cause, cause)

    def test_defaults_are_none(self):
        exc = errors.LLMError("boom")
        self.assertIsNone(exc.provider)
        self.assertIsNone(exc.status_code)
        self.assertIsNone(exc.retry_after)
        self.assertIsNone(exc.cause)

    def test_unmeasured_error_latency_is_none(self):
        # Errors raised before any call started must not claim a zero-second call.
        self.assertIsNone(errors.LLMAuthError("no key").latency_s)

    def test_taxonomy_is_re_exported_from_the_package(self):
        self.assertIs(llm.LLMError, errors.LLMError)
        self.assertIs(llm.LLMRateLimitError, errors.LLMRateLimitError)


class StatusCodeMappingTests(unittest.TestCase):
    """STATUS_TABLE, walked on all three surfaces it has to hold for: the httpx
    mapper, the anthropic mapper (SDK-named subclass and the generic
    ``APIStatusError`` a future SDK may hand us), and the adapter that has to
    raise the mapped class rather than the vendor's."""

    def test_both_mappers_agree_on_every_status_code(self):
        for status_code, expected, retryable, sdk_cls in STATUS_TABLE:
            with self.subTest(status_code=status_code):
                httpx_exc = _httpx_status_error(status_code)
                mapped = errors.map_httpx_error(httpx_exc, "groq")
                self.assertIsInstance(mapped, expected)
                self.assertEqual(mapped.status_code, status_code)
                self.assertEqual(mapped.provider, "groq")
                self.assertIs(mapped.cause, httpx_exc)
                self.assertEqual(mapped.retryable, retryable)

                # An SDK subclass we don't enumerate must land by status code.
                generic = _anthropic_status_error(anthropic.APIStatusError, status_code)
                mapped = errors.map_anthropic_error(generic, "claude")
                self.assertIsInstance(mapped, expected)
                self.assertEqual(mapped.status_code, status_code)
                self.assertEqual(mapped.retryable, retryable)

                if sdk_cls is None:
                    continue
                sdk_exc = _anthropic_status_error(sdk_cls, status_code)
                mapped = errors.map_anthropic_error(sdk_exc, "claude")
                self.assertIsInstance(mapped, expected)
                self.assertEqual(mapped.provider, "claude")
                self.assertIs(mapped.cause, sdk_exc)
                self.assertEqual(mapped.retryable, isinstance(mapped, RETRYABLE_CLASSES))

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_the_adapter_raises_the_mapped_class_for_every_status_code(self):
        for status_code, expected, retryable, _sdk_cls in STATUS_TABLE:
            with self.subTest(status_code=status_code):
                with mock.patch("project.app.services.llm.openai_compatible.httpx.post") as post:
                    post.return_value = _httpx_response(status_code)
                    with self.assertRaises(expected) as ctx:
                        GroqClient().complete("a prompt")

                self.assertEqual(ctx.exception.retryable, retryable)
                # A failure has a duration too -- the same measurement
                # LLMResult.latency_s records.
                self.assertIsNotNone(ctx.exception.latency_s)
                self.assertGreaterEqual(ctx.exception.latency_s, 0.0)

    def test_a_non_error_status_falls_back_to_the_base_class(self):
        # The mapper must not silently call a redirect "transient".
        mapped = errors.map_httpx_error(_httpx_status_error(304), "groq")
        self.assertIs(type(mapped), errors.LLMError)
        self.assertFalse(mapped.retryable)

    def test_an_unenumerated_4xx_is_a_non_retryable_bad_request(self):
        mapped = errors.map_httpx_error(_httpx_status_error(451), "groq")
        self.assertIsInstance(mapped, errors.LLMBadRequestError)
        self.assertFalse(mapped.retryable)


class NonStatusMappingTests(unittest.TestCase):
    """Everything that never carries a status code: transport failures, unusable
    response shapes and the residue. One row per exception, both mappers.

    An expected class of ``LLMError`` means *exactly* that class -- the
    non-retryable base, not one of its buckets.
    """

    _REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    HTTPX_CASES = (
        # Every timeout flavour is a timeout, never "transient".
        (httpx.TimeoutException("slow"), errors.LLMTimeoutError),
        (httpx.ConnectTimeout("slow"), errors.LLMTimeoutError),
        (httpx.ReadTimeout("slow"), errors.LLMTimeoutError),
        (httpx.WriteTimeout("slow"), errors.LLMTimeoutError),
        (httpx.PoolTimeout("slow"), errors.LLMTimeoutError),
        # Transport failures are worth another attempt.
        (httpx.ConnectError("refused"), errors.LLMTransientError),
        (httpx.ReadError("reset"), errors.LLMTransientError),
        (httpx.RemoteProtocolError("bad frame"), errors.LLMTransientError),
        # A 200 whose body is not the shape the adapter parses.
        (json.JSONDecodeError("no json", "<html>", 0), errors.LLMMalformedResponseError),
        (KeyError("choices"), errors.LLMMalformedResponseError),
        (IndexError("list index out of range"), errors.LLMMalformedResponseError),
        (TypeError("'NoneType' object is not subscriptable"), errors.LLMMalformedResponseError),
        (AttributeError("'int' object has no attribute 'strip'"), errors.LLMMalformedResponseError),
        # Residue. InvalidURL is deliberately here: it derives from Exception,
        # NOT from httpx.HTTPError, so it only reaches the taxonomy because the
        # adapter names it explicitly in its except clause.
        (httpx.TooManyRedirects("looping"), errors.LLMError),
        (httpx.InvalidURL("bad base_url"), errors.LLMError),
        (ValueError("something else entirely"), errors.LLMError),
    )

    ANTHROPIC_CASES = (
        # REGRESSION GUARD: APITimeoutError subclasses APIConnectionError, so a
        # wrong isinstance order silently classifies every timeout as transient.
        (anthropic.APITimeoutError(_REQUEST), errors.LLMTimeoutError),
        (anthropic.APIConnectionError(request=_REQUEST), errors.LLMTransientError),
        # RetryableError subclasses AnthropicError, not APIError, so without an
        # explicit branch it would land on the non-retryable base.
        (anthropic.RetryableError("try again"), errors.LLMTransientError),
        (
            anthropic.APIResponseValidationError(response=_httpx_response(200), body=None),
            errors.LLMMalformedResponseError,
        ),
        # Residue, including a type that never came from the SDK: the mapper
        # takes BaseException and must not blow up on one.
        (anthropic.AnthropicError("odd"), errors.LLMError),
        (TypeError("could not resolve auth"), errors.LLMError),
    )

    def test_every_non_status_failure_lands_in_its_declared_bucket(self):
        surfaces = (
            (errors.map_httpx_error, "groq", self.HTTPX_CASES),
            (errors.map_anthropic_error, "claude", self.ANTHROPIC_CASES),
        )
        for mapper, provider, cases in surfaces:
            for exc, expected in cases:
                with self.subTest(mapper=mapper.__name__, exc=type(exc).__name__):
                    mapped = mapper(exc, provider)
                    if expected is errors.LLMError:
                        self.assertIs(type(mapped), errors.LLMError)
                    else:
                        self.assertIsInstance(mapped, expected)
                    # The buckets stay disjoint: the retry policy reads the class.
                    if expected is errors.LLMTimeoutError:
                        self.assertNotIsInstance(mapped, errors.LLMTransientError)
                    self.assertEqual(mapped.provider, provider)
                    self.assertEqual(mapped.retryable, expected.retryable)


class RetryAfterTests(unittest.TestCase):
    """The only header the taxonomy reads, on both mappers."""

    def test_it_is_parsed_from_the_header_and_absent_without_one(self):
        with_header = errors.map_httpx_error(_httpx_status_error(429, {"Retry-After": "30"}), "g")
        self.assertEqual(with_header.retry_after, 30.0)
        self.assertIsNone(errors.map_httpx_error(_httpx_status_error(429), "g").retry_after)

        anthropic_with = _anthropic_status_error(
            anthropic.RateLimitError, 429, headers={"retry-after": "12.5"}
        )
        self.assertEqual(errors.map_anthropic_error(anthropic_with, "claude").retry_after, 12.5)
        anthropic_without = _anthropic_status_error(anthropic.RateLimitError, 429)
        self.assertIsNone(errors.map_anthropic_error(anthropic_without, "claude").retry_after)

    def test_unusable_values_are_ignored(self):
        # Date-form or negative values degrade to "no guidance", not a bad sleep().
        for raw in ("Wed, 21 Oct 2015 07:28:00 GMT", "-5", "", "soon", "nan", "inf"):
            with self.subTest(raw=raw):
                mapped = errors.map_httpx_error(_httpx_status_error(429, {"Retry-After": raw}), "g")
                self.assertIsNone(mapped.retry_after)

    def test_an_absurd_value_is_clamped(self):
        # base_url is operator-configurable; a proxy must not park a worker.
        mapped = errors.map_httpx_error(_httpx_status_error(429, {"Retry-After": "86400000"}), "g")
        self.assertEqual(mapped.retry_after, errors.MAX_RETRY_AFTER_SECONDS)

    def test_unreadable_headers_do_not_break_the_mapper(self):
        # `headers` is read with getattr; a bad value must degrade to "no
        # guidance", never to an exception raised from inside the mapper.
        exc = anthropic.AnthropicError("odd")
        exc.response = types.SimpleNamespace(headers=object())
        self.assertIsNone(errors.map_anthropic_error(exc, "claude").retry_after)

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_the_adapter_carries_it_out_of_a_rate_limited_call(self):
        with mock.patch("project.app.services.llm.openai_compatible.httpx.post") as post:
            post.return_value = _httpx_response(429, headers={"Retry-After": "7"})
            with self.assertRaises(errors.LLMRateLimitError) as ctx:
                GroqClient().complete("a prompt")
        self.assertEqual(ctx.exception.retry_after, 7.0)


class MappedMessageTests(unittest.TestCase):
    """What the reviewer-facing message may and may not contain."""

    def test_the_providers_own_error_message_survives_into_the_mapped_error(self):
        response = _httpx_response(
            400,
            content=json.dumps(
                {"error": {"message": "Failed to call a function.", "code": "tool_use_failed"}}
            ),
        )
        exc = httpx.HTTPStatusError("boom", request=response.request, response=response)
        mapped = errors.map_httpx_error(exc, "groq")
        self.assertIn("Failed to call a function.", str(mapped))
        self.assertIsInstance(mapped, errors.LLMBadRequestError)

    def test_an_oversized_body_is_truncated_rather_than_persisted_whole(self):
        """A 200KB proxy error page must not land in the reviewer-facing TextField."""
        response = _httpx_response(502, content="<html>" + "x" * 50_000 + "</html>")
        exc = httpx.HTTPStatusError("boom", request=response.request, response=response)
        self.assertLess(len(str(errors.map_httpx_error(exc, "groq"))), 1_000)

    def test_an_unreadable_body_degrades_instead_of_raising(self):
        """An unread streaming body (`.text` raises) degrades instead of raising."""
        response = httpx.Response(
            503,
            request=httpx.Request("POST", "https://example.test/v1/chat/completions"),
            stream=httpx.ByteStream(b"unread"),
        )
        exc = httpx.HTTPStatusError("boom", request=response.request, response=response)
        mapped = errors.map_httpx_error(exc, "groq")
        self.assertIsInstance(mapped, errors.LLMTransientError)
        self.assertIn("boom", str(mapped))


# ---------------------------------------------------------------------------
# Adapters raise the taxonomy, not vendor exceptions
# ---------------------------------------------------------------------------


class AdapterErrorTranslationTests(unittest.TestCase):
    """The failures an adapter raises without any HTTP status to map."""

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_missing_key_is_a_non_retryable_auth_error(self):
        with self.assertRaises(errors.LLMAuthError) as ctx:
            GroqClient().complete("a prompt")
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(ctx.exception.provider, "groq")

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_an_unusable_completion_body_is_malformed(self):
        payloads = (
            {},
            {"choices": []},
            {"choices": [{"message": {"content": None}}]},
            {"choices": [{"message": {"content": "   \n "}}]},  # whitespace only
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                response = mock.Mock()
                response.raise_for_status.return_value = None
                response.json.return_value = payload
                with mock.patch("project.app.services.llm.openai_compatible.httpx.post") as post:
                    post.return_value = response
                    with self.assertRaises(errors.LLMMalformedResponseError):
                        GroqClient().complete("a prompt")

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_claude_missing_key_is_a_non_retryable_auth_error(self):
        # default_credentials() is patched off: clearing os.environ is not
        # enough -- the SDK also reads the active profile from disk, so a
        # logged-in developer machine would resolve a credential anyway.
        with (
            mock.patch.object(
                claude_mod.anthropic._client, "default_credentials", return_value=None
            ),
            self.assertRaises(errors.LLMAuthError) as ctx,
        ):
            claude_mod.ClaudeClient().complete("p")
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(ctx.exception.provider, "claude")
        self.assertIn("ANTHROPIC_API_KEY", str(ctx.exception))

    def test_claude_sdk_error_becomes_a_taxonomy_error(self):
        with mock.patch.object(claude_mod.anthropic, "Anthropic") as mock_cls:
            client = mock_cls.return_value
            client.messages.create.side_effect = _anthropic_status_error(
                anthropic.InternalServerError, 500
            )
            with self.assertRaises(errors.LLMTransientError) as ctx:
                claude_mod.ClaudeClient().complete("p")
        self.assertEqual(ctx.exception.provider, "claude")
        self.assertTrue(ctx.exception.retryable)

    def test_claude_raw_httpx_error_is_still_typed(self):
        # Insurance branch: the SDK normally wraps transport failures itself.
        with mock.patch.object(claude_mod.anthropic, "Anthropic") as mock_cls:
            client = mock_cls.return_value
            client.messages.create.side_effect = httpx.ConnectError("refused")
            with self.assertRaises(errors.LLMTransientError):
                claude_mod.ClaudeClient().complete("p")

    def test_claude_response_with_no_text_block_is_malformed(self):
        with mock.patch.object(claude_mod.anthropic, "Anthropic") as mock_cls:
            client = mock_cls.return_value
            response = mock.Mock()
            response.content = []
            client.messages.create.return_value = response
            with self.assertRaises(errors.LLMMalformedResponseError):
                claude_mod.ClaudeClient().complete("p")


# ---------------------------------------------------------------------------
# LLMResult: usage, model and finish reason survive the adapter
# ---------------------------------------------------------------------------


class NormalizeFinishReasonTests(unittest.TestCase):
    def test_both_provider_vocabularies_collapse_onto_ours(self):
        table = (
            # Anthropic stop_reason
            ("end_turn", base.FINISH_STOP),
            ("stop_sequence", base.FINISH_STOP),
            ("max_tokens", base.FINISH_LENGTH),
            ("tool_use", base.FINISH_TOOL_CALLS),
            ("refusal", base.FINISH_CONTENT_FILTER),
            # OpenAI-compatible finish_reason
            ("stop", base.FINISH_STOP),
            ("length", base.FINISH_LENGTH),
            ("content_filter", base.FINISH_CONTENT_FILTER),
            ("tool_calls", base.FINISH_TOOL_CALLS),
            ("function_call", base.FINISH_TOOL_CALLS),
        )
        for raw, expected in table:
            with self.subTest(raw=raw):
                self.assertEqual(base.normalize_finish_reason(raw), expected)

    def test_unknown_reason_is_none_not_an_error(self):
        # A provider's new reason (Anthropic shipped "pause_turn" post-map) must
        # not read as an error; the raw string is kept regardless.
        for raw in ("pause_turn", "something_new", "", None, 7):
            with self.subTest(raw=raw):
                self.assertIsNone(base.normalize_finish_reason(raw))


class CoerceTokenCountTests(unittest.TestCase):
    def test_real_counts_including_zero_are_preserved(self):
        # A reported 0 is a measurement; only an OMITTED count becomes None.
        self.assertEqual(base.coerce_token_count(0), 0)
        self.assertEqual(base.coerce_token_count(1234), 1234)

    def test_anything_that_is_not_a_count_is_none(self):
        for value in (None, "512", 12.5, mock.Mock(), True, False, -1):
            with self.subTest(value=repr(value)):
                self.assertIsNone(base.coerce_token_count(value))


class LLMResultTests(unittest.TestCase):
    def test_result_is_immutable(self):
        result = base.LLMResult(text="hi", provider="groq", model="m")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.text = "tampered"

    def test_unmeasured_latency_is_none_not_zero(self):
        # Must not claim a real observation of a zero-second call.
        self.assertIsNone(base.LLMResult(text="hi", provider="groq", model="m").latency_s)

    def test_complete_is_a_thin_text_wrapper_over_generate(self):
        # complete() stays sync and returns a plain str, sourced from generate().
        class _Stub(base.LLMClient):
            provider_name = "stub"

            def generate(self, prompt, max_tokens=None, timeout=None):
                return base.LLMResult(text="the copy", provider="stub", model="m", input_tokens=7)

        client = _Stub(model="m")
        text = client.complete("p", max_tokens=9)
        self.assertEqual(text, "the copy")
        self.assertIsInstance(text, str)

    def test_base_agenerate_refuses_rather_than_faking_async(self):
        # No silent thread-pool fallback -- it would cap concurrency at pool size.
        class _Stub(base.LLMClient):
            provider_name = "stub"

            def generate(self, prompt, max_tokens=None, timeout=None):
                return base.LLMResult(text="x", provider="stub", model="m")

        with self.assertRaises(NotImplementedError) as ctx:
            asyncio.run(_Stub(model="m").agenerate("p"))
        self.assertIn("_Stub", str(ctx.exception))

    def test_acomplete_is_the_text_only_wrapper_over_agenerate(self):
        # Mirrors complete()/generate().
        class _AsyncStub(base.LLMClient):
            provider_name = "stub"

            def generate(self, prompt, max_tokens=None, timeout=None):  # pragma: no cover - unused
                raise AssertionError("acomplete must not fall back to the sync path")

            async def agenerate(self, prompt, max_tokens=None, timeout=None):
                return base.LLMResult(text="async copy", provider="stub", model="m")

        self.assertEqual(asyncio.run(_AsyncStub(model="m").acomplete("p")), "async copy")


class ClaudeResultTests(unittest.TestCase):
    def _response(self, *, text="Subject: Hi\n\nBody", **overrides):
        response = mock.Mock()
        block = mock.Mock()
        block.type = "text"
        block.text = text
        response.content = [block]
        for key, value in overrides.items():
            setattr(response, key, value)
        return response

    def _generate(self, response):
        with mock.patch.object(claude_mod.anthropic, "Anthropic") as mock_cls:
            client = mock_cls.return_value
            client.messages.create.return_value = response
            return claude_mod.ClaudeClient(model="claude-requested").generate("p")

    def test_extracts_usage_model_and_finish_reason(self):
        usage = mock.Mock(input_tokens=1200, output_tokens=310)
        result = self._generate(
            self._response(
                usage=usage,
                model="claude-sonnet-4-6-20260101",
                stop_reason="end_turn",
            )
        )
        self.assertEqual(result.text, "Subject: Hi\n\nBody")
        self.assertEqual(result.provider, "claude")
        # Requested model vs what the provider says it served -- both kept.
        self.assertEqual(result.model, "claude-requested")
        self.assertEqual(result.response_model, "claude-sonnet-4-6-20260101")
        self.assertEqual(result.input_tokens, 1200)
        self.assertEqual(result.output_tokens, 310)
        self.assertEqual(result.raw_finish_reason, "end_turn")
        self.assertEqual(result.finish_reason, base.FINISH_STOP)
        self.assertGreaterEqual(result.latency_s, 0.0)

    def test_truncated_generation_normalizes_to_length(self):
        usage = mock.Mock(input_tokens=1, output_tokens=500)
        result = self._generate(self._response(usage=usage, stop_reason="max_tokens"))
        self.assertEqual(result.finish_reason, base.FINISH_LENGTH)
        self.assertEqual(result.raw_finish_reason, "max_tokens")

    def test_absent_usage_yields_none_not_zero(self):
        result = self._generate(self._response(usage=None, model=None, stop_reason=None))
        self.assertIsNone(result.input_tokens)
        self.assertIsNone(result.output_tokens)
        self.assertIsNone(result.response_model)
        self.assertIsNone(result.finish_reason)
        self.assertIsNone(result.raw_finish_reason)
        # "No observation", not an observation of zero.


class ClaudeSdkShapeTests(unittest.TestCase):
    """Pin the extraction against the SDK's REAL response types — Mocks would
    stay green if ``anthropic`` renamed ``Usage.input_tokens``."""

    def _message(self, **overrides):
        fields = {
            "id": "msg_01",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6-20260101",
            "stop_reason": "end_turn",
            "content": [anthropic.types.TextBlock(type="text", text="Subject: Hi\n\nBody")],
            "usage": anthropic.types.Usage(
                input_tokens=1200,
                output_tokens=310,
                cache_read_input_tokens=64,
                cache_creation_input_tokens=8,
            ),
        }
        fields.update(overrides)
        return anthropic.types.Message(**fields)

    def test_extraction_matches_the_real_sdk_response_type(self):
        result = claude_mod.ClaudeClient(model="claude-requested")._build_result(
            self._message(), latency_s=0.25
        )
        self.assertEqual(result.text, "Subject: Hi\n\nBody")
        self.assertEqual(result.response_model, "claude-sonnet-4-6-20260101")
        self.assertEqual(result.input_tokens, 1200)
        self.assertEqual(result.output_tokens, 310)
        self.assertEqual(result.cache_read_tokens, 64)
        self.assertEqual(result.cache_write_tokens, 8)
        self.assertEqual(result.raw_finish_reason, "end_turn")
        self.assertEqual(result.finish_reason, base.FINISH_STOP)
        self.assertEqual(result.latency_s, 0.25)

    def test_a_dumped_response_reads_identically(self):
        # with_raw_response / model_dump() turn the SDK's models into plain dicts.
        dumped = self._message().model_dump()
        result = claude_mod.ClaudeClient(model="m")._build_result(dumped, latency_s=0.1)
        self.assertEqual(result.text, "Subject: Hi\n\nBody")
        self.assertEqual(result.input_tokens, 1200)
        self.assertEqual(result.response_model, "claude-sonnet-4-6-20260101")
        self.assertEqual(result.finish_reason, base.FINISH_STOP)


class OpenAICompatibleResultTests(unittest.TestCase):
    def _generate(self, payload, model="some-model"):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = payload
        with mock.patch("project.app.services.llm.openai_compatible.httpx.post") as post:
            post.return_value = response
            return GroqClient(model=model).generate("a prompt")

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_extracts_usage_model_and_finish_reason(self):
        result = self._generate(
            {
                "model": "openai/gpt-oss-20b-0000",
                "usage": {"prompt_tokens": 980, "completion_tokens": 142},
                "choices": [
                    {"message": {"content": "Generated copy"}, "finish_reason": "stop"},
                ],
            }
        )
        self.assertEqual(result.text, "Generated copy")
        self.assertEqual(result.provider, "groq")
        self.assertEqual(result.model, "some-model")
        self.assertEqual(result.response_model, "openai/gpt-oss-20b-0000")
        self.assertEqual(result.input_tokens, 980)
        self.assertEqual(result.output_tokens, 142)
        self.assertEqual(result.raw_finish_reason, "stop")
        self.assertEqual(result.finish_reason, base.FINISH_STOP)
        self.assertGreaterEqual(result.latency_s, 0.0)

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_missing_metadata_degrades_to_none_not_an_error(self):
        # The minimal legal body: metadata is best-effort, only the text is contractual.
        result = self._generate({"choices": [{"message": {"content": "Generated copy"}}]})
        self.assertEqual(result.text, "Generated copy")
        self.assertIsNone(result.input_tokens)
        self.assertIsNone(result.output_tokens)
        self.assertIsNone(result.response_model)
        self.assertIsNone(result.finish_reason)
        self.assertIsNone(result.raw_finish_reason)

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_unusable_metadata_shapes_do_not_break_a_good_completion(self):
        # Neither shape is a reason to throw away a valid email.
        for payload in (
            {
                "usage": None,
                "model": None,
                "choices": [{"message": {"content": "Generated copy"}}],
            },
            # Mapping-keyed "choices": the text chain still resolves, so the
            # metadata readers must not be the thing that fails.
            {"choices": {0: {"message": {"content": "Generated copy"}}}},
        ):
            with self.subTest(payload=payload):
                result = self._generate(payload)
                self.assertEqual(result.text, "Generated copy")
                self.assertIsNone(result.input_tokens)
                self.assertIsNone(result.response_model)

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_half_a_usage_block_keeps_the_half_that_is_there(self):
        # A partial block must not cost us the count the provider DID send.
        result = self._generate(
            {
                "usage": {"completion_tokens": 12},
                "choices": [{"message": {"content": "Generated copy"}, "finish_reason": None}],
            }
        )
        self.assertEqual(result.output_tokens, 12)
        self.assertIsNone(result.input_tokens)
        self.assertIsNone(result.raw_finish_reason)

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_cached_prompt_tokens_are_read_from_the_details_block(self):
        result = self._generate(
            {
                "usage": {
                    "prompt_tokens": 980,
                    "completion_tokens": 12,
                    "prompt_tokens_details": {"cached_tokens": 512},
                },
                "choices": [{"message": {"content": "Generated copy"}}],
            }
        )
        self.assertEqual(result.cache_read_tokens, 512)
        # OpenAI-compatible providers count cached tokens WITHIN prompt_tokens
        # (unlike Anthropic's), so the two are not additive.
        self.assertEqual(result.input_tokens, 980)
        self.assertIsNone(result.cache_write_tokens)

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_reported_zero_tokens_is_kept_as_zero(self):
        result = self._generate(
            {
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                "choices": [{"message": {"content": "Generated copy"}}],
            }
        )
        self.assertEqual(result.input_tokens, 0)
        self.assertEqual(result.output_tokens, 0)

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_content_filter_finish_reason_is_normalized(self):
        result = self._generate(
            {
                "choices": [
                    {"message": {"content": "Generated copy"}, "finish_reason": "content_filter"},
                ]
            }
        )
        self.assertEqual(result.finish_reason, base.FINISH_CONTENT_FILTER)


if __name__ == "__main__":
    unittest.main()
