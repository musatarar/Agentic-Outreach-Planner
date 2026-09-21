from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status

from project.app.models import (
    Lead,
    OutreachAction,
    Shape,
)
from project.app.tests.tests_auth_utils import AuthenticatedAPITestCase


def make_lead(lead_id, *, owner=None, **overrides):
    data = dict(
        agency_name=f"Agency {lead_id}",
        contact_name=f"Contact {lead_id}",
        contact_email=f"{lead_id}@example.com",
        contact_phone="555-0100",
        state="CA",
        num_producers=3,
        years_in_business=5,
        estimated_book_size_usd=1_000_000,
        stage="active_trial",
        signed_up_date="2026-01-01",
        last_login_date="2026-06-01",
        quotes_created=10,
        quotes_submitted=4,
        deals_closed=1,
        last_contacted_date="2026-05-01",
        hubspot_notes="",
    )
    data.update(overrides)
    return Lead.objects.create(id=lead_id, owner=owner, data=data)


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
        # The full lead serializer carries the blob whole; naming a column is
        # the reader's shape's job, not this endpoint's.
        first = resp.data[0]
        self.assertEqual(set(first), {"id", "owner", "data"})
        self.assertEqual(first["data"]["agency_name"], "Alpha")


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
        self.assertEqual(set(row["lead"].keys()), {"id", "data", "recent_events"})


class ShapeViewTests(AuthenticatedAPITestCase):
    """GET/PUT /api/shape/ — one declaration per user, the caller's own."""

    def _put(self, **overrides):
        payload = {
            "lead_columns": [
                {"name": "agency_name", "type": "text", "lead_authored": False},
                {"name": "contact_name", "type": "text", "lead_authored": False},
                {"name": "crm_notes", "type": "text", "lead_authored": True},
            ],
            "event_columns": [{"name": "kind", "type": "text"}],
            "roles": {"contact_name": "contact_name", "agency_name": "agency_name"},
        }
        payload.update(overrides)
        return self.client.put(reverse("shape"), payload, format="json")

    def test_a_user_who_has_declared_nothing_reads_an_empty_shape(self):
        resp = self.client.get(reverse("shape"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["lead_columns"], [])
        self.assertEqual(resp.data["roles"], {})

    def test_putting_a_shape_stores_it_and_reads_back(self):
        self.assertEqual(self._put().status_code, status.HTTP_200_OK)
        resp = self.client.get(reverse("shape"))
        self.assertEqual([c["name"] for c in resp.data["lead_columns"]][-1], "crm_notes")
        self.assertEqual(resp.data["event_columns"], [{"name": "kind", "type": "text"}])

    def test_a_second_put_replaces_the_first_rather_than_adding_one(self):
        self._put()
        self._put(event_columns=[])
        self.assertEqual(Shape.objects.filter(owner=self.user).count(), 1)
        self.assertEqual(self.client.get(reverse("shape")).data["event_columns"], [])

    def test_the_shape_is_the_callers_own(self):
        other = get_user_model().objects.create(username="other@example.com")
        Shape.objects.create(
            owner=other,
            lead_columns=[{"name": "somebody_elses", "type": "text", "lead_authored": False}],
        )
        resp = self.client.get(reverse("shape"))
        self.assertEqual(resp.data["lead_columns"], [])

    def test_an_unknown_column_type_is_refused(self):
        resp = self._put(
            lead_columns=[{"name": "agency_name", "type": "colour", "lead_authored": False}]
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("lead_columns", resp.data["detail"])

    def test_a_lead_column_without_lead_authored_is_refused(self):
        resp = self._put(
            lead_columns=[
                {"name": "agency_name", "type": "text"},
                {"name": "contact_name", "type": "text", "lead_authored": False},
            ]
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("lead_authored", resp.data["detail"])

    def test_a_role_naming_an_undeclared_column_is_refused(self):
        resp = self._put(roles={"contact_name": "nobody", "agency_name": "agency_name"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("roles", resp.data["detail"])

    def test_a_role_on_a_lead_authored_column_is_refused(self):
        # It would put lead-controlled text into the trusted record.
        resp = self._put(roles={"contact_name": "crm_notes", "agency_name": "agency_name"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("roles", resp.data["detail"])

    def test_a_duplicate_column_name_is_refused(self):
        resp = self._put(
            lead_columns=[
                {"name": "agency_name", "type": "text", "lead_authored": False},
                {"name": "contact_name", "type": "text", "lead_authored": False},
                {"name": "agency_name", "type": "text", "lead_authored": False},
            ]
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_event_column_cannot_claim_the_structural_timestamp(self):
        resp = self._put(event_columns=[{"name": "timestamp", "type": "date"}])
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("event_columns", resp.data["detail"])
