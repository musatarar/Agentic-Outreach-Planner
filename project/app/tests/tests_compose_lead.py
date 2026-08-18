"""Composing outreach for ONE client, on demand (MUS-68).

Pins that the scope is real (exactly one lead reaches the provider, asserted on the
stub's call count) and that the unscoped whole-book run is unchanged. The provider seam
is ``outreach.agenerate_copy``, so phase 3 stays exercised.
"""

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status

from project.app.models import DismissedOutreachKey, Lead, OutreachAction
from project.app.services import actions
from project.app.services import dedupe as dedupe_service
from project.app.services.outreach import plan_outreach
from project.app.tests.tests_auth_utils import AuthenticatedAPITestCase


def _make_lead(lead_id, agency_name):
    """A lead that classifies to ``complete_onboarding`` — ``demo_completed`` with no
    signup date is the one date-independent classification, so nothing drifts."""
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


def _unmatched_lead(lead_id="lead_nomatch"):
    """No stage, no dates, no usage -- falls through every rule to UNKNOWN."""
    return Lead.objects.create(
        id=lead_id,
        agency_name="Nomatch Agency",
        contact_name="Pat Quinn",
        contact_email="pat@nowhere.test",
        contact_phone="555-0001",
        state="NV",
        num_producers=1,
        years_in_business=1,
        estimated_book_size_usd=0,
        stage="",
        signed_up_date=None,
    )


def _copy_for(agency):
    """A well-shaped email naming its own lead, so a mix-up is visible."""
    return (
        f"Subject: A quick idea for {agency}\n\n"
        "Hi there,\n\n"
        f"{agency} has been working steadily through the portal, and I wanted to "
        "share one small change that usually helps agencies of this size get more "
        "quotes over the line. It takes about fifteen minutes to walk through, and "
        "your producers can start using it the same day. I would rather show you "
        "than write it all out here, since the useful part is seeing it against "
        "your own book of business. Would you have time for a short call this "
        "week?\n\n"
        "Best,\nDana"
    )


class _ProviderStub:
    """Records every prompt phase 3 sends; the prompt is the only channel that
    identifies the lead, so "who reached the model" is testable."""

    def __init__(self):
        self.prompts = []

    async def __call__(self, lead, action_type, reason, *, prompt=None, client=None, **_runtime):
        self.prompts.append(prompt or "")
        return _copy_for("the agency")

    def agencies_called(self, leads):
        """Which of ``leads`` had their agency name land in a sent prompt."""
        return {
            lead.agency_name for lead in leads if any(lead.agency_name in p for p in self.prompts)
        }


def _stub_provider(stub):
    return patch("project.app.services.outreach.agenerate_copy", stub)


# ---------------------------------------------------------------------------
# The service: plan_outreach(lead_ids=...)
# ---------------------------------------------------------------------------


@override_settings(COPY_VERIFY_LEVEL="off")
class ScopedPlanOutreachTests(TestCase):
    """``lead_ids`` narrows what is classified and what reaches the provider."""

    def setUp(self):
        super().setUp()
        self.alpha = _make_lead("lead_001", "Alpha Agency")
        self.bravo = _make_lead("lead_002", "Bravo Agency")
        self.charlie = _make_lead("lead_003", "Charlie Agency")

    def test_scoping_to_one_lead_writes_exactly_one_row_for_that_lead(self):
        stub = _ProviderStub()
        with _stub_provider(stub):
            planned = plan_outreach(lead_ids=[self.bravo.id])

        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0].lead_id, self.bravo.id)
        self.assertEqual(OutreachAction.objects.count(), 1)
        self.assertEqual(OutreachAction.objects.get().lead_id, self.bravo.id)

    def test_scoping_to_one_lead_sends_exactly_one_prompt_to_the_provider(self):
        stub = _ProviderStub()
        with _stub_provider(stub):
            plan_outreach(lead_ids=[self.bravo.id])

        self.assertEqual(len(stub.prompts), 1)
        self.assertEqual(
            stub.agencies_called([self.alpha, self.bravo, self.charlie]),
            {"Bravo Agency"},
        )

    def test_an_unmatched_lead_still_writes_its_row_and_routes_to_a_human(self):
        # UNKNOWN produces no prompt, so this costs no provider call.
        nomatch = _unmatched_lead()
        stub = _ProviderStub()
        with _stub_provider(stub):
            planned = plan_outreach(lead_ids=[nomatch.id])

        self.assertEqual(stub.prompts, [])
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0].action_type, actions.UNKNOWN)
        self.assertTrue(planned[0].needs_human)

    def test_an_open_recommendation_suppresses_the_scoped_run(self):
        # The suppression rule is consulted before the prompt is built.
        OutreachAction.objects.create(
            lead=self.bravo,
            priority=2,
            action_type=actions.COMPLETE_ONBOARDING,
            reason="already queued",
            suggested_copy="an existing draft",
            status=OutreachAction.STATUS_PENDING,
            dedupe_key=dedupe_service.dedupe_key(self.bravo.id, actions.COMPLETE_ONBOARDING),
        )

        stub = _ProviderStub()
        with _stub_provider(stub):
            planned = plan_outreach(lead_ids=[self.bravo.id])

        self.assertEqual(planned, [])
        self.assertEqual(stub.prompts, [])
        self.assertEqual(OutreachAction.objects.count(), 1)

    def test_a_dismissed_recommendation_suppresses_the_scoped_run(self):
        DismissedOutreachKey.objects.create(
            dedupe_key=dedupe_service.dedupe_key(self.bravo.id, actions.COMPLETE_ONBOARDING),
            lead=self.bravo,
            action_type=actions.COMPLETE_ONBOARDING,
            reason="not_a_fit",
        )

        stub = _ProviderStub()
        with _stub_provider(stub):
            planned = plan_outreach(lead_ids=[self.bravo.id])

        self.assertEqual(planned, [])
        self.assertEqual(stub.prompts, [])
        self.assertEqual(OutreachAction.objects.count(), 0)

    def test_an_unknown_lead_id_plans_nothing_and_calls_nothing(self):
        stub = _ProviderStub()
        with _stub_provider(stub):
            planned = plan_outreach(lead_ids=["lead_does_not_exist"])

        self.assertEqual(planned, [])
        self.assertEqual(stub.prompts, [])
        self.assertEqual(OutreachAction.objects.count(), 0)

    def test_the_whole_book_run_is_unchanged_when_no_scope_is_given(self):
        stub = _ProviderStub()
        with _stub_provider(stub):
            planned = plan_outreach()

        self.assertEqual(len(planned), 3)
        self.assertEqual(len(stub.prompts), 3)
        self.assertEqual(
            stub.agencies_called([self.alpha, self.bravo, self.charlie]),
            {"Alpha Agency", "Bravo Agency", "Charlie Agency"},
        )
        self.assertEqual(OutreachAction.objects.count(), 3)


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


@override_settings(COPY_VERIFY_LEVEL="off")
class ComposeForLeadViewTests(AuthenticatedAPITestCase):
    """POST /api/leads/<lead_id>/compose/ — one client, one press, one mail."""

    def setUp(self):
        super().setUp()
        self.alpha = _make_lead("lead_001", "Alpha Agency")
        self.bravo = _make_lead("lead_002", "Bravo Agency")

    def url_for(self, lead_id):
        return reverse("lead-compose", kwargs={"lead_id": lead_id})

    def test_composing_returns_the_action_in_the_shared_action_shape(self):
        stub = _ProviderStub()
        with _stub_provider(stub):
            resp = self.client.post(self.url_for(self.bravo.id))

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # The same shape OutreachActionSerializer emits everywhere else.
        self.assertEqual(
            set(resp.data.keys()),
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
        self.assertEqual(resp.data["lead"]["id"], self.bravo.id)
        self.assertEqual(resp.data["lead"]["agency_name"], "Bravo Agency")
        self.assertTrue(resp.data["suggested_copy"])

    def test_composing_touches_only_the_requested_client(self):
        stub = _ProviderStub()
        with _stub_provider(stub):
            self.client.post(self.url_for(self.bravo.id))

        self.assertEqual(len(stub.prompts), 1)
        self.assertEqual(stub.agencies_called([self.alpha, self.bravo]), {"Bravo Agency"})
        self.assertEqual(
            list(OutreachAction.objects.values_list("lead_id", flat=True)), [self.bravo.id]
        )

    def test_an_unknown_client_is_a_404_and_writes_nothing(self):
        stub = _ProviderStub()
        with _stub_provider(stub):
            resp = self.client.post(self.url_for("lead_does_not_exist"))

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(resp.data["error"], "unknown_lead")
        self.assertEqual(stub.prompts, [])
        self.assertEqual(OutreachAction.objects.count(), 0)

    def test_a_client_with_an_open_recommendation_is_a_409_and_costs_nothing(self):
        OutreachAction.objects.create(
            lead=self.bravo,
            priority=2,
            action_type=actions.COMPLETE_ONBOARDING,
            reason="already queued",
            suggested_copy="an existing draft",
            status=OutreachAction.STATUS_PENDING,
            dedupe_key=dedupe_service.dedupe_key(self.bravo.id, actions.COMPLETE_ONBOARDING),
        )

        stub = _ProviderStub()
        with _stub_provider(stub):
            resp = self.client.post(self.url_for(self.bravo.id))

        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(resp.data["error"], "no_new_recommendation")
        self.assertEqual(stub.prompts, [])
        self.assertEqual(OutreachAction.objects.count(), 1)

    def test_a_dismissed_recommendation_is_a_409_and_costs_nothing(self):
        DismissedOutreachKey.objects.create(
            dedupe_key=dedupe_service.dedupe_key(self.bravo.id, actions.COMPLETE_ONBOARDING),
            lead=self.bravo,
            action_type=actions.COMPLETE_ONBOARDING,
            reason="not_a_fit",
        )

        stub = _ProviderStub()
        with _stub_provider(stub):
            resp = self.client.post(self.url_for(self.bravo.id))

        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(resp.data["error"], "no_new_recommendation")
        self.assertEqual(stub.prompts, [])
        self.assertEqual(OutreachAction.objects.count(), 0)

    def test_an_unmatched_client_still_gets_a_row_routed_to_a_human(self):
        nomatch = _unmatched_lead()
        stub = _ProviderStub()
        with _stub_provider(stub):
            resp = self.client.post(self.url_for(nomatch.id))

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["action_type"], actions.UNKNOWN)
        self.assertTrue(resp.data["needs_human"])
        self.assertEqual(stub.prompts, [])

    def test_the_endpoint_requires_a_session(self):
        self.client.logout()
        resp = self.client.post(self.url_for(self.bravo.id))
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
