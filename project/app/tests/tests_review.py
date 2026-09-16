"""The one review flow, end to end through the URLs.

``pending -> approved | dismissed``, both reversible with ``reopen``; an edit
never touches ``suggested_copy``; every illegal move is a 409 rather than a
silent no-op; and a dismissal suppresses the recommendation until the reopen
revokes it.
"""

from datetime import date
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.settings import api_settings
from rest_framework.throttling import SimpleRateThrottle

from project.app.models import DismissedOutreachKey, Lead, OutreachAction
from project.app.services import dedupe
from project.app.services.outreach import plan_outreach
from project.app.tests.tests_auth_utils import AuthenticatedAPITestCase
from project.app.views.review import ReviewListView, ReviewVerifyView

# A lead and a draft that between them exercise every grounded claim kind the
# verifier checks: contact name, deal count, quote count and dollar amount.
GROUNDED_COPY = (
    "Subject: Volume pricing ahead of your next milestone\n\n"
    "Hi Priya,\n\n"
    "You have closed 6 deals from 14 quotes submitted so far, which puts Summit Risk "
    "Advisors well on the way to the volume-pricing conversation your notes mention. "
    "On a $1,400,000 book that pace is genuinely impressive, and it is usually the "
    "point where agencies start asking what changes at the next tier. I would rather "
    "walk you through it than write it all out here, because the useful part is "
    "seeing the numbers against your own book and the way your producers actually "
    "work through submissions each week. Would you have twenty minutes this week to "
    "talk it through?\n\n"
    "Best,\nDana"
)


def make_lead(lead_id="lead_001", **overrides):
    """A power user: deals >= 5 and submissions >= 10, so the classification is
    date-independent (rule R2) and ``GROUNDED_COPY`` is fully grounded."""
    defaults = dict(
        id=lead_id,
        agency_name="Summit Risk Advisors",
        contact_name="Priya Nair",
        contact_email="priya.nair@example.com",
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
        hubspot_notes="Wants volume pricing at the 20-deal milestone.",
    )
    defaults.update(overrides)
    return Lead.objects.create(**defaults)


def make_action(lead=None, **overrides):
    lead = lead or make_lead()
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


def url(name, action):
    return reverse(name, args=[action.id])


class ReviewAPITestCase(AuthenticatedAPITestCase):
    """DRF keeps throttle history in the default cache, which outlives a test."""

    def setUp(self):
        super().setUp()
        cache.clear()

    def tearDown(self):
        cache.clear()
        super().tearDown()


class ReviewListTests(ReviewAPITestCase):
    """GET /api/outreach/ — the inbox."""

    def test_the_list_returns_the_latest_action_per_lead(self):
        lead = make_lead()
        superseded = make_action(lead, reason="last week")
        latest = make_action(lead, reason="today")

        results = self.client.get(reverse("outreach-list")).data["results"]

        self.assertEqual([row["id"] for row in results], [latest.id])
        self.assertNotIn(superseded.id, [row["id"] for row in results])

    def test_the_list_is_paginated_rather_than_serializing_the_whole_table(self):
        for index in range(3):
            make_action(make_lead(f"lead_{index:03d}"))

        resp = self.client.get(reverse("outreach-list"), {"page_size": 2})

        self.assertEqual(resp.data["count"], 3)
        self.assertEqual(len(resp.data["results"]), 2)
        self.assertIsNotNone(resp.data["next"])
        second = self.client.get(reverse("outreach-list"), {"page_size": 2, "page": 2})
        self.assertEqual(len(second.data["results"]), 1)

    def test_an_item_carries_the_copy_and_the_verification_the_reviewer_decides_on(self):
        action = make_action()

        row = self.client.get(reverse("outreach-list")).data["results"][0]

        self.assertEqual(row["id"], action.id)
        self.assertEqual(row["status"], OutreachAction.STATUS_PENDING)
        self.assertEqual(row["effective_copy"], GROUNDED_COPY)
        self.assertFalse(row["is_edited"])
        self.assertTrue(row["can_approve"])
        self.assertEqual(row["can_approve"], row["verification"]["can_approve"])
        self.assertEqual(row["verification"]["copy"], GROUNDED_COPY)
        self.assertEqual(row["lead"]["id"], action.lead_id)

    def test_the_list_declares_a_throttle_scope_with_a_configured_rate(self):
        self.assertEqual(ReviewListView.throttle_scope, "outreach_list")
        self.assertIn("outreach_list", api_settings.DEFAULT_THROTTLE_RATES)

    def test_the_list_throttle_returns_the_contract_envelope(self):
        make_action()
        with patch.dict(SimpleRateThrottle.THROTTLE_RATES, {"outreach_list": "1/min"}):
            self.assertEqual(
                self.client.get(reverse("outreach-list")).status_code, status.HTTP_200_OK
            )
            resp = self.client.get(reverse("outreach-list"))
        self.assertEqual(resp.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(resp.data["code"], "rate_limited")


class ReviewEditTests(ReviewAPITestCase):
    """POST /api/outreach/{id}/edit/."""

    def setUp(self):
        super().setUp()
        self.action = make_action()

    def _edit(self, body):
        return self.client.post(url("outreach-edit", self.action), body, format="json")

    def test_an_edit_lands_in_edited_copy_and_leaves_suggested_copy_untouched(self):
        edited = GROUNDED_COPY.replace("twenty minutes", "fifteen minutes")

        resp = self._edit({"copy": edited})

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["edited_copy"], edited)
        self.assertEqual(resp.data["effective_copy"], edited)
        self.assertTrue(resp.data["is_edited"])
        self.action.refresh_from_db()
        # The immutability invariant: the corpus diffs the model's draft
        # against what a human actually sent.
        self.assertEqual(self.action.suggested_copy, GROUNDED_COPY)
        self.assertEqual(self.action.edited_copy, edited)

    def test_an_edit_rewrites_the_stored_verification_for_the_new_copy(self):
        wrong = GROUNDED_COPY.replace("closed 6 deals", "closed 9 deals")

        resp = self._edit({"copy": wrong})

        self.assertEqual(resp.data["verification"]["copy"], wrong)
        self.assertEqual(resp.data["verification"]["unverified_count"], 1)
        self.assertFalse(resp.data["can_approve"])
        self.action.refresh_from_db()
        self.assertEqual(self.action.verification["copy"], wrong)

    def test_a_null_copy_reverts_to_the_original_draft(self):
        self._edit({"copy": GROUNDED_COPY.replace("closed 6 deals", "closed 9 deals")})

        resp = self._edit({"copy": None})

        self.assertEqual(resp.data["edited_copy"], "")
        self.assertEqual(resp.data["effective_copy"], GROUNDED_COPY)
        self.assertFalse(resp.data["is_edited"])
        self.assertTrue(resp.data["can_approve"])

    def test_copy_is_normalized_before_it_is_stored(self):
        resp = self._edit({"copy": "Subject: Hi\r\n\r\nHello there.\r\n"})
        self.assertEqual(resp.data["edited_copy"], "Subject: Hi\n\nHello there.\n")

    def test_a_missing_copy_key_is_a_validation_error(self):
        resp = self._edit({})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data["code"], "validation_error")

    def test_a_non_string_copy_is_a_validation_error(self):
        resp = self._edit({"copy": 12})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data["code"], "validation_error")

    def test_blank_copy_is_refused_rather_than_stored(self):
        resp = self._edit({"copy": "   \n  "})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data["code"], "empty_copy")
        self.action.refresh_from_db()
        self.assertEqual(self.action.edited_copy, "")

    def test_a_decided_item_cannot_be_edited(self):
        for verb in ("outreach-approve", "outreach-dismiss"):
            with self.subTest(verb):
                action = make_action(make_lead(f"lead_{verb}"))
                self.client.post(url(verb, action), {}, format="json")
                resp = self.client.post(
                    url("outreach-edit", action), {"copy": "Subject: No\n\nNope.\n"}, format="json"
                )
                self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
                self.assertEqual(resp.data["code"], "invalid_transition")

    def test_an_unknown_id_is_a_404_in_the_contract_shape(self):
        resp = self.client.post(
            reverse("outreach-edit", args=[999_999]), {"copy": "x"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(resp.data["code"], "not_found")


class ReviewVerifyTests(ReviewAPITestCase):
    """POST /api/outreach/{id}/verify/ — the live grounding check."""

    def setUp(self):
        super().setUp()
        self.action = make_action()

    def test_verify_reports_on_candidate_copy_without_storing_it(self):
        candidate = GROUNDED_COPY.replace("closed 6 deals", "closed 40 deals")

        resp = self.client.post(
            url("outreach-verify", self.action), {"copy": candidate}, format="json"
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["copy"], candidate)
        self.assertEqual(resp.data["unverified_count"], 1)
        self.assertFalse(resp.data["can_approve"])
        # A dry run: nothing about the row changed.
        self.action.refresh_from_db()
        self.assertEqual(self.action.edited_copy, "")
        self.assertEqual(self.action.verification, {})

    def test_verify_confirms_grounded_copy(self):
        resp = self.client.post(
            url("outreach-verify", self.action), {"copy": GROUNDED_COPY}, format="json"
        )
        self.assertTrue(resp.data["can_approve"])
        self.assertEqual(resp.data["unverified_count"], 0)

    def test_blank_copy_is_refused(self):
        resp = self.client.post(url("outreach-verify", self.action), {"copy": ""}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data["code"], "empty_copy")

    def test_verify_declares_a_throttle_scope_with_a_configured_rate(self):
        # Key repeat in the inline editor must not hammer the verifier.
        self.assertEqual(ReviewVerifyView.throttle_scope, "copy_verify")
        self.assertIn("copy_verify", api_settings.DEFAULT_THROTTLE_RATES)


class ReviewApproveTests(ReviewAPITestCase):
    """POST /api/outreach/{id}/approve/."""

    def setUp(self):
        super().setUp()
        self.action = make_action()

    def test_approving_a_pending_item_stamps_the_decision(self):
        resp = self.client.post(url("outreach-approve", self.action), {}, format="json")

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["status"], OutreachAction.STATUS_APPROVED)
        self.action.refresh_from_db()
        self.assertEqual(self.action.status, OutreachAction.STATUS_APPROVED)
        self.assertIsNotNone(self.action.status_changed_at)
        # The report that justified the approval is stored with it.
        self.assertEqual(self.action.verification["copy"], GROUNDED_COPY)
        self.assertTrue(self.action.verification["can_approve"])

    def test_approval_is_blocked_while_a_claim_is_unverified(self):
        self.client.post(
            url("outreach-edit", self.action),
            {"copy": GROUNDED_COPY.replace("closed 6 deals", "closed 9 deals")},
            format="json",
        )

        resp = self.client.post(url("outreach-approve", self.action), {}, format="json")

        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(resp.data["code"], "unverified_claims")
        self.action.refresh_from_db()
        self.assertEqual(self.action.status, OutreachAction.STATUS_PENDING)

    def test_approval_fails_closed_when_no_report_can_be_produced(self):
        # "We could not check this copy" blocks approval rather than waving it
        # through; the gate reads one report and never infers from its absence.
        with patch("project.app.services.queue_copy.build_verification", return_value={}) as build:
            resp = self.client.post(url("outreach-approve", self.action), {}, format="json")

        build.assert_called()
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(resp.data["code"], "unverified_claims")
        self.action.refresh_from_db()
        self.assertEqual(self.action.status, OutreachAction.STATUS_PENDING)

    def test_approving_twice_is_a_409(self):
        self.client.post(url("outreach-approve", self.action), {}, format="json")

        resp = self.client.post(url("outreach-approve", self.action), {}, format="json")

        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(resp.data["code"], "invalid_transition")

    def test_a_dismissed_item_cannot_be_approved_without_reopening_it(self):
        self.client.post(url("outreach-dismiss", self.action), {}, format="json")

        resp = self.client.post(url("outreach-approve", self.action), {}, format="json")

        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(resp.data["code"], "invalid_transition")


class ReviewDismissTests(ReviewAPITestCase):
    """POST /api/outreach/{id}/dismiss/."""

    def setUp(self):
        super().setUp()
        self.action = make_action()

    def test_dismissing_writes_the_suppression_row_with_its_reason(self):
        resp = self.client.post(
            url("outreach-dismiss", self.action), {"reason": "bad_timing"}, format="json"
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["status"], OutreachAction.STATUS_DISMISSED)
        self.action.refresh_from_db()
        self.assertIsNotNone(self.action.status_changed_at)
        key = DismissedOutreachKey.objects.get(dedupe_key=self.action.dedupe_key)
        self.assertEqual(key.reason, "bad_timing")
        self.assertEqual(key.action_type, self.action.action_type)
        self.assertEqual(key.source_action_id, self.action.id)
        self.assertEqual(key.dismissed_by, self.TEST_EMAIL)
        self.assertIsNone(key.revoked_at)

    def test_a_dismissal_without_a_reason_is_allowed(self):
        resp = self.client.post(url("outreach-dismiss", self.action), {}, format="json")

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            DismissedOutreachKey.objects.get(dedupe_key=self.action.dedupe_key).reason, ""
        )

    def test_an_unrecognized_reason_is_refused_and_nothing_is_written(self):
        resp = self.client.post(
            url("outreach-dismiss", self.action), {"reason": "because"}, format="json"
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data["code"], "invalid_reason")
        self.action.refresh_from_db()
        self.assertEqual(self.action.status, OutreachAction.STATUS_PENDING)
        self.assertEqual(DismissedOutreachKey.objects.count(), 0)

    def test_an_approved_item_cannot_be_dismissed_without_reopening_it(self):
        self.client.post(url("outreach-approve", self.action), {}, format="json")

        resp = self.client.post(url("outreach-dismiss", self.action), {}, format="json")

        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(resp.data["code"], "invalid_transition")


class ReviewReopenTests(ReviewAPITestCase):
    """POST /api/outreach/{id}/reopen/ — the way back from either decision."""

    def setUp(self):
        super().setUp()
        self.action = make_action()

    def test_reopening_an_approval_returns_it_to_pending(self):
        self.client.post(url("outreach-approve", self.action), {}, format="json")

        resp = self.client.post(url("outreach-reopen", self.action), {}, format="json")

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["status"], OutreachAction.STATUS_PENDING)
        self.action.refresh_from_db()
        self.assertEqual(self.action.status, OutreachAction.STATUS_PENDING)
        self.assertIsNotNone(self.action.status_changed_at)

    def test_reopening_a_dismissal_revokes_its_suppression_row(self):
        self.client.post(
            url("outreach-dismiss", self.action), {"reason": "bad_timing"}, format="json"
        )

        self.client.post(url("outreach-reopen", self.action), {}, format="json")

        key = DismissedOutreachKey.objects.get(dedupe_key=self.action.dedupe_key)
        # Revoked, not deleted: the dismissal stays on the record.
        self.assertIsNotNone(key.revoked_at)
        self.assertEqual(key.reason, "bad_timing")

    def test_a_pending_item_has_nothing_to_reopen(self):
        resp = self.client.post(url("outreach-reopen", self.action), {}, format="json")

        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(resp.data["code"], "invalid_transition")

    def test_re_dismissing_after_a_reopen_suppresses_again(self):
        self.client.post(url("outreach-dismiss", self.action), {}, format="json")
        self.client.post(url("outreach-reopen", self.action), {}, format="json")

        self.client.post(url("outreach-dismiss", self.action), {}, format="json")

        key = DismissedOutreachKey.objects.get(dedupe_key=self.action.dedupe_key)
        self.assertIsNone(key.revoked_at)


class SuppressionAndThePlannerTests(ReviewAPITestCase):
    """What a dismissal, and a reopen of one, do to the next planner run."""

    def setUp(self):
        super().setUp()
        self.lead = make_lead()
        with patch("project.app.services.outreach.agenerate_copy", return_value=GROUNDED_COPY):
            plan_outreach()
        self.action = OutreachAction.objects.get()

    def _run_planner(self):
        with patch("project.app.services.outreach.agenerate_copy", return_value=GROUNDED_COPY):
            return plan_outreach()

    def test_a_dismissed_recommendation_is_not_offered_again(self):
        self.client.post(url("outreach-dismiss", self.action), {"reason": "not_a_fit"})

        self.assertEqual(self._run_planner(), [])
        self.assertEqual(OutreachAction.objects.count(), 1)

    def test_reopening_a_dismissal_lets_the_planner_offer_the_lead_again(self):
        self.client.post(url("outreach-dismiss", self.action), {"reason": "not_a_fit"})
        self.client.post(url("outreach-reopen", self.action), {})

        # Still suppressed by the open-item rule while the reopened row is
        # pending; deciding it is what frees the lead.
        self.client.post(url("outreach-dismiss", self.action), {"reason": "not_a_fit"})
        self.client.post(url("outreach-reopen", self.action), {})
        self.client.post(url("outreach-approve", self.action), {})

        planned = self._run_planner()

        self.assertEqual([action.lead_id for action in planned], [self.lead.id])

    def test_an_open_pending_item_still_blocks_a_second_recommendation(self):
        self.assertEqual(self._run_planner(), [])


class TransitionTableTests(TestCase):
    """The state machine itself, independent of the endpoints."""

    def test_the_legal_moves_are_pending_to_a_decision_and_back(self):
        self.assertEqual(
            OutreachAction.ALLOWED_TRANSITIONS,
            {
                OutreachAction.STATUS_PENDING: (
                    OutreachAction.STATUS_APPROVED,
                    OutreachAction.STATUS_DISMISSED,
                ),
                OutreachAction.STATUS_APPROVED: (OutreachAction.STATUS_PENDING,),
                OutreachAction.STATUS_DISMISSED: (OutreachAction.STATUS_PENDING,),
            },
        )

    def test_only_a_pending_item_is_editable(self):
        self.assertEqual(OutreachAction.EDITABLE_STATUSES, (OutreachAction.STATUS_PENDING,))

    def test_a_new_action_starts_pending_undecided_and_unedited(self):
        action = make_action()
        self.assertEqual(action.status, OutreachAction.STATUS_PENDING)
        self.assertIsNone(action.status_changed_at)
        self.assertEqual(action.edited_copy, "")  # "" not None
        self.assertEqual(action.verification, {})
        self.assertEqual(action.effective_copy, action.suggested_copy)
