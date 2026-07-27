"""Tests for the provider-agnostic LLM layer (project/app/services/llm/)."""

import base64
import os
import unittest
from unittest import mock

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APITestCase as DRFAPITestCase

from project.app import checks as app_checks
from project.app.models import LLMConfiguration, LLMModel, LLMProvider
from project.app.services import crypto, llm
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


@override_settings(LLM_ADMIN_USERNAME="admin", LLM_ADMIN_PASSWORD="secret")
class ConfigTestEndpointTests(DRFAPITestCase):
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

    def setUp(self):
        creds = base64.b64encode(b"admin:secret").decode()
        self.client.credentials(HTTP_AUTHORIZATION=f"Basic {creds}")

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
