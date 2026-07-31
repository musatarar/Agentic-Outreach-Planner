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
from cryptography.fernet import Fernet
from django.test import TestCase
from rest_framework import status

from project.app import checks as app_checks
from project.app.models import LLMConfiguration, LLMModel, LLMProvider
from project.app.services import crypto, llm
from project.app.services.llm import base, config, errors
from project.app.services.llm import claude as claude_mod
from project.app.services.llm.groq import GroqClient
from project.app.tests_auth_utils import AuthenticatedAPITestCase

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
            mock.patch.object(config, "resolve_active_key", return_value=(None, "none")),
        ):
            client = llm.get_llm_client()

        self.assertIsInstance(client, GroqClient)
        self.assertEqual(client.model, "configured-model")
        self.assertEqual(client.default_max_tokens, 256)

    def test_unknown_provider_raises(self):
        with (
            mock.patch.object(config, "get_provider", return_value="bogus"),
            mock.patch.object(config, "get_provider_config", return_value={}),
            mock.patch.object(config, "resolve_active_key", return_value=(None, "none")),
        ):
            with self.assertRaises(ValueError) as ctx:
                llm.get_llm_client()
        self.assertIn("bogus", str(ctx.exception))


# ---------------------------------------------------------------------------
# Resolved key + per-call timeout (MUS-32's contract, on the F-b/F-c shape)
# ---------------------------------------------------------------------------


class ResolvedKeyAndTimeoutTests(unittest.TestCase):
    """The two things the database-backed config layer asks of an adapter.

    MUS-32 moved key resolution into the database and gave ``complete()`` a
    per-call ``timeout`` so the config "test connection" endpoint can fail fast
    instead of hanging. ``complete()`` is now a wrapper over ``generate()``, so
    both travel one hop further than they used to; these pin that neither got
    dropped on the way. The async half lives in ``tests_llm_async.py``.
    """

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
        # It has to ride on the request rather than the client, because the
        # client is now built once. Passing it unconditionally would override
        # the client-level timeout with None on every ordinary call and restore
        # the SDK's 600-second default by accident.
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
# Error taxonomy (MUS-43) -- pure mapping functions, no network, no mocking
# ---------------------------------------------------------------------------


def _httpx_response(status_code, headers=None):
    """A real httpx.Response, not a Mock.

    The mappers read ``.status_code`` and ``.headers``; a Mock would satisfy
    those reads with values httpx itself would never produce, so the table
    below would be testing our test doubles rather than the mapping. (Mocks are
    still used further down where the *body* is the thing under test.)
    """
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    return httpx.Response(status_code, headers=headers or {}, request=request)


def _anthropic_status_error(cls, status_code, headers=None):
    """Build an anthropic APIStatusError subclass the way the SDK does."""
    response = _httpx_response(status_code, headers)
    return cls("boom", response=response, body=None)


# (status_code, expected LLMError subclass, retryable) -- the table both mappers
# share. `retryable` is spelled out per row rather than derived from the class,
# so the row is an independent statement of intent and not a restatement of the
# class attribute it is meant to check.
STATUS_TABLE = (
    (400, errors.LLMBadRequestError, False),
    (401, errors.LLMAuthError, False),
    (403, errors.LLMAuthError, False),
    (404, errors.LLMBadRequestError, False),
    (408, errors.LLMTimeoutError, True),
    (409, errors.LLMBadRequestError, False),
    (413, errors.LLMBadRequestError, False),
    (422, errors.LLMBadRequestError, False),
    (425, errors.LLMTransientError, True),
    (429, errors.LLMRateLimitError, True),
    (500, errors.LLMTransientError, True),
    (502, errors.LLMTransientError, True),
    (503, errors.LLMTransientError, True),
    (529, errors.LLMTransientError, True),
)

RETRYABLE_CLASSES = (
    errors.LLMRateLimitError,
    errors.LLMTimeoutError,
    errors.LLMTransientError,
)


class ErrorTaxonomyTests(unittest.TestCase):
    """Shape of the taxonomy itself, independent of any mapping."""

    def test_llm_error_is_a_runtime_error(self):
        # Load-bearing: openai_compatible's missing-key raise becomes
        # LLMAuthError and every existing `assertRaises(RuntimeError)` caller
        # keeps working. If this ever fails, the base class is wrong.
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

    def test_taxonomy_is_re_exported_from_the_package(self):
        self.assertIs(llm.LLMError, errors.LLMError)
        self.assertIs(llm.LLMRateLimitError, errors.LLMRateLimitError)


class HttpxErrorMappingTests(unittest.TestCase):
    def test_status_codes_map_to_expected_classes(self):
        for status_code, expected, retryable in STATUS_TABLE:
            with self.subTest(status_code=status_code):
                response = _httpx_response(status_code)
                exc = httpx.HTTPStatusError("boom", request=response.request, response=response)
                mapped = errors.map_httpx_error(exc, "groq")
                self.assertIsInstance(mapped, expected)
                self.assertEqual(mapped.status_code, status_code)
                self.assertEqual(mapped.provider, "groq")
                self.assertIs(mapped.cause, exc)
                self.assertEqual(mapped.retryable, retryable)

    def test_non_error_status_falls_back_to_the_base_class(self):
        # raise_for_status() never produces a 3xx, but the mapper takes whatever
        # it is handed and must not silently call a redirect "transient".
        response = _httpx_response(304)
        exc = httpx.HTTPStatusError("boom", request=response.request, response=response)
        mapped = errors.map_httpx_error(exc, "groq")
        self.assertIs(type(mapped), errors.LLMError)
        self.assertFalse(mapped.retryable)

    def test_unenumerated_4xx_is_a_non_retryable_bad_request(self):
        response = _httpx_response(451)
        exc = httpx.HTTPStatusError("boom", request=response.request, response=response)
        mapped = errors.map_httpx_error(exc, "groq")
        self.assertIsInstance(mapped, errors.LLMBadRequestError)
        self.assertFalse(mapped.retryable)

    def test_retry_after_is_parsed_from_the_header(self):
        response = _httpx_response(429, headers={"Retry-After": "30"})
        exc = httpx.HTTPStatusError("boom", request=response.request, response=response)
        self.assertEqual(errors.map_httpx_error(exc, "groq").retry_after, 30.0)

    def test_retry_after_is_none_without_the_header(self):
        response = _httpx_response(429)
        exc = httpx.HTTPStatusError("boom", request=response.request, response=response)
        self.assertIsNone(errors.map_httpx_error(exc, "groq").retry_after)

    def _rate_limited(self, retry_after):
        response = _httpx_response(429, headers={"Retry-After": retry_after})
        return httpx.HTTPStatusError("boom", request=response.request, response=response)

    def test_unusable_retry_after_values_are_ignored(self):
        # A date-form (legal per RFC 9110, never sent by an LLM provider) or a
        # negative value must degrade to "no guidance", not to a bad sleep().
        for raw in ("Wed, 21 Oct 2015 07:28:00 GMT", "-5", "", "soon", "nan", "inf"):
            with self.subTest(raw=raw):
                exc = self._rate_limited(raw)
                self.assertIsNone(errors.map_httpx_error(exc, "groq").retry_after)

    def test_absurd_retry_after_is_clamped(self):
        # base_url is operator-configurable, so a proxy answering with a
        # millennium must not be able to park a worker for one.
        mapped = errors.map_httpx_error(self._rate_limited("86400000"), "groq")
        self.assertEqual(mapped.retry_after, errors.MAX_RETRY_AFTER_SECONDS)

    def test_timeouts_map_to_timeout_not_transient(self):
        for cls in (
            httpx.TimeoutException,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
        ):
            with self.subTest(cls=cls.__name__):
                mapped = errors.map_httpx_error(cls("slow"), "groq")
                self.assertIsInstance(mapped, errors.LLMTimeoutError)
                self.assertTrue(mapped.retryable)

    def test_transport_errors_are_transient(self):
        for exc in (
            httpx.ConnectError("refused"),
            httpx.ReadError("reset"),
            httpx.RemoteProtocolError("bad frame"),
        ):
            with self.subTest(cls=type(exc).__name__):
                mapped = errors.map_httpx_error(exc, "groq")
                self.assertIsInstance(mapped, errors.LLMTransientError)
                self.assertTrue(mapped.retryable)

    def test_response_shape_failures_are_malformed(self):
        for exc in (
            json.JSONDecodeError("no json", "<html>", 0),
            KeyError("choices"),
            IndexError("list index out of range"),
            TypeError("'NoneType' object is not subscriptable"),
            AttributeError("'int' object has no attribute 'strip'"),
        ):
            with self.subTest(cls=type(exc).__name__):
                mapped = errors.map_httpx_error(exc, "groq")
                self.assertIsInstance(mapped, errors.LLMMalformedResponseError)
                self.assertFalse(mapped.retryable)

    def test_residual_and_non_httpx_exceptions_are_the_non_retryable_base(self):
        # InvalidURL is deliberately in this list: it derives from Exception,
        # NOT from httpx.HTTPError, so it only reaches the taxonomy because the
        # adapter names it explicitly in its except clause.
        for exc in (
            httpx.TooManyRedirects("looping"),
            httpx.InvalidURL("bad base_url"),
            ValueError("something else entirely"),
        ):
            with self.subTest(cls=type(exc).__name__):
                mapped = errors.map_httpx_error(exc, "groq")
                self.assertIs(type(mapped), errors.LLMError)
                self.assertFalse(mapped.retryable)
                self.assertEqual(mapped.provider, "groq")


class AnthropicErrorMappingTests(unittest.TestCase):
    def test_status_carrying_classes_map_to_expected_classes(self):
        table = (
            (anthropic.BadRequestError, 400, errors.LLMBadRequestError),
            (anthropic.AuthenticationError, 401, errors.LLMAuthError),
            (anthropic.PermissionDeniedError, 403, errors.LLMAuthError),
            (anthropic.NotFoundError, 404, errors.LLMBadRequestError),
            (anthropic.ConflictError, 409, errors.LLMBadRequestError),
            (anthropic.RequestTooLargeError, 413, errors.LLMBadRequestError),
            (anthropic.UnprocessableEntityError, 422, errors.LLMBadRequestError),
            (anthropic.RateLimitError, 429, errors.LLMRateLimitError),
            (anthropic.InternalServerError, 500, errors.LLMTransientError),
            (anthropic.OverloadedError, 529, errors.LLMTransientError),
        )
        for cls, status_code, expected in table:
            with self.subTest(cls=cls.__name__):
                exc = _anthropic_status_error(cls, status_code)
                mapped = errors.map_anthropic_error(exc, "claude")
                self.assertIsInstance(mapped, expected)
                self.assertEqual(mapped.provider, "claude")
                self.assertIs(mapped.cause, exc)
                self.assertEqual(mapped.retryable, isinstance(mapped, RETRYABLE_CLASSES))

    def test_unnamed_api_status_error_falls_back_to_the_status_code(self):
        # A future SDK release adding a subclass we don't enumerate must still
        # land in the right bucket -- RequestTooLargeError arrived that way.
        for status_code, expected, retryable in STATUS_TABLE:
            with self.subTest(status_code=status_code):
                exc = _anthropic_status_error(anthropic.APIStatusError, status_code)
                mapped = errors.map_anthropic_error(exc, "claude")
                self.assertIsInstance(mapped, expected)
                self.assertEqual(mapped.status_code, status_code)
                self.assertEqual(mapped.retryable, retryable)

    def test_sdk_retryable_error_is_not_inverted(self):
        # anthropic.RetryableError subclasses AnthropicError, not APIError, so
        # without an explicit branch it lands on the non-retryable base -- the
        # exact opposite of what its name promises.
        mapped = errors.map_anthropic_error(anthropic.RetryableError("try again"), "claude")
        self.assertTrue(mapped.retryable)

    def test_non_anthropic_exception_is_the_non_retryable_base(self):
        # The mapper is pure and takes BaseException; handing it something the
        # SDK never raises must not blow up inside the mapper itself.
        mapped = errors.map_anthropic_error(TypeError("could not resolve auth"), "claude")
        self.assertIs(type(mapped), errors.LLMError)
        self.assertFalse(mapped.retryable)

    def test_timeout_maps_to_timeout_not_transient(self):
        # REGRESSION GUARD: APITimeoutError subclasses APIConnectionError, so an
        # isinstance chain in the wrong order silently classifies every timeout
        # as a generic transient failure. Both are retryable, so nothing would
        # break loudly -- we would just lose the distinction in traces and in
        # the message a BD reviewer reads.
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        mapped = errors.map_anthropic_error(anthropic.APITimeoutError(request), "claude")
        self.assertIsInstance(mapped, errors.LLMTimeoutError)
        self.assertNotIsInstance(mapped, errors.LLMTransientError)
        self.assertTrue(mapped.retryable)

    def test_connection_error_is_transient(self):
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        exc = anthropic.APIConnectionError(request=request)
        mapped = errors.map_anthropic_error(exc, "claude")
        self.assertIsInstance(mapped, errors.LLMTransientError)
        self.assertTrue(mapped.retryable)

    def test_response_validation_error_is_malformed(self):
        exc = anthropic.APIResponseValidationError(response=_httpx_response(200), body=None)
        mapped = errors.map_anthropic_error(exc, "claude")
        self.assertIsInstance(mapped, errors.LLMMalformedResponseError)
        self.assertFalse(mapped.retryable)

    def test_rate_limit_retry_after_is_parsed_and_optional(self):
        with_header = _anthropic_status_error(
            anthropic.RateLimitError, 429, headers={"retry-after": "12.5"}
        )
        self.assertEqual(errors.map_anthropic_error(with_header, "claude").retry_after, 12.5)

        without_header = _anthropic_status_error(anthropic.RateLimitError, 429)
        self.assertIsNone(errors.map_anthropic_error(without_header, "claude").retry_after)

    def test_residual_anthropic_error_is_the_non_retryable_base(self):
        mapped = errors.map_anthropic_error(anthropic.AnthropicError("odd"), "claude")
        self.assertIs(type(mapped), errors.LLMError)
        self.assertFalse(mapped.retryable)

    def test_unreadable_headers_do_not_break_the_mapper(self):
        # `headers` is read off the exception with getattr, so a test double or
        # a future SDK could put anything there. A bad header must degrade to
        # "no guidance", never to an exception raised from inside the mapper.
        exc = anthropic.AnthropicError("odd")
        exc.response = types.SimpleNamespace(headers=object())
        self.assertIsNone(errors.map_anthropic_error(exc, "claude").retry_after)


# ---------------------------------------------------------------------------
# Adapters raise the taxonomy, not vendor exceptions
# ---------------------------------------------------------------------------


class AdapterErrorTranslationTests(unittest.TestCase):
    @mock.patch.dict(os.environ, {}, clear=True)
    def test_missing_key_is_a_non_retryable_auth_error(self):
        with self.assertRaises(errors.LLMAuthError) as ctx:
            GroqClient().complete("a prompt")
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(ctx.exception.provider, "groq")

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_http_429_becomes_a_rate_limit_error(self):
        response = _httpx_response(429, headers={"Retry-After": "7"})
        with mock.patch("project.app.services.llm.openai_compatible.httpx.post") as post:
            post.return_value = response
            with self.assertRaises(errors.LLMRateLimitError) as ctx:
                GroqClient().complete("a prompt")
        self.assertEqual(ctx.exception.retry_after, 7.0)
        self.assertTrue(ctx.exception.retryable)

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_unreadable_body_becomes_a_malformed_response_error(self):
        for payload in ({}, {"choices": []}, {"choices": [{"message": {"content": None}}]}):
            with self.subTest(payload=payload):
                response = mock.Mock()
                response.raise_for_status.return_value = None
                response.json.return_value = payload
                with mock.patch("project.app.services.llm.openai_compatible.httpx.post") as post:
                    post.return_value = response
                    with self.assertRaises(errors.LLMMalformedResponseError):
                        GroqClient().complete("a prompt")

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_whitespace_only_completion_is_malformed(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"choices": [{"message": {"content": "   \n "}}]}
        with mock.patch("project.app.services.llm.openai_compatible.httpx.post") as post:
            post.return_value = response
            with self.assertRaises(errors.LLMMalformedResponseError):
                GroqClient().complete("a prompt")

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_claude_missing_key_is_a_non_retryable_auth_error(self):
        # anthropic==0.109.1 builds a keyless client without complaint and only
        # fails at send time with a bare TypeError, which is not an
        # AnthropicError. Without the up-front check that would be the one
        # missing-key path in the repo that isn't an LLMAuthError.
        #
        # default_credentials() is patched off because clearing os.environ is
        # not enough to make this test hermetic: the SDK also reads the active
        # profile from disk, so on a developer machine that has ever logged in,
        # the client would resolve a credential and this would fail for a reason
        # that has nothing to do with the code under test.
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

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_a_failed_call_still_reports_how_long_it_took(self):
        # A failure has a duration too, and it is the same measurement
        # LLMResult.latency_s records. Without it, anything wanting to time
        # every attempt would have to re-time around the adapter and would end
        # up including our parsing -- and later the backoff sleeps.
        response = _httpx_response(503)
        with mock.patch("project.app.services.llm.openai_compatible.httpx.post") as post:
            post.return_value = response
            with self.assertRaises(errors.LLMTransientError) as ctx:
                GroqClient().complete("a prompt")
        self.assertIsNotNone(ctx.exception.latency_s)
        self.assertGreaterEqual(ctx.exception.latency_s, 0.0)

    def test_unmeasured_error_latency_is_none(self):
        # Errors raised before any call started (a missing key) must not claim
        # a zero-second provider call.
        self.assertIsNone(errors.LLMAuthError("no key").latency_s)

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
        # The SDK normally wraps transport failures into APIConnectionError.
        # This pins the insurance branch for when it doesn't.
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
# LLMResult (MUS-45): usage, model and finish reason survive the adapter
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
        # Anthropic shipped "pause_turn" after this map was written. A provider
        # adding a legitimate new reason must not make healthy generations show
        # up as errors on a dashboard -- the raw string is kept regardless.
        for raw in ("pause_turn", "something_new", "", None, 7):
            with self.subTest(raw=raw):
                self.assertIsNone(base.normalize_finish_reason(raw))


class CoerceTokenCountTests(unittest.TestCase):
    def test_real_counts_including_zero_are_preserved(self):
        # A reported 0 is a measurement and must survive; it is only an OMITTED
        # count that becomes None.
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
        # Same argument as token counts: a caller-constructed result must not
        # claim a real observation of a zero-second call.
        self.assertIsNone(base.LLMResult(text="hi", provider="groq", model="m").latency_s)

    def test_complete_is_a_thin_text_wrapper_over_generate(self):
        # The compatibility seam: complete() stays sync, keeps its name, and
        # still returns a plain str -- it just gets it from generate() now.
        class _Stub(base.LLMClient):
            provider_name = "stub"

            def generate(self, prompt, max_tokens=None, timeout=None):
                return base.LLMResult(text="the copy", provider="stub", model="m", input_tokens=7)

        client = _Stub(model="m")
        text = client.complete("p", max_tokens=9)
        self.assertEqual(text, "the copy")
        self.assertIsInstance(text, str)

    def test_base_agenerate_refuses_rather_than_faking_async(self):
        # F-c gives the adapters native async clients. Until then the base must
        # not quietly fall back to a thread pool, which would look like it
        # worked while capping concurrency at the pool size.
        class _Stub(base.LLMClient):
            provider_name = "stub"

            def generate(self, prompt, max_tokens=None, timeout=None):
                return base.LLMResult(text="x", provider="stub", model="m")

        with self.assertRaises(NotImplementedError) as ctx:
            asyncio.run(_Stub(model="m").agenerate("p"))
        self.assertIn("_Stub", str(ctx.exception))

    def test_acomplete_is_the_text_only_wrapper_over_agenerate(self):
        # Mirrors complete()/generate(). Pinned now so F-c's native async
        # adapters inherit a wrapper whose contract is already asserted.
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
        # What we asked for vs what the provider says it served: an alias
        # resolving to a dated snapshot is exactly why both are kept.
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
        # The distinction that matters downstream: a token histogram must see
        # "no observation" here, not an observation of zero.


class ClaudeSdkShapeTests(unittest.TestCase):
    """Pin the extraction against the SDK's REAL response types.

    Every other Claude test here builds a ``mock.Mock`` response, which asserts
    that we read the attributes we think we read — but would stay green forever
    if ``anthropic`` renamed ``Usage.input_tokens``, while production silently
    reported ``None``. The SDK is a pinned dependency and already installed, so
    one test against the genuine article closes that gap cheaply.
    """

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
        # with_raw_response / model_dump() turn the SDK's models into plain
        # dicts. read_field exists so that shape isn't silently zeroed.
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
                "model": "llama-3.3-70b-versatile-0000",
                "usage": {"prompt_tokens": 980, "completion_tokens": 142},
                "choices": [
                    {"message": {"content": "Generated copy"}, "finish_reason": "stop"},
                ],
            }
        )
        self.assertEqual(result.text, "Generated copy")
        self.assertEqual(result.provider, "groq")
        self.assertEqual(result.model, "some-model")
        self.assertEqual(result.response_model, "llama-3.3-70b-versatile-0000")
        self.assertEqual(result.input_tokens, 980)
        self.assertEqual(result.output_tokens, 142)
        self.assertEqual(result.raw_finish_reason, "stop")
        self.assertEqual(result.finish_reason, base.FINISH_STOP)
        self.assertGreaterEqual(result.latency_s, 0.0)

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_missing_metadata_degrades_to_none_not_an_error(self):
        # The minimal legal body -- exactly what the pre-existing adapter tests
        # mock. Metadata is best-effort; only the text is contractual.
        result = self._generate({"choices": [{"message": {"content": "Generated copy"}}]})
        self.assertEqual(result.text, "Generated copy")
        self.assertIsNone(result.input_tokens)
        self.assertIsNone(result.output_tokens)
        self.assertIsNone(result.response_model)
        self.assertIsNone(result.finish_reason)
        self.assertIsNone(result.raw_finish_reason)

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_unusable_metadata_shapes_do_not_break_a_good_completion(self):
        # A null usage block and a "choices" that isn't a list: neither is a
        # reason to throw away a valid email.
        for payload in (
            {
                "usage": None,
                "model": None,
                "choices": [{"message": {"content": "Generated copy"}}],
            },
            # "choices" as a mapping keyed by index rather than a list. Nothing
            # sends this, but the text chain still resolves, so the metadata
            # readers must not be the thing that fails.
            {"choices": {0: {"message": {"content": "Generated copy"}}}},
        ):
            with self.subTest(payload=payload):
                result = self._generate(payload)
                self.assertEqual(result.text, "Generated copy")
                self.assertIsNone(result.input_tokens)
                self.assertIsNone(result.response_model)

    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_half_a_usage_block_keeps_the_half_that_is_there(self):
        # The interesting direction: a partial block must not cost us the count
        # the provider DID send.
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
        # OpenAI-compatible providers count cached tokens WITHIN prompt_tokens,
        # so the two are not additive here (unlike Anthropic's).
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


# ---------------------------------------------------------------------------
# Key encryption (crypto.py) -- round trip + non-determinism across saves
# ---------------------------------------------------------------------------


class CryptoRoundTripTests(unittest.TestCase):
    def setUp(self):
        key = Fernet.generate_key().decode()
        self._patcher = mock.patch.dict(os.environ, {"LLM_KEY_ENCRYPTION_KEY": key})
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_encrypt_then_decrypt_round_trips(self):
        plaintext = "sk-ant-abcdef1234567890"
        blob = crypto.encrypt_key(plaintext)
        self.assertIsInstance(blob, bytes)
        self.assertEqual(crypto.decrypt_key(blob), plaintext)

    def test_ciphertext_differs_across_saves(self):
        # Fernet embeds a random IV + timestamp, so encrypting the same
        # plaintext twice must never produce identical ciphertext.
        plaintext = "sk-ant-abcdef1234567890"
        first = crypto.encrypt_key(plaintext)
        second = crypto.encrypt_key(plaintext)
        self.assertNotEqual(first, second)
        # Both still decrypt back to the same plaintext.
        self.assertEqual(crypto.decrypt_key(first), plaintext)
        self.assertEqual(crypto.decrypt_key(second), plaintext)

    def test_missing_encryption_key_raises_loudly(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(crypto.LLMKeyEncryptionError):
                crypto.encrypt_key("sk-ant-x")

    def test_decrypt_with_wrong_key_raises(self):
        blob = crypto.encrypt_key("sk-ant-x")
        other_key = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, {"LLM_KEY_ENCRYPTION_KEY": other_key}):
            with self.assertRaises(crypto.LLMKeyEncryptionError):
                crypto.decrypt_key(blob)


# ---------------------------------------------------------------------------
# Cache invalidation regression (MUS-32 step 7): a config save must not leave
# get_llm_client() serving a stale client for the same provider.
# ---------------------------------------------------------------------------


class CacheInvalidationRegressionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.claude = LLMProvider.objects.create(
            key="claude",
            label="Anthropic Claude",
            api_key_url="https://console.anthropic.com/settings/keys",
            api_key_label="Anthropic API key",
            api_key_prefix="sk-ant-",
        )
        cls.model_a = LLMModel.objects.create(
            provider=cls.claude,
            model_id="model-a",
            label="Model A",
            context_window=100_000,
            default_max_tokens=500,
            input_price_per_mtok_usd="1.00",
            output_price_per_mtok_usd="1.00",
        )
        cls.model_a2 = LLMModel.objects.create(
            provider=cls.claude,
            model_id="model-a2",
            label="Model A2",
            context_window=100_000,
            default_max_tokens=500,
            input_price_per_mtok_usd="1.00",
            output_price_per_mtok_usd="1.00",
        )

    def setUp(self):
        llm._build_client.cache_clear()
        key = Fernet.generate_key().decode()
        self._patcher = mock.patch.dict(os.environ, {"LLM_KEY_ENCRYPTION_KEY": key})
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        llm._build_client.cache_clear()

    def test_config_save_busts_stale_client_for_same_provider(self):
        # Configuration A: claude / model-a / key "key-a".
        cfg = LLMConfiguration.load(provider=self.claude, model=self.model_a, max_tokens=500)
        cfg.encrypted_api_key = crypto.encrypt_key("key-a")
        cfg.key_last_four = "ey-a"[-4:]
        cfg.save()

        with mock.patch.object(claude_mod.anthropic, "Anthropic"):
            client_a = llm.get_llm_client()
        self.assertEqual(client_a.model, "model-a")
        self.assertEqual(client_a.api_key, "key-a")

        # Configuration B: SAME provider, different model + key. The old
        # bug (@lru_cache keyed only on the provider string) would have kept
        # serving client_a here.
        cfg.model = self.model_a2
        cfg.encrypted_api_key = crypto.encrypt_key("key-b")
        cfg.key_last_four = "ey-b"[-4:]
        cfg.save()

        with mock.patch.object(claude_mod.anthropic, "Anthropic"):
            client_b = llm.get_llm_client()

        self.assertIsNot(client_b, client_a)
        self.assertEqual(client_b.model, "model-a2")
        self.assertEqual(client_b.api_key, "key-b")


# ---------------------------------------------------------------------------
# POST /api/llm/config/test/
# ---------------------------------------------------------------------------


class ConfigTestEndpointTests(AuthenticatedAPITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.provider = LLMProvider.objects.create(
            key="claude",
            label="Anthropic Claude",
            api_key_url="https://console.anthropic.com/settings/keys",
            api_key_label="Anthropic API key",
            api_key_prefix="sk-ant-",
        )
        cls.model = LLMModel.objects.create(
            provider=cls.provider,
            model_id="model-a",
            label="Model A",
            context_window=100_000,
            default_max_tokens=500,
            input_price_per_mtok_usd="1.00",
            output_price_per_mtok_usd="1.00",
        )

    def test_no_stored_or_env_key_maps_to_auth_error_kind(self):
        # No LLMConfiguration row's key AND no provider env var set -- the
        # test endpoint should report this as an "auth" failure, not crash.
        LLMConfiguration.load(provider=self.provider, model=self.model, max_tokens=500)
        with mock.patch.dict(os.environ, {}, clear=True):
            resp = self.client.post("/api/llm/config/test/")

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["ok"], False)
        self.assertEqual(resp.data["error_kind"], "auth")
        # Never echoes anything resembling SDK internals or a key.
        self.assertNotIn("ANTHROPIC_API_KEY", resp.content.decode())

    def test_no_configuration_saved_yet_returns_unknown_model(self):
        # No LLMConfiguration row at all (fresh DB / never PUT).
        resp = self.client.post("/api/llm/config/test/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["ok"], False)
        self.assertEqual(resp.data["error_kind"], "unknown_model")

    def test_successful_completion_returns_ok_with_latency_and_model_echo(self):
        LLMConfiguration.load(provider=self.provider, model=self.model, max_tokens=500)
        with (
            mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "env-key"}),
            mock.patch.object(claude_mod.ClaudeClient, "complete", return_value="pong"),
        ):
            resp = self.client.post("/api/llm/config/test/")

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data["ok"])
        self.assertEqual(resp.data["model_echo"], "model-a")
        self.assertIsInstance(resp.data["latency_ms"], int)
        self.assertGreaterEqual(resp.data["latency_ms"], 0)

    def test_provider_failure_maps_to_documented_error_kind(self):
        LLMConfiguration.load(provider=self.provider, model=self.model, max_tokens=500)

        class _FakeRateLimitError(Exception):
            status_code = 429

        with (
            mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "env-key"}),
            mock.patch.object(
                claude_mod.ClaudeClient, "complete", side_effect=_FakeRateLimitError("nope")
            ),
        ):
            resp = self.client.post("/api/llm/config/test/")

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data["ok"])
        self.assertEqual(resp.data["error_kind"], "rate_limit")

    def test_candidate_body_tests_an_unsaved_key_not_the_saved_one(self):
        # Nothing saved yet (fresh clone) -- a candidate body must still be
        # testable, using the key from the request, not "no configuration".
        with mock.patch.object(claude_mod.ClaudeClient, "complete", return_value="pong") as mocked:
            resp = self.client.post(
                "/api/llm/config/test/",
                {
                    "provider": "claude",
                    "model": "model-a",
                    "max_tokens": 500,
                    "api_key": "sk-ant-unsaved-candidate",
                },
                format="json",
            )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data["ok"])
        self.assertEqual(resp.data["model_echo"], "model-a")
        self.assertTrue(mocked.called)
        # The candidate key was used to build a client, but never echoed back.
        self.assertNotIn("sk-ant-unsaved-candidate", resp.content.decode())

    def test_candidate_body_does_not_leak_previously_saved_key_for_a_different_provider(self):
        # A saved Claude config exists, but the candidate body asks to test
        # a *different* provider with no key of its own and no env var set --
        # this must not fall back to Claude's saved key.
        other_provider = LLMProvider.objects.create(
            key="groq",
            label="Groq",
            api_key_url="https://console.groq.com/keys",
            api_key_label="Groq API key",
            api_key_prefix="gsk_",
        )
        other_model = LLMModel.objects.create(
            provider=other_provider,
            model_id="model-b",
            label="Model B",
            context_window=8_000,
            default_max_tokens=500,
            input_price_per_mtok_usd="0.10",
            output_price_per_mtok_usd="0.10",
        )
        LLMConfiguration.load(provider=self.provider, model=self.model, max_tokens=500)
        config_row = LLMConfiguration.objects.get(pk=1)
        with mock.patch.dict(
            os.environ, {"LLM_KEY_ENCRYPTION_KEY": Fernet.generate_key().decode()}
        ):
            config_row.encrypted_api_key = crypto.encrypt_key("claude-saved-key")
        config_row.save()

        with mock.patch.dict(os.environ, {}, clear=True):
            resp = self.client.post(
                "/api/llm/config/test/",
                {"provider": other_provider.key, "model": other_model.model_id, "max_tokens": 500},
                format="json",
            )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data["ok"])
        self.assertEqual(resp.data["error_kind"], "auth")

    def test_candidate_body_with_unknown_model_for_provider_returns_400(self):
        resp = self.client.post(
            "/api/llm/config/test/",
            {"provider": "claude", "model": "does-not-exist", "max_tokens": 500},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn("nope", resp.content.decode())


class LLMKeyEncryptionCheckTests(TestCase):
    """project/app/checks.py::llm_key_encryption_check -- boot-time guard."""

    @classmethod
    def setUpTestData(cls):
        cls.provider = LLMProvider.objects.create(
            key="claude",
            label="Anthropic Claude",
            api_key_url="https://console.anthropic.com/settings/keys",
            api_key_label="Anthropic API key",
            api_key_prefix="sk-ant-",
        )
        cls.model = LLMModel.objects.create(
            provider=cls.provider,
            model_id="model-a",
            label="Model A",
            context_window=100_000,
            default_max_tokens=500,
            input_price_per_mtok_usd="1.00",
            output_price_per_mtok_usd="1.00",
        )

    def test_passes_when_env_var_is_set(self):
        with mock.patch.dict(os.environ, {"LLM_KEY_ENCRYPTION_KEY": "some-key"}):
            errors = app_checks.llm_key_encryption_check(app_configs=None)
        self.assertEqual(errors, [])

    def test_passes_when_no_stored_key_exists(self):
        LLMConfiguration.load(provider=self.provider, model=self.model, max_tokens=500)
        with mock.patch.dict(os.environ, {}, clear=True):
            errors = app_checks.llm_key_encryption_check(app_configs=None)
        self.assertEqual(errors, [])

    def test_fails_when_stored_key_exists_and_env_var_unset(self):
        key = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, {"LLM_KEY_ENCRYPTION_KEY": key}):
            cfg = LLMConfiguration.load(provider=self.provider, model=self.model, max_tokens=500)
            cfg.encrypted_api_key = crypto.encrypt_key("sk-ant-x")
            cfg.save()

        with mock.patch.dict(os.environ, {}, clear=True):
            errors = app_checks.llm_key_encryption_check(app_configs=None)

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].id, "app.E001")


class ClassifyErrorKindTests(unittest.TestCase):
    """Unit tests for LLMConfigTestView._classify (no HTTP/DB involved)."""

    def _classify(self, exc):
        from project.app.views import LLMConfigTestView

        return LLMConfigTestView._classify(exc)

    def test_status_code_401_is_auth(self):
        exc = Exception("nope")
        exc.status_code = 401
        self.assertEqual(self._classify(exc), "auth")

    def test_status_code_403_is_auth(self):
        exc = Exception("nope")
        exc.status_code = 403
        self.assertEqual(self._classify(exc), "auth")

    def test_status_code_429_is_rate_limit(self):
        exc = Exception("nope")
        exc.status_code = 429
        self.assertEqual(self._classify(exc), "rate_limit")

    def test_status_code_404_is_unknown_model(self):
        exc = Exception("nope")
        exc.status_code = 404
        self.assertEqual(self._classify(exc), "unknown_model")

    def test_response_attr_status_code_is_read(self):
        exc = Exception("nope")
        exc.response = mock.Mock(status_code=401)
        self.assertEqual(self._classify(exc), "auth")

    def test_exception_class_name_fallback_auth(self):
        class AuthenticationError(Exception):
            pass

        self.assertEqual(self._classify(AuthenticationError("x")), "auth")

    def test_exception_class_name_fallback_rate_limit(self):
        class RateLimitError(Exception):
            pass

        self.assertEqual(self._classify(RateLimitError("x")), "rate_limit")

    def test_exception_class_name_fallback_unknown_model(self):
        class NotFoundError(Exception):
            pass

        self.assertEqual(self._classify(NotFoundError("x")), "unknown_model")

    def test_unrecognized_exception_falls_back_to_network(self):
        self.assertEqual(self._classify(ConnectionError("x")), "network")


if __name__ == "__main__":
    unittest.main()
