"""Leads belong to a user, and that user is the only one who sees them.

``Lead.tenant`` is the tenant id. It is nullable while the book is being
backfilled, so the rule under test has two halves: a lead you own is yours, a
lead nobody owns is everybody's, and a lead someone ELSE owns is invisible --
not a 403, a 404, so nothing leaks about another tenant's book.
"""

from datetime import date
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status

from project.app.models import Lead, OutreachAction
from project.app.services.outreach import plan_outreach
from project.app.tests.tests_auth_utils import AuthenticatedAPITestCase

OTHER_EMAIL = "other-tenant@example.com"


def make_lead(lead_id, tenant=None, **overrides):
    """A lead that classifies to ``complete_onboarding`` -- date-independent."""
    defaults = dict(
        id=lead_id,
        tenant=tenant,
        agency_name=f"Agency {lead_id}",
        contact_name=f"Contact {lead_id}",
        contact_email=f"{lead_id}@example.com",
        contact_phone="555-0100",
        state="CA",
        num_producers=3,
        years_in_business=5,
        estimated_book_size_usd=1_000_000,
        stage="demo_completed",
        signed_up_date=None,
        last_login_date=date(2026, 6, 1),
    )
    defaults.update(overrides)
    return Lead.objects.create(**defaults)


def make_other_tenant():
    return get_user_model().objects.create(username=OTHER_EMAIL, email=OTHER_EMAIL)


def make_action(lead, **overrides):
    defaults = dict(
        lead=lead,
        priority=1,
        action_type="nudge_usage",
        reason="because",
        suggested_copy="Hello there.",
    )
    defaults.update(overrides)
    return OutreachAction.objects.create(**defaults)


class _ProviderStub:
    """Records every prompt phase 3 sends -- the prompt is the only channel that
    names the lead, so "whose book reached the provider" is testable, and a lead
    that is not yours must cost nothing."""

    def __init__(self):
        self.prompts = []

    async def __call__(self, lead, action_type, reason, *, prompt=None, client=None, **_runtime):
        self.prompts.append(prompt or "")
        return (
            "Subject: A quick idea\n\n"
            "Hi there,\n\nI wanted to share one small change that usually helps "
            "agencies get more quotes over the line. Would you have time for a "
            "short call this week?\n\nBest,\nDana"
        )

    def leads_called(self, lead_ids):
        """Which of ``lead_ids`` had their agency name land in a sent prompt."""
        return sorted(
            lead_id for lead_id in lead_ids if any(f"Agency {lead_id}" in p for p in self.prompts)
        )


def _stub_provider(stub):
    return patch("project.app.services.outreach.agenerate_copy", stub)


class LeadTenantQuerySetTests(TestCase):
    """The one definition of the rule: ``Lead.objects.for_tenant``."""

    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create(username="me@example.com")
        self.other = make_other_tenant()
        self.mine = make_lead("lead_mine", tenant=self.user)
        self.theirs = make_lead("lead_theirs", tenant=self.other)
        self.unassigned = make_lead("lead_unassigned")

    def test_a_tenant_sees_their_own_leads_and_the_unassigned_ones(self):
        visible = set(Lead.objects.for_tenant(self.user).values_list("id", flat=True))
        self.assertEqual(visible, {"lead_mine", "lead_unassigned"})

    def test_a_tenant_never_sees_another_tenants_leads(self):
        self.assertNotIn(
            "lead_theirs", set(Lead.objects.for_tenant(self.user).values_list("id", flat=True))
        )

    def test_an_anonymous_caller_sees_no_leads_at_all(self):
        self.assertEqual(Lead.objects.for_tenant(AnonymousUser()).count(), 0)
        self.assertEqual(Lead.objects.for_tenant(None).count(), 0)

    def test_outreach_actions_inherit_the_scope_of_their_lead(self):
        make_action(self.mine)
        make_action(self.theirs)
        make_action(self.unassigned)

        visible = set(
            OutreachAction.objects.for_tenant(self.user).values_list("lead_id", flat=True)
        )
        self.assertEqual(visible, {"lead_mine", "lead_unassigned"})


class LeadListTenantTests(AuthenticatedAPITestCase):
    """GET /api/leads/ — the caller's book, and nobody else's."""

    def setUp(self):
        super().setUp()
        self.other = make_other_tenant()
        make_lead("lead_mine", tenant=self.user)
        make_lead("lead_theirs", tenant=self.other)
        make_lead("lead_unassigned")

    def test_the_lead_list_omits_another_tenants_leads(self):
        resp = self.client.get(reverse("lead-list"))

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual([row["id"] for row in resp.data], ["lead_mine", "lead_unassigned"])


@override_settings(COPY_VERIFY_LEVEL="off")
class ComposeTenantTests(AuthenticatedAPITestCase):
    """POST /api/leads/{id}/compose/ — another tenant's lead does not exist."""

    def setUp(self):
        super().setUp()
        self.other = make_other_tenant()
        self.theirs = make_lead("lead_theirs", tenant=self.other)

    def test_composing_for_another_tenants_lead_is_a_404_and_costs_no_provider_call(self):
        stub = _ProviderStub()
        with _stub_provider(stub):
            resp = self.client.post(reverse("lead-compose", kwargs={"lead_id": self.theirs.id}))

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(resp.data["error"], "unknown_lead")
        self.assertEqual(stub.prompts, [])
        self.assertEqual(OutreachAction.objects.count(), 0)


@override_settings(COPY_VERIFY_LEVEL="off")
class PlanOutreachTenantTests(TestCase):
    """A scoped run spends nothing on a book that is not the caller's."""

    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create(username="me@example.com")
        self.other = make_other_tenant()
        make_lead("lead_mine", tenant=self.user)
        make_lead("lead_theirs", tenant=self.other)
        make_lead("lead_unassigned")

    def test_a_tenant_run_plans_only_that_tenants_and_the_unassigned_leads(self):
        stub = _ProviderStub()
        with _stub_provider(stub):
            planned = plan_outreach(tenant=self.user)

        self.assertEqual(
            stub.leads_called(["lead_mine", "lead_theirs", "lead_unassigned"]),
            ["lead_mine", "lead_unassigned"],
        )
        self.assertEqual(
            sorted(action.lead_id for action in planned), ["lead_mine", "lead_unassigned"]
        )

    def test_a_run_with_no_tenant_still_plans_the_whole_table(self):
        stub = _ProviderStub()
        with _stub_provider(stub):
            planned = plan_outreach()

        self.assertEqual(len(planned), 3)
        self.assertEqual(
            stub.leads_called(["lead_mine", "lead_theirs", "lead_unassigned"]),
            ["lead_mine", "lead_theirs", "lead_unassigned"],
        )


class ReviewTenantTests(AuthenticatedAPITestCase):
    """The inbox and its five lifecycle moves stop at the tenant boundary."""

    def setUp(self):
        super().setUp()
        self.other = make_other_tenant()
        self.mine = make_action(make_lead("lead_mine", tenant=self.user))
        self.theirs = make_action(make_lead("lead_theirs", tenant=self.other))

    def test_the_inbox_omits_actions_about_another_tenants_lead(self):
        resp = self.client.get(reverse("outreach-list"))

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual([row["id"] for row in resp.data["results"]], [self.mine.id])

    def test_every_lifecycle_move_on_another_tenants_action_is_a_404(self):
        for route in ("edit", "verify", "approve", "dismiss", "reopen"):
            with self.subTest(route=route):
                resp = self.client.post(
                    reverse(f"outreach-{route}", kwargs={"pk": self.theirs.id}),
                    {"copy": "Some replacement copy."},
                    format="json",
                )
                self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
                self.assertEqual(resp.data["code"], "not_found")

        self.theirs.refresh_from_db()
        self.assertEqual(self.theirs.status, OutreachAction.STATUS_PENDING)
        self.assertEqual(self.theirs.edited_copy, "")


class BackfillLeadTenantCommandTests(TestCase):
    """``manage.py backfill_lead_tenant``: the nullable-first column's other half."""

    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create(username="me@example.com")
        self.other = make_other_tenant()

    def run_command(self, *args):
        out = StringIO()
        call_command("backfill_lead_tenant", *args, stdout=out)
        return out.getvalue()

    def test_it_assigns_every_unassigned_lead_to_the_named_user(self):
        make_lead("lead_a")
        make_lead("lead_b")

        self.run_command("--user", "me@example.com")

        self.assertEqual(Lead.objects.filter(tenant=self.user).count(), 2)

    def test_it_never_reassigns_a_lead_that_already_has_a_tenant(self):
        make_lead("lead_theirs", tenant=self.other)

        self.run_command("--user", "me@example.com")

        self.assertEqual(Lead.objects.get(id="lead_theirs").tenant, self.other)

    def test_running_it_twice_changes_nothing_the_second_time(self):
        make_lead("lead_a")

        self.run_command("--user", "me@example.com")
        second = self.run_command("--user", "me@example.com")

        self.assertIn("Assigned 0 lead(s)", second)
        self.assertEqual(Lead.objects.get(id="lead_a").tenant, self.user)

    def test_lead_id_narrows_the_backfill_to_the_named_leads(self):
        make_lead("lead_a")
        make_lead("lead_b")

        self.run_command("--user", "me@example.com", "--lead-id", "lead_a")

        self.assertEqual(Lead.objects.get(id="lead_a").tenant, self.user)
        self.assertIsNone(Lead.objects.get(id="lead_b").tenant)

    def test_a_dry_run_reports_the_count_and_writes_nothing(self):
        make_lead("lead_a")

        output = self.run_command("--user", "me@example.com", "--dry-run")

        self.assertIn("Would assign 1 lead(s)", output)
        self.assertIsNone(Lead.objects.get(id="lead_a").tenant)

    def test_an_unknown_user_is_an_error_not_a_silent_no_op(self):
        make_lead("lead_a")

        with self.assertRaises(CommandError):
            self.run_command("--user", "nobody@example.com")

        self.assertIsNone(Lead.objects.get(id="lead_a").tenant)
