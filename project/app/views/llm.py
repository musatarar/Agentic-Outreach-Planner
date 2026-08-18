"""LLM configuration endpoints (MUS-32), session-authenticated like the rest of the API."""

import time

from django.db.models import Prefetch
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from project.app.models import LLMConfiguration, LLMModel, LLMProvider
from project.app.serializers import LLMConfigurationSerializer, LLMProviderSerializer
from project.app.services.crypto import encrypt_key
from project.app.services.llm import _REGISTRY as LLM_PROVIDER_REGISTRY
from project.app.services.llm import config as llm_config

# Error kinds the config-test endpoint may report. Never the raw SDK
# exception text or the API key itself.
_ERROR_MESSAGES = {
    "auth": "Authentication failed — check the configured API key.",
    "rate_limit": "Rate limit exceeded — try again shortly.",
    "unknown_model": "The configured model was not recognized by the provider.",
    "network": "Could not reach the provider — check network connectivity.",
}


class LLMCatalogView(APIView):
    """GET /api/llm/catalog/ — enabled providers and models (seeded reference data)."""

    def get(self, request, *args, **kwargs):
        providers = (
            LLMProvider.objects.filter(enabled=True)
            .prefetch_related(
                Prefetch(
                    "models",
                    queryset=LLMModel.objects.filter(enabled=True).order_by(
                        "sort_order", "model_id"
                    ),
                )
            )
            .order_by("sort_order", "key")
        )
        serializer = LLMProviderSerializer(providers, many=True)
        return Response({"providers": serializer.data}, status=status.HTTP_200_OK)


class LLMConfigView(APIView):
    """GET/PUT /api/llm/config/ — the active provider/model/key selection.

    The one place a stored provider API key can be written; behind the same
    magic-link session as the rest of the API (MUS-37).
    """

    def get(self, request, *args, **kwargs):
        return Response(self._current_state(), status=status.HTTP_200_OK)

    def put(self, request, *args, **kwargs):
        serializer = LLMConfigurationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data

        config = LLMConfiguration.objects.select_related("provider", "model").filter(pk=1).first()
        if config is None:
            config = LLMConfiguration(pk=1)
        config.provider = validated["provider"]
        config.model = validated["model"]
        config.max_tokens = validated["max_tokens"]

        if "api_key" in validated:
            new_key = validated["api_key"]
            if new_key is None:
                config.encrypted_api_key = None
                config.key_last_four = ""
            else:
                config.encrypted_api_key = encrypt_key(new_key)
                config.key_last_four = new_key[-4:]
        config.save()

        return Response(self._current_state(), status=status.HTTP_200_OK)

    def _current_state(self):
        """Build the GET/PUT response shape, whether or not a row is saved yet."""
        config = LLMConfiguration.objects.select_related("provider", "model").filter(pk=1).first()
        if config is not None:
            if config.encrypted_api_key:
                key_source = "database"
            else:
                _key, key_source = llm_config.resolve_active_key(config.provider_id)
            config.resolved_key_source = key_source
            return LLMConfigurationSerializer(config).data

        provider_key = llm_config.get_provider()
        provider_cfg = llm_config.get_provider_config(provider_key)
        _key, key_source = llm_config.resolve_active_key(provider_key)
        model_id = provider_cfg.get("model")
        return {
            "provider": provider_key,
            "model": model_id,
            "max_tokens": provider_cfg.get("max_tokens", 500),
            "has_key": False,
            "key_last_four": "",
            "key_source": key_source,
            "updated_at": None,
        }


class LLMConfigTestView(APIView):
    """POST /api/llm/config/test/ — one minimal completion to verify a
    provider/model/key combination actually works.

    Optional candidate body in the same shape as ``PUT /api/llm/config/`` so a
    key can be tested before saving; an omitted ``api_key`` falls back to the
    resolvable key, and an omitted ``provider`` tests the saved configuration.
    """

    _TEST_PROMPT = "Reply with exactly one word: pong"
    _TEST_TIMEOUT_SECONDS = 10
    _TEST_MAX_TOKENS = 8

    def post(self, request, *args, **kwargs):
        candidate = request.data if isinstance(request.data, dict) else {}

        if candidate.get("provider"):
            serializer = LLMConfigurationSerializer(data=candidate)
            serializer.is_valid(raise_exception=True)
            validated = serializer.validated_data
            provider = validated["provider"]
            model = validated["model"]
            candidate_key = validated.get("api_key")
            if candidate_key:
                api_key = candidate_key
            else:
                api_key, _key_source = llm_config.resolve_active_key(provider.key)
        else:
            config = (
                LLMConfiguration.objects.select_related("provider", "model").filter(pk=1).first()
            )
            if config is None:
                return Response(
                    {
                        "ok": False,
                        "error_kind": "unknown_model",
                        "message": "No LLM configuration saved yet.",
                    },
                    status=status.HTTP_200_OK,
                )
            provider = config.provider
            model = config.model
            api_key, _key_source = llm_config.resolve_active_key(provider.key)

        if not api_key:
            return Response(
                {
                    "ok": False,
                    "error_kind": "auth",
                    "message": "No API key configured for this provider.",
                },
                status=status.HTTP_200_OK,
            )

        client_cls = LLM_PROVIDER_REGISTRY.get(provider.key)
        if client_cls is None:
            return Response(
                {
                    "ok": False,
                    "error_kind": "unknown_model",
                    "message": f"Unknown provider '{provider.key}'.",
                },
                status=status.HTTP_200_OK,
            )

        client = client_cls(
            model=model.model_id, default_max_tokens=self._TEST_MAX_TOKENS, api_key=api_key
        )
        start = time.monotonic()
        try:
            client.complete(
                self._TEST_PROMPT,
                max_tokens=self._TEST_MAX_TOKENS,
                timeout=self._TEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 -- classify below, never leak raw SDK text
            error_kind = self._classify(exc)
            return Response(
                {"ok": False, "error_kind": error_kind, "message": _ERROR_MESSAGES[error_kind]},
                status=status.HTTP_200_OK,
            )

        latency_ms = int((time.monotonic() - start) * 1000)
        return Response(
            {"ok": True, "latency_ms": latency_ms, "model_echo": model.model_id},
            status=status.HTTP_200_OK,
        )

    @staticmethod
    def _classify(exc):
        """Map a provider SDK/HTTP exception to one of the 4 documented error
        kinds, without echoing its message text."""
        status_code = getattr(exc, "status_code", None)
        if status_code is None:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)

        if status_code in (401, 403):
            return "auth"
        if status_code == 429:
            return "rate_limit"
        if status_code == 404:
            return "unknown_model"

        name = exc.__class__.__name__.lower()
        if "auth" in name or "permission" in name:
            return "auth"
        if "ratelimit" in name or "rate_limit" in name or "throttl" in name:
            return "rate_limit"
        if "notfound" in name:
            return "unknown_model"
        return "network"
