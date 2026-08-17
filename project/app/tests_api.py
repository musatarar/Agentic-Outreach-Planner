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
    ReviewDecision,
)
from project.app.tests_auth_utils import AuthenticatedAPITestCase


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
        self.assertEqual(len(resp.data), 2)

        ids = [row["id"] for row in resp.data]
        self.assertNotIn(self.old.id, ids)
        self.assertIn(self.recent.id, ids)

        self.assertEqual([row["priority"] for row in resp.data], [2, 3])
        self.assertEqual(resp.data[0]["id"], self.action2.id)
        self.assertEqual(resp.data[1]["id"], self.recent.id)

    def test_action_item_shape_matches_contract(self):
        resp = self.client.get(reverse("outreach-list"))
        row = resp.data[0]
        self.assertEqual(
            set(row.keys()),
            {
                "id",
                "lead",
                "priority",
                "action_type",
                "reason",
                "suggested_copy",
                "needs_human",
                "further_action",
                "created_at",
            },
        )
        self.assertEqual(
            set(row["lead"].keys()),
            {"id", "agency_name", "contact_name", "contact_email"},
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


class OutreachReportViewTests(AuthenticatedAPITestCase):
    """GET /api/reports/ returns the FULL action history, newest first."""

    @classmethod
    def setUpTestData(cls):
        cls.lead = make_lead("lead_001", agency_name="Alpha")
        cls.older = OutreachAction.objects.create(
            lead=cls.lead,
            priority=2,
            action_type="nudge_usage",
            reason="older run",
            suggested_copy="old copy",
        )
        cls.newer = OutreachAction.objects.create(
            lead=cls.lead,
            priority=1,
            action_type="follow_up_after_hold",
            reason="newer run",
            suggested_copy="new copy",
        )

    def test_returns_all_actions_not_deduped(self):
        resp = self.client.get(reverse("outreach-reports"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 2)

    def test_newest_first(self):
        resp = self.client.get(reverse("outreach-reports"))
        self.assertEqual([row["id"] for row in resp.data], [self.newer.id, self.older.id])

    def test_item_shape_matches_contract(self):
        resp = self.client.get(reverse("outreach-reports"))
        item = resp.data[0]
        self.assertEqual(
            set(item.keys()),
            {
                "id",
                "lead",
                "priority",
                "action_type",
                "reason",
                "suggested_copy",
                "needs_human",
                "further_action",
                "created_at",
            },
        )
        self.assertEqual(item["lead"]["agency_name"], "Alpha")


class ReviewQueueViewTests(AuthenticatedAPITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.lead1 = make_lead("lead_001")
        cls.lead2 = make_lead("lead_002")
        cls.lead3 = make_lead("lead_003")

        # lead1: older (non-human) then newer needs_human -> newest wins, in queue.
        cls.lead1_old = OutreachAction.objects.create(
            lead=cls.lead1,
            priority=2,
            action_type="nudge_usage",
            reason="old",
            needs_human=False,
        )
        cls.lead1_new = OutreachAction.objects.create(
            lead=cls.lead1,
            priority=1,
            action_type="unknown",
            reason="needs review",
            needs_human=True,
        )
        # lead2: needs_human but already has a resolved decision -> excluded.
        cls.lead2_action = OutreachAction.objects.create(
            lead=cls.lead2,
            priority=1,
            action_type="unknown",
            reason="needs review",
            needs_human=True,
        )
        ReviewDecision.objects.create(
            outreach_action=cls.lead2_action,
            kind=ReviewDecision.KIND_SELECT,
            status=ReviewDecision.STATUS_RESOLVED,
            selected_action_type="nudge_usage",
        )
        # lead3: not needs_human -> excluded.
        cls.lead3_action = OutreachAction.objects.create(
            lead=cls.lead3,
            priority=1,
            action_type="nudge_usage",
            reason="fine",
            needs_human=False,
        )

    def test_queue_only_needs_human_without_decision(self):
        resp = self.client.get(reverse("review-queue"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [row["id"] for row in resp.data["items"]]
        self.assertEqual(ids, [self.lead1_new.id])
        self.assertNotIn(self.lead1_old.id, ids)
        self.assertNotIn(self.lead2_action.id, ids)
        self.assertNotIn(self.lead3_action.id, ids)

    def test_action_options_excludes_unknown(self):
        resp = self.client.get(reverse("review-queue"))
        options = resp.data["action_options"]
        values = [o["value"] for o in options]
        self.assertNotIn("unknown", values)
        for opt in options:
            self.assertEqual(set(opt.keys()), {"value", "label", "urgency"})


class ReviewDecisionCreateTests(AuthenticatedAPITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.lead = make_lead("lead_001")
        cls.action = OutreachAction.objects.create(
            lead=cls.lead,
            priority=1,
            action_type="unknown",
            reason="needs review",
            needs_human=True,
        )

    def test_select_existing_valid_returns_201_resolved(self):
        resp = self.client.post(
            reverse("review-decisions"),
            {
                "outreach_action": self.action.id,
                "kind": "select_existing",
                "selected_action_type": "nudge_usage",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["status"], "resolved")

    def test_select_existing_invalid_action_type_400(self):
        resp = self.client.post(
            reverse("review-decisions"),
            {
                "outreach_action": self.action.id,
                "kind": "select_existing",
                "selected_action_type": "not_a_real_type",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_propose_new_valid_returns_201_pending(self):
        resp = self.client.post(
            reverse("review-decisions"),
            {
                "outreach_action": self.action.id,
                "kind": "propose_new",
                "proposed_name": "Renewal outreach",
                "proposed_what": "Reach out about renewal",
                "proposed_when": "Within 2 weeks",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["status"], "pending_engineering")

    def test_propose_new_missing_what_400(self):
        resp = self.client.post(
            reverse("review-decisions"),
            {
                "outreach_action": self.action.id,
                "kind": "propose_new",
                "proposed_name": "Renewal outreach",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_kind_400(self):
        resp = self.client.post(
            reverse("review-decisions"),
            {
                "outreach_action": self.action.id,
                "kind": "bogus",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_select_existing_unknown_type_400(self):
        # "unknown" is what *put* the item in the queue; it isn't a selectable pick.
        resp = self.client.post(
            reverse("review-decisions"),
            {
                "outreach_action": self.action.id,
                "kind": "select_existing",
                "selected_action_type": "unknown",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_duplicate_decision_returns_409(self):
        payload = {
            "outreach_action": self.action.id,
            "kind": "select_existing",
            "selected_action_type": "nudge_usage",
        }
        first = self.client.post(reverse("review-decisions"), payload, format="json")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        # Second decision for the same action (double-click / racing reviewer).
        second = self.client.post(reverse("review-decisions"), payload, format="json")
        self.assertEqual(second.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(ReviewDecision.objects.filter(outreach_action=self.action).count(), 1)

    def test_decision_on_non_review_action_400(self):
        not_human = OutreachAction.objects.create(
            lead=self.lead,
            priority=2,
            action_type="nudge_usage",
            reason="handled automatically",
            needs_human=False,
        )
        resp = self.client.post(
            reverse("review-decisions"),
            {
                "outreach_action": not_human.id,
                "kind": "select_existing",
                "selected_action_type": "nudge_usage",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class ReviewDecisionListTests(AuthenticatedAPITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.lead = make_lead("lead_001")
        # One action per decision: outreach_action is OneToOne.
        cls.action = OutreachAction.objects.create(
            lead=cls.lead,
            priority=1,
            action_type="unknown",
            reason="needs review",
            needs_human=True,
        )
        cls.action2 = OutreachAction.objects.create(
            lead=cls.lead,
            priority=1,
            action_type="unknown",
            reason="needs review",
            needs_human=True,
        )
        cls.resolved = ReviewDecision.objects.create(
            outreach_action=cls.action,
            kind=ReviewDecision.KIND_SELECT,
            status=ReviewDecision.STATUS_RESOLVED,
            selected_action_type="nudge_usage",
        )
        cls.pending = ReviewDecision.objects.create(
            outreach_action=cls.action2,
            kind=ReviewDecision.KIND_PROPOSE,
            status=ReviewDecision.STATUS_PENDING,
            proposed_name="X",
            proposed_what="Y",
        )

    def test_list_newest_first(self):
        resp = self.client.get(reverse("review-decisions"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [row["id"] for row in resp.data]
        self.assertEqual(ids, [self.pending.id, self.resolved.id])

    def test_list_status_filter(self):
        resp = self.client.get(reverse("review-decisions"), {"status": "pending_engineering"})
        ids = [row["id"] for row in resp.data]
        self.assertEqual(ids, [self.pending.id])


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
