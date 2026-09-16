"""Workspaces: what one tenant can see, plan and change — and what it cannot.

Two workspaces exist in most of these tests, each with its own lead, and the
question is always the same: does anything belonging to the other one leak in?
The provider seam is stubbed everywhere (``outreach.agenerate_copy``), so a
refusal that still calls the model is visible as a call count.
"""

from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.test import APIClient

from project.app import urls as app_urls
from project.app.models import (
    DismissedOutreachKey,
    Event,
    Lead,
    OutreachAction,
    Tenant,
    TenantMembership,
)
from project.app.permissions import HasTenant
from project.app.services import dedupe
from project.app.services.outreach import plan_outreach
from project.app.services.tenancy import (
    add_member,
    create_tenant,
    tenant_for_user,
    user_for_email,
)
from project.app.tests.tests_auth_utils import AuthenticatedAPITestCase

# The endpoints that serve nobody's data, so they carry no tenant requirement.
AUTH_ROUTE_NAMES = {"auth-request-link", "auth-consume", "auth-logout", "auth-me"}

GROUNDED_COPY = (
    "Subject: Volume pricing ahead of your next milestone\n\n"
    "Hi Priya,\n\n"
    "You have closed 6 deals from 14 quotes submitted so far, which puts the team "
    "well on the way to the volume-pricing conversation your notes mention. On a "
    "book that size that pace is genuinely impressive, and it is usually the point "
    "where agencies start asking what changes at the next tier. I would rather walk "
    "you through it than write it all out here, because the useful part is seeing "
    "the numbers against your own book and the way your producers actually work "
    "through submissions each week. Would you have twenty minutes this week to talk "
    "it through?\n\n"
    "Best,\nDana"
)


def make_tenant(slug, name=None):
    return create_tenant(slug, name or f"{slug.title()} workspace")


def make_lead(tenant, lead_id, **overrides):
    """A power user (deals >= 5, submissions >= 10): a date-independent
    classification, so nothing here drifts with the wall clock."""
    defaults = dict(
        tenant=tenant,
        id=lead_id,
        agency_name=f"Agency {lead_id}",
        contact_name="Priya Nair",
        contact_email=f"{lead_id}@example.com",
        contact_phone="555-0100",
        state="CO",
        num_producers=4,
        years_in_business=5,
        estimated_book_size_usd=1_400_000,
        stage="active_trial",
        signed_up_date=date(2026, 1, 1),
        last_login_date=date(2026, 6, 1),
        quotes_created=19,
        quotes_submitted=14,
        deals_closed=6,
        last_contacted_date=date(2026, 5, 1),
        hubspot_notes="",
    )
    defaults.update(overrides)
    return Lead.objects.create(**defaults)


def make_action(lead, **overrides):
    defaults = dict(
        lead=lead,
        priority=2,
        action_type="power_user_reward",
        reason="A power user worth rewarding.",
        suggested_copy=GROUNDED_COPY,
        dedupe_key=dedupe.dedupe_key(lead.id, "power_user_reward"),
    )
    defaults.update(overrides)
    return OutreachAction.objects.create(**defaults)


def _stub(copy=GROUNDED_COPY):
    return patch("project.app.services.outreach.agenerate_copy", return_value=copy)


# ---------------------------------------------------------------------------
# The service layer
# ---------------------------------------------------------------------------


class TenantResolutionTests(TestCase):
    """``tenant_for_user`` is the only answer to "whose data is this?"."""

    def test_a_user_with_a_membership_resolves_to_their_workspace(self):
        tenant = make_tenant("alpha")
        user = user_for_email("someone@example.com")
        add_member(tenant, "someone@example.com")

        self.assertEqual(tenant_for_user(user), tenant)

    def test_a_user_with_no_membership_resolves_to_nothing(self):
        user = user_for_email("nobody@example.com")

        self.assertIsNone(tenant_for_user(user))

    def test_an_anonymous_caller_resolves_to_nothing(self):
        from django.contrib.auth.models import AnonymousUser

        self.assertIsNone(tenant_for_user(AnonymousUser()))
        self.assertIsNone(tenant_for_user(None))

    def test_resolving_a_tenant_costs_one_query(self):
        tenant = make_tenant("alpha")
        add_member(tenant, "someone@example.com")
        user = get_user_model().objects.get(username="someone@example.com")

        with self.assertNumQueries(1):
            # select_related: reading the tenant's name must not cost a second.
            self.assertEqual(tenant_for_user(user).name, tenant.name)


class CreateTenantServiceTests(TestCase):
    def test_creating_the_same_slug_twice_returns_the_same_workspace(self):
        first = create_tenant("alpha", "Alpha")
        second = create_tenant("alpha", "Alpha")

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Tenant.objects.filter(slug="alpha").count(), 1)

    def test_a_new_name_renames_the_existing_workspace(self):
        create_tenant("alpha", "Alpha")

        renamed = create_tenant("alpha", "Alpha Insurance")

        self.assertEqual(renamed.name, "Alpha Insurance")
        self.assertEqual(Tenant.objects.get(slug="alpha").name, "Alpha Insurance")


class AddMemberServiceTests(TestCase):
    def test_adding_a_member_creates_the_user_without_a_usable_password(self):
        tenant = make_tenant("alpha")

        membership = add_member(tenant, "new@example.com")

        self.assertEqual(membership.tenant, tenant)
        self.assertFalse(membership.user.has_usable_password())

    def test_adding_the_same_member_twice_is_one_membership(self):
        tenant = make_tenant("alpha")

        first = add_member(tenant, "new@example.com")
        second = add_member(tenant, "new@example.com")

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(TenantMembership.objects.count(), 1)

    def test_moving_a_member_to_another_workspace_is_refused(self):
        alpha = make_tenant("alpha")
        bravo = make_tenant("bravo")
        add_member(alpha, "new@example.com")

        with self.assertRaises(ValueError) as caught:
            add_member(bravo, "new@example.com")

        self.assertIn("alpha", str(caught.exception))
        self.assertIn("bravo", str(caught.exception))
        self.assertEqual(TenantMembership.objects.get().tenant, alpha)


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------


@override_settings(COPY_VERIFY_LEVEL="off")
class ScopedPlannerTests(TestCase):
    def setUp(self):
        super().setUp()
        self.alpha = make_tenant("alpha")
        self.bravo = make_tenant("bravo")
        self.alpha_lead = make_lead(self.alpha, "lead_alpha")
        self.bravo_lead = make_lead(self.bravo, "lead_bravo")

    def test_a_run_plans_only_the_given_workspaces_leads(self):
        with _stub():
            planned = plan_outreach(self.alpha)

        self.assertEqual([action.lead_id for action in planned], ["lead_alpha"])
        self.assertEqual(
            list(OutreachAction.objects.values_list("lead_id", flat=True)), ["lead_alpha"]
        )

    def test_another_workspaces_lead_never_reaches_the_provider(self):
        prompts = []

        async def record(lead, action_type, reason, *, prompt=None, client=None, **_runtime):
            prompts.append(prompt or "")
            return GROUNDED_COPY

        with patch("project.app.services.outreach.agenerate_copy", record):
            plan_outreach(self.alpha)

        self.assertEqual(len(prompts), 1)
        self.assertNotIn(self.bravo_lead.agency_name, "".join(prompts))

    def test_naming_another_workspaces_lead_plans_nothing(self):
        with _stub():
            planned = plan_outreach(self.alpha, lead_ids=[self.bravo_lead.id])

        self.assertEqual(planned, [])
        self.assertEqual(OutreachAction.objects.count(), 0)

    def test_a_pending_action_in_another_workspace_does_not_suppress_this_one(self):
        # Same action type, same dedupe shape -- but a different lead, so the
        # open-item rule must not reach across the workspace boundary.
        make_action(self.bravo_lead)

        with _stub():
            planned = plan_outreach(self.alpha)

        self.assertEqual([action.lead_id for action in planned], ["lead_alpha"])

    def test_a_dismissal_in_another_workspace_does_not_suppress_this_one(self):
        DismissedOutreachKey.objects.create(
            dedupe_key=dedupe.dedupe_key(self.bravo_lead.id, "power_user_reward"),
            lead=self.bravo_lead,
            action_type="power_user_reward",
        )

        with _stub():
            planned = plan_outreach(self.alpha)

        self.assertEqual([action.lead_id for action in planned], ["lead_alpha"])

    def test_planning_without_a_tenant_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            plan_outreach(None)

        self.assertIn("tenant", str(caught.exception))

    def test_a_refused_run_makes_no_provider_call_and_writes_nothing(self):
        with _stub() as stub:
            with self.assertRaises(ValueError):
                plan_outreach(None)

        self.assertEqual(stub.call_count, 0)
        self.assertEqual(OutreachAction.objects.count(), 0)


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------


class TenantAPITestCase(AuthenticatedAPITestCase):
    """The signed-in user is in ``self.tenant``; ``other`` is somebody else's."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.other = make_tenant("other")
        self.mine = make_lead(self.tenant, "lead_mine")
        self.theirs = make_lead(self.other, "lead_theirs")

    def tearDown(self):
        cache.clear()
        super().tearDown()


class ScopedLeadEndpointTests(TenantAPITestCase):
    def test_the_lead_list_returns_only_this_workspaces_leads(self):
        resp = self.client.get(reverse("lead-list"))

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual([row["id"] for row in resp.data], ["lead_mine"])

    def test_composing_for_another_workspaces_lead_is_an_unknown_lead(self):
        with _stub() as stub:
            resp = self.client.post(reverse("lead-compose", args=[self.theirs.id]))

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(resp.data["error"], "unknown_lead")
        # The refusal is indistinguishable from a lead that does not exist, and
        # it costs nothing at the provider.
        self.assertEqual(stub.call_count, 0)
        self.assertEqual(OutreachAction.objects.count(), 0)

    @override_settings(COPY_VERIFY_LEVEL="off")
    def test_running_the_planner_plans_only_this_workspace(self):
        with _stub():
            resp = self.client.post(reverse("outreach-run"))

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual([row["lead"]["id"] for row in resp.data], ["lead_mine"])


class ScopedReviewEndpointTests(TenantAPITestCase):
    def setUp(self):
        super().setUp()
        self.my_action = make_action(self.mine)
        self.their_action = make_action(self.theirs)

    def test_the_inbox_lists_only_this_workspaces_actions(self):
        results = self.client.get(reverse("outreach-list")).data["results"]

        self.assertEqual([row["id"] for row in results], [self.my_action.id])

    def test_every_mutation_on_another_workspaces_action_is_a_404(self):
        bodies = {
            "outreach-edit": {"copy": "Anything at all, at length, for the verifier."},
            "outreach-verify": {"copy": GROUNDED_COPY},
            "outreach-approve": {},
            "outreach-dismiss": {"reason": "not_a_fit"},
            "outreach-reopen": {},
        }
        for name, body in bodies.items():
            with self.subTest(endpoint=name):
                resp = self.client.post(
                    reverse(name, args=[self.their_action.id]), body, format="json"
                )

                self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
                self.assertEqual(resp.data["code"], "not_found")

    def test_another_workspaces_action_is_left_untouched_by_the_refusal(self):
        self.client.post(
            reverse("outreach-dismiss", args=[self.their_action.id]),
            {"reason": "not_a_fit"},
            format="json",
        )

        self.their_action.refresh_from_db()
        self.assertEqual(self.their_action.status, OutreachAction.STATUS_PENDING)
        self.assertEqual(DismissedOutreachKey.objects.count(), 0)

    def test_this_workspaces_own_action_still_moves(self):
        resp = self.client.post(reverse("outreach-dismiss", args=[self.my_action.id]), {})

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.my_action.refresh_from_db()
        self.assertEqual(self.my_action.status, OutreachAction.STATUS_DISMISSED)


class NoMembershipTests(TestCase):
    """A signed-in user with no workspace sees nothing, and is told why."""

    client_class = APIClient

    def setUp(self):
        super().setUp()
        cache.clear()
        self.user = user_for_email("orphan@example.com")
        self.client.force_login(self.user)

    def tearDown(self):
        cache.clear()
        super().tearDown()

    def test_the_lead_list_is_forbidden_with_no_tenant(self):
        resp = self.client.get(reverse("lead-list"))

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data["code"], "no_tenant")

    def test_the_outreach_list_is_forbidden_with_no_tenant(self):
        resp = self.client.get(reverse("outreach-list"))

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data["code"], "no_tenant")

    def test_running_the_planner_is_forbidden_with_no_tenant(self):
        with _stub() as stub:
            resp = self.client.post(reverse("outreach-run"))

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data["code"], "no_tenant")
        self.assertEqual(stub.call_count, 0)

    def test_an_anonymous_caller_is_still_unauthenticated_not_forbidden(self):
        anonymous = APIClient()

        resp = anonymous.get(reverse("lead-list"))

        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(resp.data["code"], "not_authenticated")

    def test_me_reports_a_null_workspace(self):
        resp = self.client.get(reverse("auth-me"))

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIsNone(resp.data["tenant"])


class MeCarriesTheWorkspaceTests(AuthenticatedAPITestCase):
    def test_me_names_the_workspace_the_caller_belongs_to(self):
        resp = self.client.get(reverse("auth-me"))

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["tenant"], {"slug": self.tenant.slug, "name": self.tenant.name})


class EveryDataEndpointIsScopedTests(TestCase):
    """The rule, enforced on the URL map: a new data endpoint cannot forget it.

    Auth endpoints are exempt by name -- they serve the session, not a book of
    leads.
    """

    def test_every_non_auth_endpoint_requires_a_tenant(self):
        for pattern in app_urls.urlpatterns:
            if pattern.name in AUTH_ROUTE_NAMES:
                continue
            with self.subTest(route=pattern.name):
                permissions = pattern.callback.cls.permission_classes
                self.assertIn(HasTenant, permissions)

    def test_no_data_endpoint_is_open_to_anyone(self):
        for pattern in app_urls.urlpatterns:
            if pattern.name in AUTH_ROUTE_NAMES:
                continue
            with self.subTest(route=pattern.name):
                self.assertNotIn(AllowAny, pattern.callback.cls.permission_classes)

    def test_the_exempt_list_is_exactly_the_auth_endpoints(self):
        # A route added to the exemption set above without being an auth route
        # would silently opt itself out of tenancy.
        named = {pattern.name for pattern in app_urls.urlpatterns}
        self.assertTrue(AUTH_ROUTE_NAMES.issubset(named))
        self.assertTrue(all(name.startswith("auth-") for name in AUTH_ROUTE_NAMES))


# ---------------------------------------------------------------------------
# Management commands
# ---------------------------------------------------------------------------


class CreateTenantCommandTests(TestCase):
    def test_it_creates_the_workspace(self):
        call_command("create_tenant", "alpha", name="Alpha")

        self.assertEqual(Tenant.objects.get(slug="alpha").name, "Alpha")

    def test_running_it_twice_creates_one_workspace(self):
        call_command("create_tenant", "alpha", name="Alpha")
        call_command("create_tenant", "alpha", name="Alpha")

        self.assertEqual(Tenant.objects.filter(slug="alpha").count(), 1)


class AddTenantMemberCommandTests(TestCase):
    def setUp(self):
        super().setUp()
        self.alpha = make_tenant("alpha")

    def test_it_creates_the_user_and_the_membership(self):
        call_command("add_tenant_member", "alpha", "new@example.com")

        user = get_user_model().objects.get(username="new@example.com")
        self.assertEqual(tenant_for_user(user), self.alpha)

    def test_it_adds_several_addresses_at_once(self):
        call_command("add_tenant_member", "alpha", "one@example.com", "two@example.com")

        self.assertEqual(TenantMembership.objects.filter(tenant=self.alpha).count(), 2)

    def test_running_it_twice_leaves_one_membership(self):
        call_command("add_tenant_member", "alpha", "new@example.com")
        call_command("add_tenant_member", "alpha", "new@example.com")

        self.assertEqual(TenantMembership.objects.count(), 1)

    def test_it_refuses_to_move_someone_between_workspaces(self):
        make_tenant("bravo")
        call_command("add_tenant_member", "alpha", "new@example.com")

        with self.assertRaises(CommandError) as caught:
            call_command("add_tenant_member", "bravo", "new@example.com")

        self.assertIn("alpha", str(caught.exception))

    def test_an_unknown_workspace_is_an_error(self):
        with self.assertRaises(CommandError):
            call_command("add_tenant_member", "nope", "new@example.com")


class IngestDataTenantTests(TestCase):
    def test_ingesting_without_a_workspace_is_refused(self):
        with self.assertRaises(CommandError) as caught:
            call_command("ingest_data")

        self.assertIn("--tenant", str(caught.exception))
        self.assertEqual(Lead.objects.count(), 0)

    def test_ingesting_into_an_unknown_workspace_is_refused(self):
        with self.assertRaises(CommandError):
            call_command("ingest_data", tenant="nope")

    def test_every_ingested_lead_and_event_carries_the_workspace(self):
        alpha = make_tenant("alpha")

        call_command("ingest_data", tenant="alpha")

        self.assertEqual(Lead.objects.exclude(tenant=alpha).count(), 0)
        self.assertEqual(Event.objects.exclude(tenant=alpha).count(), 0)
        self.assertGreater(Lead.objects.count(), 0)
        self.assertGreater(Event.objects.count(), 0)

    def test_an_event_always_carries_its_own_leads_workspace(self):
        make_tenant("alpha")

        call_command("ingest_data", tenant="alpha")

        mismatched = [
            event.id
            for event in Event.objects.select_related("lead")
            if event.tenant_id != event.lead.tenant_id
        ]
        self.assertEqual(mismatched, [])

    def test_a_lead_id_already_owned_elsewhere_is_refused_and_rolls_back(self):
        make_tenant("alpha")
        bravo = make_tenant("bravo")
        call_command("ingest_data", tenant="alpha")
        before = Lead.objects.filter(tenant__slug="alpha").count()

        with self.assertRaises(CommandError) as caught:
            call_command("ingest_data", tenant="bravo")

        self.assertIn("alpha", str(caught.exception))
        self.assertIn("bravo", str(caught.exception))
        # Nothing moved: the command is atomic.
        self.assertEqual(Lead.objects.filter(tenant__slug="alpha").count(), before)
        self.assertEqual(Lead.objects.filter(tenant=bravo).count(), 0)

    def test_re_ingesting_the_same_workspace_is_still_idempotent(self):
        make_tenant("alpha")

        call_command("ingest_data", tenant="alpha")
        leads, events = Lead.objects.count(), Event.objects.count()
        call_command("ingest_data", tenant="alpha")

        self.assertEqual(Lead.objects.count(), leads)
        self.assertEqual(Event.objects.count(), events)


class BackfillTenantCommandTests(TestCase):
    def setUp(self):
        super().setUp()
        self.alpha = make_tenant("alpha")
        # A pre-tenancy row: nullable column, nothing assigned.
        self.orphan = make_lead(None, "lead_orphan")
        self.event = Event.objects.create(
            lead=self.orphan, tenant=None, type="login", timestamp=timezone.now()
        )

    def test_it_assigns_orphaned_leads_and_their_events(self):
        call_command("backfill_tenant", "alpha")

        self.orphan.refresh_from_db()
        self.event.refresh_from_db()
        self.assertEqual(self.orphan.tenant, self.alpha)
        self.assertEqual(self.event.tenant, self.alpha)

    def test_it_leaves_rows_that_already_have_a_workspace_alone(self):
        bravo = make_tenant("bravo")
        settled = make_lead(bravo, "lead_settled")

        call_command("backfill_tenant", "alpha")

        settled.refresh_from_db()
        self.assertEqual(settled.tenant, bravo)

    def test_running_it_twice_changes_nothing_the_second_time(self):
        call_command("backfill_tenant", "alpha")
        call_command("backfill_tenant", "alpha")

        self.assertEqual(Lead.objects.filter(tenant__isnull=True).count(), 0)
        self.assertEqual(Event.objects.filter(tenant__isnull=True).count(), 0)
        self.orphan.refresh_from_db()
        self.assertEqual(self.orphan.tenant, self.alpha)

    def test_an_unknown_workspace_is_an_error(self):
        with self.assertRaises(CommandError):
            call_command("backfill_tenant", "nope")

    def test_a_row_with_no_workspace_is_invisible_to_the_planner(self):
        with _stub():
            planned = plan_outreach(self.alpha)

        self.assertEqual(planned, [])
