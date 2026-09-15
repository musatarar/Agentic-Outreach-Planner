import json
import os
from datetime import date
from unittest import mock
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from project.app.models import (
    Lead,
    LLMConfiguration,
    LLMModel,
    LLMProvider,
    OutreachAction,
)
from project.app.tests.tests_auth_utils import AuthenticatedAPITestCase


def make_lead(lead_id, **overrides):
    defaults = dict(
        id=lead_id,
        agency_name=f"Agency {lead_id}",
        contact_name=f"Contact {lead_id}",
        contact_email=f"{lead_id}@example.com",
        contact_phone="555-0100",
        state="CA",
        num_producers=3,
        years_in_business=5,
        estimated_book_size_usd=1_000_000,
        stage="active_trial",
        signed_up_date=date(2026, 1, 1),
        last_login_date=date(2026, 6, 1),
        quotes_created=10,
        quotes_submitted=4,
        deals_closed=1,
        last_contacted_date=date(2026, 5, 1),
        hubspot_notes="",
    )
    defaults.update(overrides)
    return Lead.objects.create(**defaults)


def make_llm_provider(key="claude", **overrides):
    defaults = dict(
        key=key,
        label=f"Provider {key}",
        api_key_url=f"https://example.com/{key}/keys",
        api_key_label=f"{key} API key",
        api_key_prefix="sk-",
    )
    defaults.update(overrides)
    return LLMProvider.objects.create(**defaults)


def make_llm_model(provider, model_id="model-a", **overrides):
    defaults = dict(
        provider=provider,
        model_id=model_id,
        label=f"Model {model_id}",
        context_window=100_000,
        default_max_tokens=500,
        input_price_per_mtok_usd="1.00",
        output_price_per_mtok_usd="2.00",
        tier="balanced",
    )
    defaults.update(overrides)
    return LLMModel.objects.create(**defaults)


class LeadListViewTests(AuthenticatedAPITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.lead_b = make_lead("lead_002", agency_name="Bravo")
        cls.lead_a = make_lead("lead_001", agency_name="Alpha")

    def test_lists_all_leads_ordered_by_id(self):
        resp = self.client.get(reverse("lead-list"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 2)
        self.assertEqual([row["id"] for row in resp.data], ["lead_001", "lead_002"])
        # Full lead serializer exposes all model fields.
        first = resp.data[0]
        for field in (
            "agency_name",
            "contact_name",
            "contact_email",
            "contact_phone",
            "state",
            "num_producers",
            "estimated_book_size_usd",
            "stage",
            "hubspot_notes",
        ):
            self.assertIn(field, first)


class OutreachListViewTests(AuthenticatedAPITestCase):
    """GET /api/outreach/ — the review inbox: latest action per lead, paginated."""

    @classmethod
    def setUpTestData(cls):
        cls.lead1 = make_lead("lead_001")
        cls.lead2 = make_lead("lead_002")

        # Two actions for lead1 — only the most recent should appear.
        cls.old = OutreachAction.objects.create(
            lead=cls.lead1,
            priority=1,
            action_type="nudge_usage",
            reason="old reason",
            suggested_copy="old copy",
        )
        cls.recent = OutreachAction.objects.create(
            lead=cls.lead1,
            priority=3,
            action_type="reengage_dormant",
            reason="recent reason",
            suggested_copy="recent copy",
        )
        # Single action for lead2 at higher priority (lower number).
        cls.action2 = OutreachAction.objects.create(
            lead=cls.lead2,
            priority=2,
            action_type="complete_onboarding",
            reason="onboard",
            needs_human=False,
        )

    def test_most_recent_action_per_lead_ordered_by_priority(self):
        resp = self.client.get(reverse("outreach-list"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data["results"]
        self.assertEqual(len(results), 2)

        ids = [row["id"] for row in results]
        self.assertNotIn(self.old.id, ids)
        self.assertIn(self.recent.id, ids)

        self.assertEqual([row["priority"] for row in results], [2, 3])
        self.assertEqual(results[0]["id"], self.action2.id)
        self.assertEqual(results[1]["id"], self.recent.id)

    def test_the_list_is_paginated(self):
        resp = self.client.get(reverse("outreach-list"), {"page_size": 1})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # The envelope is the contract: an unbounded array is what pagination fixes.
        self.assertEqual(set(resp.data.keys()), {"count", "next", "previous", "results"})
        self.assertEqual(resp.data["count"], 2)
        self.assertEqual(len(resp.data["results"]), 1)
        self.assertIsNotNone(resp.data["next"])

    def test_review_item_shape_matches_contract(self):
        resp = self.client.get(reverse("outreach-list"))
        row = resp.data["results"][0]
        self.assertEqual(
            set(row.keys()),
            {
                "id",
                "status",
                "status_changed_at",
                "priority",
                "action_type",
                "action_label",
                "reason",
                "needs_human",
                "further_action",
                "created_at",
                "dedupe_key",
                "lead",
                "suggested_copy",
                "edited_copy",
                "effective_copy",
                "is_edited",
                "verification",
                "can_approve",
            },
        )
        self.assertEqual(
            set(row["lead"].keys()),
            {
                "id",
                "agency_name",
                "contact_name",
                "contact_email",
                "state",
                "stage",
                "num_producers",
                "estimated_book_size_usd",
                "quotes_created",
                "quotes_submitted",
                "deals_closed",
                "signed_up_date",
                "last_login_date",
                "last_contacted_date",
                "recent_events",
            },
        )


class OutreachRunViewTests(AuthenticatedAPITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.lead1 = make_lead("lead_001", agency_name="Alpha")
        cls.lead2 = make_lead("lead_002", agency_name="Bravo")

    def test_run_serializes_planned_actions_ordered_by_priority(self):
        # plan_outreach returns persisted actions; build them here and mock it.
        a_low = OutreachAction.objects.create(
            lead=self.lead2,
            priority=3,
            action_type="nudge_usage",
            reason="low priority",
            suggested_copy="copy 2",
        )
        a_high = OutreachAction.objects.create(
            lead=self.lead1,
            priority=1,
            action_type="follow_up_after_hold",
            reason="high priority",
            suggested_copy="copy 1",
        )

        # The view imports plan_outreach inside the method from this path.
        with patch(
            "project.app.services.outreach.plan_outreach",
            return_value=[a_low, a_high],
        ) as mock_plan:
            resp = self.client.post(reverse("outreach-run"))

        mock_plan.assert_called_once()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 2)
        self.assertEqual([row["priority"] for row in resp.data], [1, 3])
        self.assertEqual(resp.data[0]["id"], a_high.id)
        self.assertEqual(resp.data[1]["id"], a_low.id)
        self.assertEqual(resp.data[0]["action_type"], "follow_up_after_hold")
        self.assertEqual(resp.data[0]["lead"]["agency_name"], "Alpha")


# ---------------------------------------------------------------------------
# LLM configuration endpoints (MUS-32)
# ---------------------------------------------------------------------------


class LLMCatalogViewTests(AuthenticatedAPITestCase):
    """GET /api/llm/catalog/ -- read-only, no auth required."""

    @classmethod
    def setUpTestData(cls):
        cls.claude = make_llm_provider("claude", sort_order=1)
        make_llm_model(cls.claude, "opus", sort_order=1)
        make_llm_model(cls.claude, "haiku", sort_order=2)
        cls.disabled_provider = make_llm_provider("disabled", sort_order=2, enabled=False)
        make_llm_model(cls.disabled_provider, "hidden-model")
        # A disabled model on an otherwise-enabled provider shouldn't show up.
        make_llm_model(cls.claude, "disabled-model", sort_order=3, enabled=False)

    def test_catalog_shape_and_enabled_filtering(self):
        resp = self.client.get(reverse("llm-catalog"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        providers = resp.data["providers"]
        keys = [p["key"] for p in providers]
        self.assertEqual(keys, ["claude"])  # disabled provider excluded

        provider = providers[0]
        self.assertEqual(
            set(provider.keys()),
            {"key", "label", "api_key_url", "api_key_label", "api_key_prefix", "models"},
        )
        model_ids = [m["id"] for m in provider["models"]]
        self.assertEqual(model_ids, ["opus", "haiku"])  # disabled-model excluded, sorted

        model = provider["models"][0]
        self.assertEqual(
            set(model.keys()),
            {
                "id",
                "label",
                "context_window",
                "default_max_tokens",
                "input_price_per_mtok_usd",
                "output_price_per_mtok_usd",
                "tier",
                "notes",
            },
        )
        # Prices serialize as JSON numbers, not DRF's stringified Decimals.
        rendered_model = json.loads(resp.content)["providers"][0]["models"][0]
        self.assertIsInstance(rendered_model["input_price_per_mtok_usd"], float)


class LLMConfigViewTests(AuthenticatedAPITestCase):
    """GET/PUT /api/llm/config/ -- behind the magic-link session (MUS-37)."""

    @classmethod
    def setUpTestData(cls):
        cls.claude = make_llm_provider("claude")
        cls.opus = make_llm_model(cls.claude, "opus", context_window=200_000)
        cls.groq = make_llm_provider("groq")
        cls.llama = make_llm_model(cls.groq, "llama", context_window=100_000)

    def setUp(self):
        super().setUp()
        self._fernet_key = Fernet.generate_key().decode()
        self._patcher = mock.patch.dict(os.environ, {"LLM_KEY_ENCRYPTION_KEY": self._fernet_key})
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_unauthenticated_request_401(self):
        # 401, not 403: see SessionAuthenticationWith401 and contract 9.4.
        resp = APIClient().get(reverse("llm-config"))
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(resp.data["code"], "not_authenticated")

    def test_get_with_no_saved_row_falls_back_to_provider_default(self):
        resp = self.client.get(reverse("llm-config"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            set(resp.data.keys()),
            {
                "provider",
                "model",
                "max_tokens",
                "has_key",
                "key_last_four",
                "key_source",
                "updated_at",
            },
        )
        self.assertFalse(resp.data["has_key"])
        self.assertEqual(resp.data["key_source"], "none")
        self.assertIsNone(resp.data["updated_at"])

    def test_put_creates_config_and_never_echoes_the_key(self):
        secret_key = "sk-ant-super-secret-value-12345"
        resp = self.client.put(
            reverse("llm-config"),
            {
                "provider": "claude",
                "model": "opus",
                "max_tokens": 500,
                "api_key": secret_key,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertNotIn(secret_key, resp.content.decode())
        self.assertTrue(resp.data["has_key"])
        self.assertEqual(resp.data["key_last_four"], secret_key[-4:])
        self.assertEqual(resp.data["key_source"], "database")
        self.assertEqual(resp.data["provider"], "claude")
        self.assertEqual(resp.data["model"], "opus")
        self.assertIsNotNone(resp.data["updated_at"])

        # The row is genuinely encrypted at rest, not stored in the clear.
        config = LLMConfiguration.objects.get(pk=1)
        self.assertNotIn(secret_key.encode(), bytes(config.encrypted_api_key))

    def test_get_after_put_never_contains_stored_key_substring(self):
        secret_key = "sk-ant-another-secret-abcdef"
        self.client.put(
            reverse("llm-config"),
            {"provider": "claude", "model": "opus", "max_tokens": 500, "api_key": secret_key},
            format="json",
        )
        resp = self.client.get(reverse("llm-config"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertNotIn(secret_key, resp.content.decode())

    def test_put_omitting_api_key_preserves_existing_key(self):
        secret_key = "sk-ant-keep-me-1234"
        self.client.put(
            reverse("llm-config"),
            {"provider": "claude", "model": "opus", "max_tokens": 500, "api_key": secret_key},
            format="json",
        )
        resp = self.client.put(
            reverse("llm-config"),
            {"provider": "claude", "model": "opus", "max_tokens": 700},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data["has_key"])
        self.assertEqual(resp.data["key_last_four"], secret_key[-4:])
        self.assertEqual(resp.data["max_tokens"], 700)

    def test_put_null_api_key_clears_it(self):
        secret_key = "sk-ant-clear-me-5678"
        self.client.put(
            reverse("llm-config"),
            {"provider": "claude", "model": "opus", "max_tokens": 500, "api_key": secret_key},
            format="json",
        )
        resp = self.client.put(
            reverse("llm-config"),
            {"provider": "claude", "model": "opus", "max_tokens": 500, "api_key": None},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data["has_key"])
        self.assertEqual(resp.data["key_last_four"], "")

    def test_put_model_not_belonging_to_provider_400(self):
        resp = self.client.put(
            reverse("llm-config"),
            {"provider": "claude", "model": "llama", "max_tokens": 500},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        # Contract envelope (MUS-37): the offending field name rides in `detail`.
        self.assertEqual(resp.data["code"], "validation_error")
        self.assertIn("model", resp.data["detail"])

    def test_put_max_tokens_exceeding_context_window_400(self):
        resp = self.client.put(
            reverse("llm-config"),
            {"provider": "claude", "model": "opus", "max_tokens": 999_999},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data["code"], "validation_error")
        self.assertIn("max_tokens", resp.data["detail"])

    def test_put_unknown_provider_400(self):
        resp = self.client.put(
            reverse("llm-config"),
            {"provider": "bogus", "model": "opus", "max_tokens": 500},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data["code"], "validation_error")
        self.assertIn("provider", resp.data["detail"])

    def test_key_source_database_when_key_stored(self):
        self.client.put(
            reverse("llm-config"),
            {"provider": "claude", "model": "opus", "max_tokens": 500, "api_key": "sk-ant-x"},
            format="json",
        )
        resp = self.client.get(reverse("llm-config"))
        self.assertEqual(resp.data["key_source"], "database")

    def test_key_source_environment_when_no_stored_key_but_env_var_set(self):
        self.client.put(
            reverse("llm-config"),
            {"provider": "claude", "model": "opus", "max_tokens": 500},
            format="json",
        )
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "env-key-value"}):
            resp = self.client.get(reverse("llm-config"))
        self.assertEqual(resp.data["key_source"], "environment")
        self.assertFalse(resp.data["has_key"])

    def test_key_source_none_when_no_stored_key_and_no_env_var(self):
        self.client.put(
            reverse("llm-config"),
            {"provider": "claude", "model": "opus", "max_tokens": 500},
            format="json",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            resp = self.client.get(reverse("llm-config"))
        self.assertEqual(resp.data["key_source"], "none")
        self.assertFalse(resp.data["has_key"])
