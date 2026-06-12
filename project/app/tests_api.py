from datetime import date
from unittest.mock import patch

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from project.app.models import Lead, OutreachAction


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


class LeadListViewTests(APITestCase):
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


class OutreachListViewTests(APITestCase):
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
        # One row per lead (most recent for lead1), so 2 rows total.
        self.assertEqual(len(resp.data), 2)

        ids = [row["id"] for row in resp.data]
        self.assertNotIn(self.old.id, ids)
        self.assertIn(self.recent.id, ids)

        # Ordered by priority ascending: lead2 (priority 2) before lead1 (priority 3).
        self.assertEqual(
            [row["priority"] for row in resp.data], [2, 3]
        )
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


class OutreachRunViewTests(APITestCase):
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
        # Ordered by priority ascending.
        self.assertEqual([row["priority"] for row in resp.data], [1, 3])
        self.assertEqual(resp.data[0]["id"], a_high.id)
        self.assertEqual(resp.data[1]["id"], a_low.id)
        self.assertEqual(resp.data[0]["action_type"], "follow_up_after_hold")
        self.assertEqual(resp.data[0]["lead"]["agency_name"], "Alpha")
