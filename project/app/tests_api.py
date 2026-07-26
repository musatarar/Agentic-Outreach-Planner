from datetime import date
from unittest.mock import patch

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from project.app.models import Lead, OutreachAction, ReviewDecision


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


class OutreachReportViewTests(APITestCase):
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


class ReviewQueueViewTests(APITestCase):
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
        # Older action for same lead excluded (dedupe keeps newest).
        self.assertNotIn(self.lead1_old.id, ids)
        # Decided / non-human excluded.
        self.assertNotIn(self.lead2_action.id, ids)
        self.assertNotIn(self.lead3_action.id, ids)

    def test_action_options_excludes_unknown(self):
        resp = self.client.get(reverse("review-queue"))
        options = resp.data["action_options"]
        values = [o["value"] for o in options]
        self.assertNotIn("unknown", values)
        for opt in options:
            self.assertEqual(set(opt.keys()), {"value", "label", "urgency"})


class ReviewDecisionCreateTests(APITestCase):
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


class ReviewDecisionListTests(APITestCase):
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
