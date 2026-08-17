import time

from django.db import IntegrityError, transaction
from django.db.models import Prefetch
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from project.app.models import (
    Lead,
    LLMConfiguration,
    LLMModel,
    LLMProvider,
    OutreachAction,
    ReviewDecision,
)
from project.app.serializers import (
    LeadSerializer,
    LLMConfigurationSerializer,
    LLMProviderSerializer,
    OutreachActionSerializer,
    ReviewDecisionSerializer,
)
from project.app.services.actions import ACTION_META, SELECTABLE_ACTION_TYPES
from project.app.services.crypto import encrypt_key
from project.app.services.llm import _REGISTRY as LLM_PROVIDER_REGISTRY
from project.app.services.llm import config as llm_config


class OutreachRunView(APIView):
    """POST /api/outreach/run/ — run the planner and return created actions.

    Optional body ``{"resume_run_id": "<uuid>"}`` re-enters a crashed agent run
    (MUS-29); ``plan_outreach`` itself decides the two 400s (``unknown_run``,
    ``agent_disabled``) since scripts and tests call it directly too.
    """

    def post(self, request, *args, **kwargs):
        # Imported inside the method so this module loads independently of the
        # service module.
        from project.app.services.outreach import AgentDisabled, UnknownRun, plan_outreach

        data = request.data if isinstance(request.data, dict) else {}
        resume_run_id = str(data.get("resume_run_id") or "") or None

        try:
            actions = plan_outreach(resume_run_id=resume_run_id)
        except UnknownRun:
            return Response({"error": "unknown_run"}, status=status.HTTP_400_BAD_REQUEST)
        except AgentDisabled:
            return Response({"error": "agent_disabled"}, status=status.HTTP_400_BAD_REQUEST)
        actions = sorted(actions, key=lambda a: (a.priority, a.lead_id))
        serializer = OutreachActionSerializer(actions, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class OutreachListView(APIView):
    """GET /api/outreach/ — most recent action per lead, ordered by priority."""

    def get(self, request, *args, **kwargs):
        # Newest action per lead: order newest-first, keep the first per lead.
        latest = OutreachAction.objects.select_related("lead").order_by(
            "lead_id", "-created_at", "-id"
        )
        seen = set()
        actions = []
        for action in latest:
            if action.lead_id in seen:
                continue
            seen.add(action.lead_id)
            actions.append(action)

        actions.sort(key=lambda a: (a.priority, a.lead_id))
        serializer = OutreachActionSerializer(actions, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class OutreachReportView(APIView):
    """GET /api/reports/ — full outreach action history, newest first."""

    def get(self, request, *args, **kwargs):
        actions = OutreachAction.objects.select_related("lead").order_by("-created_at", "-id")
        serializer = OutreachActionSerializer(actions, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class ReviewQueueView(APIView):
    """GET /api/review-queue/ — needs_human actions awaiting a decision."""

    def get(self, request, *args, **kwargs):
        # Resolution kinds only (MUS-29): send decisions answer "may this go
        # out?", not "is the unknown resolved?", so they must not hide a row.
        decided_ids = set(
            ReviewDecision.objects.filter(kind__in=ReviewDecision.RESOLUTION_KINDS).values_list(
                "outreach_action_id", flat=True
            )
        )

        latest = OutreachAction.objects.select_related("lead").order_by(
            "lead_id", "-created_at", "-id"
        )
        seen = set()
        items = []
        for action in latest:
            if action.lead_id in seen:
                continue
            seen.add(action.lead_id)
            if not action.needs_human:
                continue
            if action.id in decided_ids:
                continue
            items.append(action)

        items.sort(key=lambda a: (a.priority, a.lead_id))

        action_options = [
            {
                "value": k,
                "label": ACTION_META[k]["label"],
                "urgency": ACTION_META[k]["urgency"],
            }
            for k in SELECTABLE_ACTION_TYPES
        ]

        return Response(
            {
                "items": OutreachActionSerializer(items, many=True).data,
                "action_options": action_options,
            },
            status=status.HTTP_200_OK,
        )


class ReviewDecisionListCreateView(APIView):
    """GET/POST /api/review-decisions/."""

    def get(self, request, *args, **kwargs):
        qs = ReviewDecision.objects.all().order_by("-created_at", "-id")
        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        serializer = ReviewDecisionSerializer(qs, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request, *args, **kwargs):
        serializer = ReviewDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            # Savepoint so the IntegrityError doesn't poison the surrounding
            # transaction (ATOMIC_REQUESTS / TestCase).
            with transaction.atomic():
                serializer.save()
        except IntegrityError:
            # OneToOne unique constraint: a decision already exists for this
            # action (double-click / concurrent reviewers).
            return Response(
                {"outreach_action": "A decision already exists for this action."},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class LeadListView(APIView):
    """GET /api/leads/ — all leads."""

    def get(self, request, *args, **kwargs):
        leads = Lead.objects.all().order_by("id")
        serializer = LeadSerializer(leads, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class LeadComposeView(APIView):
    """POST /api/leads/{lead_id}/compose/ — compose outreach for ONE client (MUS-68).

    The same planner ``/api/outreach/run/`` calls, scoped to one lead. 200 with
    the new action, 404 for an unknown lead, 409 when the planner declines
    (already queued or permanently dismissed) — no provider call in either refusal.
    """

    def post(self, request, lead_id, *args, **kwargs):
        # Imported inside the method for the same reason OutreachRunView's is.
        from project.app.services.outreach import plan_outreach

        if not Lead.objects.filter(pk=lead_id).exists():
            return Response({"error": "unknown_lead"}, status=status.HTTP_404_NOT_FOUND)

        planned = plan_outreach(lead_ids=[lead_id])
        if not planned:
            return Response({"error": "no_new_recommendation"}, status=status.HTTP_409_CONFLICT)

        # At most one action per lead per run, and this run is one lead.
        serializer = OutreachActionSerializer(planned[0])
        return Response(serializer.data, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# LLM configuration (MUS-32). Authenticated by session like everything above.
# ---------------------------------------------------------------------------

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
