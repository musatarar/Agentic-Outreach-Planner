"""Assessing the next action for ONE client (MUS-70), deterministic half only.

Pins the two properties that make Assess safe to press: it costs nothing (no
provider call, asserted on the stubs' call counts) and it changes nothing (no
outreach row, no dedupe ledger write). The advisory half lands separately.
"""

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from rest_framework import status

from project.app.models import DismissedOutreachKey, Lead, LeadAssessment, OutreachAction
from project.app.services import actions
from project.app.services import dedupe as dedupe_service
from project.app.services.assess import assess_lead
from project.app.tests_auth_utils import AuthenticatedAPITestCase


def _make_lead(lead_id="lead_001", agency_name="Alpha Agency"):
    """Classifies to ``complete_onboarding`` — ``demo_completed`` with no signup
    date is the one date-independent classification, so nothing drifts."""
    return Lead.objects.create(
        id=lead_id,
        agency_name=agency_name,
        contact_name=f"Contact {lead_id}",
        contact_email=f"{lead_id}@example.com",
        contact_phone="555-0100",
        state="CA",
        num_producers=3,
        years_in_business=8,
        estimated_book_size_usd=1_000_000,
        stage="demo_completed",
        signed_up_date=None,
    )


class _NeverCalled:
    """Any provider seam this stands in for; a call is the failure."""

    def __init__(self):
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("the deterministic assessment must not reach a provider")


def _no_provider(stub):
    """Both provider seams at once: the planner's generator and the client factory."""
    return (
        patch("project.app.services.outreach.agenerate_copy", stub),
        patch("project.app.services.llm.get_llm_client", stub),
    )


class _ProviderSilenceMixin:
    def assertNoProviderCall(self, stub):
        self.assertEqual(stub.calls, 0)


# ---------------------------------------------------------------------------
# The service: assess_lead()
# ---------------------------------------------------------------------------


class AssessLeadServiceTests(_ProviderSilenceMixin, TestCase):
    """``assess_lead`` writes one row from the rules alone."""

    def setUp(self):
        super().setUp()
        self.lead = _make_lead()

    def _assess(self):
        stub = _NeverCalled()
        generate, client = _no_provider(stub)
        with generate, client:
            assessment = assess_lead(self.lead)
        self.assertNoProviderCall(stub)
        return assessment

    def test_it_persists_the_rules_answer_and_the_whole_explain_envelope(self):
        assessment = self._assess()

        self.assertEqual(assessment.lead_id, self.lead.id)
        self.assertEqual(assessment.action_type, actions.COMPLETE_ONBOARDING)
        self.assertIn(assessment.priority, (1, 2, 3))
        self.assertTrue(assessment.reason)
        # The envelope is explain()'s, not a reshaped subset of it.
        self.assertEqual(assessment.rule_trace["action"]["value"], assessment.action_type)
        self.assertEqual(assessment.rule_trace["priority"]["value"], assessment.priority)
        self.assertIn("signals", assessment.rule_trace["priority"])
        self.assertIn("rejected_rules", assessment.rule_trace["action"])

    def test_the_advisory_is_absent_and_says_so(self):
        assessment = self._assess()

        self.assertEqual(assessment.advisory_text, "")
        self.assertEqual(assessment.advisory_status, LeadAssessment.ADVISORY_DISABLED)
        self.assertEqual(assessment.verification, {})

    def test_it_writes_no_outreach_row_and_no_dedupe_ledger_row(self):
        self._assess()

        self.assertEqual(OutreachAction.objects.count(), 0)
        self.assertEqual(DismissedOutreachKey.objects.count(), 0)

    def test_every_press_writes_its_own_row(self):
        self._assess()
        self._assess()

        self.assertEqual(LeadAssessment.objects.filter(lead=self.lead).count(), 2)

    def test_an_open_recommendation_is_reported_not_obeyed(self):
        open_row = OutreachAction.objects.create(
            lead=self.lead,
            priority=2,
            action_type=actions.COMPLETE_ONBOARDING,
            reason="already queued",
            suggested_copy="an existing draft",
            status=OutreachAction.STATUS_PENDING,
            dedupe_key=dedupe_service.dedupe_key(self.lead.id, actions.COMPLETE_ONBOARDING),
        )

        assessment = self._assess()

        self.assertEqual(assessment.open_outreach_action_id, open_row.id)
        self.assertFalse(assessment.dismissed)
        self.assertEqual(assessment.action_type, actions.COMPLETE_ONBOARDING)

    def test_a_dismissed_key_is_reported_not_obeyed(self):
        DismissedOutreachKey.objects.create(
            dedupe_key=dedupe_service.dedupe_key(self.lead.id, actions.COMPLETE_ONBOARDING),
            lead=self.lead,
            action_type=actions.COMPLETE_ONBOARDING,
            reason="not_a_fit",
        )

        assessment = self._assess()

        self.assertTrue(assessment.dismissed)
        self.assertIsNone(assessment.open_outreach_action_id)
        self.assertEqual(assessment.action_type, actions.COMPLETE_ONBOARDING)

    def test_a_revoked_dismissal_no_longer_counts_as_dismissed(self):
        from django.utils import timezone

        DismissedOutreachKey.objects.create(
            dedupe_key=dedupe_service.dedupe_key(self.lead.id, actions.COMPLETE_ONBOARDING),
            lead=self.lead,
            action_type=actions.COMPLETE_ONBOARDING,
            reason="not_a_fit",
            revoked_at=timezone.now(),
        )

        self.assertFalse(self._assess().dismissed)

    def test_a_dismissal_for_a_different_action_type_does_not_count(self):
        DismissedOutreachKey.objects.create(
            dedupe_key=dedupe_service.dedupe_key(self.lead.id, actions.NUDGE_USAGE),
            lead=self.lead,
            action_type=actions.NUDGE_USAGE,
            reason="not_a_fit",
        )

        self.assertFalse(self._assess().dismissed)

    def test_a_failed_generation_row_is_not_an_open_recommendation(self):
        # A real action type + no copy + needs_human records a failed attempt,
        # not something the AE has queued.
        OutreachAction.objects.create(
            lead=self.lead,
            priority=2,
            action_type=actions.COMPLETE_ONBOARDING,
            reason="generation gave up",
            suggested_copy="",
            needs_human=True,
            status=OutreachAction.STATUS_PENDING,
            dedupe_key=dedupe_service.dedupe_key(self.lead.id, actions.COMPLETE_ONBOARDING),
        )

        self.assertIsNone(self._assess().open_outreach_action_id)

    def test_a_closed_outreach_row_is_not_an_open_recommendation(self):
        OutreachAction.objects.create(
            lead=self.lead,
            priority=2,
            action_type=actions.COMPLETE_ONBOARDING,
            reason="already sent",
            suggested_copy="a sent draft",
            status=OutreachAction.STATUS_SENT,
            dedupe_key=dedupe_service.dedupe_key(self.lead.id, actions.COMPLETE_ONBOARDING),
        )

        self.assertIsNone(self._assess().open_outreach_action_id)


# ---------------------------------------------------------------------------
# The endpoint: POST /api/leads/{lead_id}/assess/
# ---------------------------------------------------------------------------


class AssessLeadEndpointTests(_ProviderSilenceMixin, AuthenticatedAPITestCase):
    """200 always, 404 for an unknown client, and never a 409."""

    def setUp(self):
        super().setUp()
        self.lead = _make_lead()

    def _post(self, lead_id=None):
        stub = _NeverCalled()
        generate, client = _no_provider(stub)
        with generate, client:
            response = self.client.post(
                reverse("lead-assess", args=[lead_id or self.lead.id]), {}, format="json"
            )
        self.assertNoProviderCall(stub)
        return response

    def test_it_answers_200_with_the_deterministic_assessment(self):
        response = self._post()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body["lead"]["id"], self.lead.id)
        self.assertEqual(body["action_type"], actions.COMPLETE_ONBOARDING)
        self.assertEqual(body["priority"], body["rule_trace"]["priority"]["value"])
        self.assertTrue(body["reason"])
        self.assertEqual(body["advisory"]["status"], LeadAssessment.ADVISORY_DISABLED)
        self.assertEqual(body["advisory"]["text"], "")

    def test_it_persists_exactly_one_row_per_press(self):
        response = self._post()

        self.assertEqual(LeadAssessment.objects.count(), 1)
        self.assertEqual(response.json()["id"], LeadAssessment.objects.get().id)

    def test_an_unknown_client_is_a_404_and_writes_nothing(self):
        response = self._post(lead_id="lead_does_not_exist")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.json()["error"], "unknown_lead")
        self.assertEqual(LeadAssessment.objects.count(), 0)

    def test_an_open_recommendation_still_answers_200_and_reports_the_row(self):
        open_row = OutreachAction.objects.create(
            lead=self.lead,
            priority=2,
            action_type=actions.COMPLETE_ONBOARDING,
            reason="already queued",
            suggested_copy="an existing draft",
            status=OutreachAction.STATUS_PENDING,
            dedupe_key=dedupe_service.dedupe_key(self.lead.id, actions.COMPLETE_ONBOARDING),
        )

        response = self._post()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        queue_state = response.json()["queue_state"]
        self.assertEqual(queue_state["open_outreach_action_id"], open_row.id)
        self.assertFalse(queue_state["dismissed"])

    def test_a_dismissed_key_still_answers_200_and_reports_the_dismissal(self):
        DismissedOutreachKey.objects.create(
            dedupe_key=dedupe_service.dedupe_key(self.lead.id, actions.COMPLETE_ONBOARDING),
            lead=self.lead,
            action_type=actions.COMPLETE_ONBOARDING,
            reason="not_a_fit",
        )

        response = self._post()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        queue_state = response.json()["queue_state"]
        self.assertTrue(queue_state["dismissed"])
        self.assertIsNone(queue_state["open_outreach_action_id"])

    def test_it_leaves_the_queue_and_the_dedupe_ledger_alone(self):
        self._post()

        self.assertEqual(OutreachAction.objects.count(), 0)
        self.assertEqual(DismissedOutreachKey.objects.count(), 0)

    def test_it_is_behind_the_session_gate(self):
        self.client.logout()

        response = self.client.post(reverse("lead-assess", args=[self.lead.id]), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(LeadAssessment.objects.count(), 0)
