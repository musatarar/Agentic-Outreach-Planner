"""Tests for the triage queue state model and API (MUS-39).

Kept in its own module: MUS-39 never edits ``tests_api.py`` (CONTRACT MUS-35
section 8.2), so the two suites can be reviewed and merged independently.
"""

from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from project.app.models import (
    DismissedOutreachKey,
    Event,
    Lead,
    OutreachAction,
    OutreachEdit,
)
from project.app.services import dedupe, queue_copy
from project.app.services.outreach import plan_outreach

try:  # pragma: no cover - the fallback disappears the moment MUS-37 merges
    from project.app.tests_auth_utils import AuthenticatedAPITestCase
except ImportError:
    # MUS-37 owns tests_auth_utils.py; its name and API are frozen in CONTRACT
    # section 8.2 precisely so MUS-39 can write against it before it lands.
    # This local twin keeps the branch green standalone and is deleted by the
    # import above the moment MUS-37 merges.
    class AuthenticatedAPITestCase(TestCase):
        """TestCase whose ``self.client`` is already signed in."""

        TEST_EMAIL = "tester@example.com"

        def setUp(self):
            super().setUp()
            self.user = get_user_model().objects.create(
                username=self.TEST_EMAIL, email=self.TEST_EMAIL
            )
            self.user.set_unusable_password()
            self.user.save()
            self.client.force_login(self.user)


# A well-shaped, grounded draft: passes both the shape gate (MUS-23) and the
# grounding gate (MUS-22) so plan_outreach() leaves needs_human False.
GOOD_COPY = (
    "Subject: A quick idea for your team\n\n"
    "Hi there,\n\n"
    "You have been steadily working through quotes in the portal, and I wanted "
    "to share one small change that usually helps agencies of your size get "
    "more of them over the line. It takes about fifteen minutes to walk "
    "through, and your producers can start using it the same day. I would "
    "rather show you than write it all out here, since the useful part is "
    "seeing it against your own book of business and your own workflow. Would "
    "you have time for a short call this week?\n\n"
    "Best,\nDana"
)


def make_lead(lead_id="lead_001", **overrides):
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


def make_action(lead=None, **overrides):
    lead = lead or make_lead()
    defaults = dict(
        lead=lead,
        priority=2,
        action_type="nudge_usage",
        reason="Underusing the portal.",
        suggested_copy="Subject: Hello\n\nHi there,\n\nA nudge.\n\nBest,\nDana",
    )
    defaults.update(overrides)
    return OutreachAction.objects.create(**defaults)


class OutreachActionStateModelTests(TestCase):
    """The lifecycle fields and the transition table (CONTRACT section 2.3)."""

    def test_new_action_defaults_to_pending_and_unedited(self):
        action = make_action()
        self.assertEqual(action.status, OutreachAction.STATUS_PENDING)
        self.assertIsNone(action.status_changed_at)
        # "" not None -- CONTRACT section 9.11.
        self.assertEqual(action.edited_copy, "")
        self.assertEqual(action.snooze_trigger, "")
        self.assertIsNone(action.snooze_until)
        self.assertIsNone(action.snooze_activity_after)
        self.assertEqual(action.dismiss_reason, "")
        self.assertEqual(action.dedupe_key, "")
        self.assertEqual(action.rule_trace, {})
        self.assertEqual(action.verification, {})

    def test_effective_copy_prefers_the_edit(self):
        action = make_action()
        self.assertEqual(action.effective_copy, action.suggested_copy)
        action.edited_copy = "Subject: Edited\n\nHi.\n"
        self.assertEqual(action.effective_copy, "Subject: Edited\n\nHi.\n")
        # suggested_copy is untouched -- the eval corpus depends on it.
        self.assertNotEqual(action.suggested_copy, action.edited_copy)

    def test_pending_may_be_approved_snoozed_or_dismissed(self):
        action = make_action()
        for target in (
            OutreachAction.STATUS_APPROVED,
            OutreachAction.STATUS_SNOOZED,
            OutreachAction.STATUS_DISMISSED,
        ):
            self.assertTrue(action.can_transition_to(target))
        self.assertFalse(action.can_transition_to(OutreachAction.STATUS_PENDING))

    def test_approved_may_only_go_back_to_pending(self):
        action = make_action(status=OutreachAction.STATUS_APPROVED)
        self.assertTrue(action.can_transition_to(OutreachAction.STATUS_PENDING))
        self.assertFalse(action.can_transition_to(OutreachAction.STATUS_DISMISSED))
        self.assertFalse(action.can_transition_to(OutreachAction.STATUS_SNOOZED))

    def test_dismissed_may_only_go_back_to_pending(self):
        action = make_action(status=OutreachAction.STATUS_DISMISSED)
        self.assertTrue(action.can_transition_to(OutreachAction.STATUS_PENDING))
        # Approving a dismissed action is an error, not a silent no-op.
        self.assertFalse(action.can_transition_to(OutreachAction.STATUS_APPROVED))

    def test_snoozed_may_be_re_snoozed(self):
        action = make_action(status=OutreachAction.STATUS_SNOOZED)
        self.assertTrue(action.can_transition_to(OutreachAction.STATUS_SNOOZED))
        self.assertTrue(action.can_transition_to(OutreachAction.STATUS_PENDING))
        self.assertTrue(action.can_transition_to(OutreachAction.STATUS_APPROVED))

    def test_only_pending_and_snoozed_are_editable(self):
        self.assertIn(OutreachAction.STATUS_PENDING, OutreachAction.EDITABLE_STATUSES)
        self.assertIn(OutreachAction.STATUS_SNOOZED, OutreachAction.EDITABLE_STATUSES)
        self.assertNotIn(OutreachAction.STATUS_APPROVED, OutreachAction.EDITABLE_STATUSES)
        self.assertNotIn(OutreachAction.STATUS_DISMISSED, OutreachAction.EDITABLE_STATUSES)

    def test_unknown_status_has_no_legal_transitions(self):
        action = make_action(status="bogus")
        self.assertFalse(action.can_transition_to(OutreachAction.STATUS_PENDING))


class DismissedOutreachKeyTests(TestCase):
    def test_dedupe_key_is_unique(self):
        lead = make_lead()
        DismissedOutreachKey.objects.create(dedupe_key="abc", lead=lead, action_type="nudge_usage")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                DismissedOutreachKey.objects.create(
                    dedupe_key="abc", lead=lead, action_type="nudge_usage"
                )

    def test_outlives_the_action_that_created_it(self):
        action = make_action()
        key = DismissedOutreachKey.objects.create(
            dedupe_key="abc",
            lead=action.lead,
            action_type=action.action_type,
            source_action=action,
        )
        action.delete()
        key.refresh_from_db()
        # SET_NULL: pruning old actions must not resurrect dismissed work.
        self.assertIsNone(key.source_action_id)
        self.assertEqual(str(key), f"dismissed {key.lead_id}/nudge_usage")

    def test_revoked_at_defaults_to_none(self):
        lead = make_lead()
        key = DismissedOutreachKey.objects.create(
            dedupe_key="abc", lead=lead, action_type="nudge_usage"
        )
        self.assertIsNone(key.revoked_at)


class OutreachEditTests(TestCase):
    def test_edits_are_ordered_oldest_first(self):
        action = make_action()
        first = OutreachEdit.objects.create(outreach_action=action, before_text="a", after_text="b")
        second = OutreachEdit.objects.create(
            outreach_action=action, before_text="b", after_text="c"
        )
        self.assertEqual(list(action.edits.all()), [first, second])
        self.assertFalse(first.committed)
        self.assertEqual(first.diff_ops, [])
        self.assertIn("edit of action", str(first))
        self.assertEqual(second.similarity, 1.0)

    def test_edits_are_deleted_with_the_action(self):
        action = make_action()
        OutreachEdit.objects.create(outreach_action=action, before_text="a", after_text="b")
        action.delete()
        self.assertEqual(OutreachEdit.objects.count(), 0)


class DedupeKeyTests(TestCase):
    """CONTRACT section 2.6 -- the documented dedupe key definition."""

    def test_key_is_stable_for_the_same_lead_and_action_type(self):
        self.assertEqual(
            dedupe.dedupe_key("lead_007", "nudge_usage"),
            dedupe.dedupe_key("lead_007", "nudge_usage"),
        )

    def test_key_is_scoped_to_action_type_not_just_the_lead(self):
        # Dismissing `nudge_usage` for a lead must not suppress a later
        # `reengage_dormant` for the same lead -- that is a different situation.
        self.assertNotEqual(
            dedupe.dedupe_key("lead_007", "nudge_usage"),
            dedupe.dedupe_key("lead_007", "reengage_dormant"),
        )

    def test_key_is_scoped_to_the_lead(self):
        self.assertNotEqual(
            dedupe.dedupe_key("lead_007", "nudge_usage"),
            dedupe.dedupe_key("lead_008", "nudge_usage"),
        )

    def test_key_ignores_the_reason_text(self):
        # The key takes no reason argument at all: a re-run that computes a
        # marginally different reason string cannot resurrect a dismissal.
        key = dedupe.dedupe_key("lead_007", "nudge_usage")
        self.assertEqual(len(key), 64)  # sha256 hexdigest
        self.assertTrue(key.startswith(key[:8]))

    def test_version_prefix_is_part_of_the_hash(self):
        with patch.object(dedupe, "DEDUPE_VERSION", "v2"):
            bumped = dedupe.dedupe_key("lead_007", "nudge_usage")
        self.assertNotEqual(bumped, dedupe.dedupe_key("lead_007", "nudge_usage"))


class NormalizeCopyTests(TestCase):
    """CONTRACT section 9.1c -- line endings are normalized before storage."""

    def test_crlf_and_cr_become_lf(self):
        self.assertEqual(queue_copy.normalize_copy("a\r\nb\rc\nd"), "a\nb\nc\nd")

    def test_none_and_empty_become_empty_string(self):
        self.assertEqual(queue_copy.normalize_copy(None), "")
        self.assertEqual(queue_copy.normalize_copy(""), "")

    def test_offsets_shift_when_crlf_is_not_normalized(self):
        # The bug this guards: two characters per line break in Python, one in
        # the rendered text, so every span after the first newline is off.
        raw = "Subject: Hi\r\n\r\n6 deals"
        self.assertEqual(raw.index("6 deals"), 15)
        self.assertEqual(queue_copy.normalize_copy(raw).index("6 deals"), 13)

    def test_astral_characters_are_flagged(self):
        self.assertTrue(queue_copy.is_astral_safe("plain text"))
        self.assertFalse(queue_copy.is_astral_safe("congrats \U0001f389 6 deals"))


class BuildVerificationTests(TestCase):
    def test_report_describes_the_normalized_copy(self):
        lead = make_lead()
        report = queue_copy.build_verification(lead, "Subject: Hi\r\n\r\nHello.", "nudge_usage")
        self.assertNotIn("\r", report["copy"])
        self.assertEqual(report["copy_length"], len(report["copy"]))
        self.assertEqual(report["version"], 1)
        self.assertIn("claims verified", report["summary"])

    def test_can_approve_defaults_open_and_honours_the_report(self):
        self.assertTrue(queue_copy.can_approve(None))
        self.assertTrue(queue_copy.can_approve({}))
        self.assertFalse(queue_copy.can_approve({"can_approve": False}))


class DiffEditTests(TestCase):
    def test_records_non_equal_opcodes_only(self):
        result = queue_copy.diff_edit("volume pricing", "volume pricing tiers")
        self.assertEqual(len(result["diff_ops"]), 1)
        op = result["diff_ops"][0]
        self.assertEqual(op["op"], "insert")
        self.assertEqual(op["after"], " tiers")
        self.assertEqual(result["chars_added"], 6)
        self.assertEqual(result["chars_removed"], 0)
        self.assertLess(result["similarity"], 1.0)

    def test_identical_text_produces_no_ops(self):
        result = queue_copy.diff_edit("same", "same")
        self.assertEqual(result["diff_ops"], [])
        self.assertEqual(result["similarity"], 1.0)

    def test_deletion_counts_removed_characters(self):
        result = queue_copy.diff_edit("hello world", "hello")
        self.assertEqual(result["chars_removed"], 6)
        self.assertEqual(result["chars_added"], 0)


class PlanOutreachDedupeTests(TestCase):
    """CONTRACT sections 2.6, 9.8 and 9.9 -- what a re-run may and may not do."""

    def _lead(self, lead_id="lead_001"):
        # demo_completed + no signup -> complete_onboarding, which is
        # date-independent, so classification never drifts with the wall clock.
        return make_lead(lead_id, stage="demo_completed", signed_up_date=None)

    def _plan(self, copy=GOOD_COPY):
        with patch("project.app.services.outreach.generate_copy", return_value=copy):
            return plan_outreach()

    def test_persists_the_dedupe_key_and_snapshots(self):
        self._lead()
        self._plan()
        action = OutreachAction.objects.get()
        self.assertEqual(action.dedupe_key, dedupe.dedupe_key(action.lead_id, action.action_type))
        # rule_trace is a snapshot; MUS-42 fills it, until then it is {}.
        self.assertIsInstance(action.rule_trace, dict)
        # verification always describes the copy in play.
        self.assertEqual(action.verification["copy"], action.effective_copy)

    def test_a_second_run_does_not_double_the_inbox(self):
        self._lead()
        self._plan()
        self.assertEqual(OutreachAction.objects.count(), 1)
        second = self._plan()
        self.assertEqual(second, [])
        self.assertEqual(OutreachAction.objects.count(), 1)

    def test_a_second_run_reopens_nothing_for_an_approved_item(self):
        # An approved item is no longer open, so the recommendation is eligible
        # to be planned again -- that is the intended behaviour, unlike dismiss.
        self._lead()
        self._plan()
        OutreachAction.objects.update(status=OutreachAction.STATUS_APPROVED)
        self._plan()
        self.assertEqual(OutreachAction.objects.count(), 2)

    def test_a_snoozed_item_still_counts_as_open(self):
        self._lead()
        self._plan()
        OutreachAction.objects.update(status=OutreachAction.STATUS_SNOOZED)
        self._plan()
        self.assertEqual(OutreachAction.objects.count(), 1)

    def test_dismissed_recommendations_are_never_recreated(self):
        lead = self._lead()
        self._plan()
        action = OutreachAction.objects.get()
        action.status = OutreachAction.STATUS_DISMISSED
        action.save(update_fields=["status"])
        DismissedOutreachKey.objects.create(
            dedupe_key=action.dedupe_key,
            lead=lead,
            action_type=action.action_type,
            source_action=action,
        )

        self._plan()

        self.assertEqual(OutreachAction.objects.count(), 1)
        self.assertEqual(OutreachAction.objects.get().status, OutreachAction.STATUS_DISMISSED)

    def test_suppression_costs_no_copy_generation(self):
        lead = self._lead()
        self._plan()
        action = OutreachAction.objects.get()
        action.status = OutreachAction.STATUS_DISMISSED
        action.save(update_fields=["status"])
        DismissedOutreachKey.objects.create(
            dedupe_key=action.dedupe_key, lead=lead, action_type=action.action_type
        )

        with patch(
            "project.app.services.outreach.generate_copy", return_value=GOOD_COPY
        ) as generate:
            plan_outreach()
        generate.assert_not_called()

    def test_a_revoked_dismissal_no_longer_suppresses(self):
        lead = self._lead()
        self._plan()
        action = OutreachAction.objects.get()
        action.status = OutreachAction.STATUS_DISMISSED
        action.save(update_fields=["status"])
        DismissedOutreachKey.objects.create(
            dedupe_key=action.dedupe_key,
            lead=lead,
            action_type=action.action_type,
            revoked_at=timezone.now(),
        )

        self._plan()

        self.assertEqual(OutreachAction.objects.filter(status="pending").count(), 1)

    def test_dismissing_one_action_type_does_not_suppress_another(self):
        # Killing `nudge_usage` for this lead must not suppress the
        # `complete_onboarding` the planner actually wants to raise.
        lead = self._lead()
        DismissedOutreachKey.objects.create(
            dedupe_key=dedupe.dedupe_key(lead.id, "nudge_usage"),
            lead=lead,
            action_type="nudge_usage",
        )
        self._plan()
        action = OutreachAction.objects.get()
        self.assertEqual(action.action_type, "complete_onboarding")

    def test_stored_copy_is_line_ending_normalized(self):
        self._lead()
        self._plan(copy=GOOD_COPY.replace("\n", "\r\n"))
        action = OutreachAction.objects.get()
        self.assertNotIn("\r", action.suggested_copy)
        self.assertEqual(action.suggested_copy, GOOD_COPY)


def make_event(lead, **overrides):
    defaults = dict(lead=lead, type="login", timestamp=timezone.now(), meta={})
    defaults.update(overrides)
    return Event.objects.create(**defaults)


class QueueListViewTests(AuthenticatedAPITestCase):
    """GET /api/queue/ -- CONTRACT section 5.2."""

    def test_requires_authentication(self):
        self.client.logout()
        resp = self.client.get(reverse("queue-list"))
        # 401 once MUS-37's SessionAuthenticationWith401 is the default; 403
        # from stock DRF SessionAuthentication until then. Never 200.
        self.assertIn(resp.status_code, (401, 403))

    def test_returns_only_pending_items(self):
        lead = make_lead()
        pending = make_action(lead)
        for status_value in ("approved", "snoozed", "dismissed"):
            make_action(
                make_lead(f"lead_{status_value}"),
                status=status_value,
                status_changed_at=timezone.now(),
            )

        resp = self.client.get(reverse("queue-list"))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual([item["id"] for item in resp.data["items"]], [pending.id])

    def test_orders_by_priority_then_lead_id(self):
        make_action(make_lead("lead_003"), priority=1)
        make_action(make_lead("lead_001"), priority=2)
        make_action(make_lead("lead_002"), priority=1)

        resp = self.client.get(reverse("queue-list"))

        self.assertEqual(
            [item["lead"]["id"] for item in resp.data["items"]],
            ["lead_002", "lead_003", "lead_001"],
        )

    def test_items_are_complete(self):
        """Section 9.14 -- advancing a row must need zero network requests."""
        lead = make_lead()
        make_event(lead, type="quote_submitted")
        action = make_action(lead)
        action.rule_trace = {"version": 1, "today": "2026-06-12"}
        action.verification = queue_copy.build_verification(
            lead, action.suggested_copy, action.action_type
        )
        action.save()

        item = self.client.get(reverse("queue-list")).data["items"][0]

        self.assertEqual(item["rule_trace"]["version"], 1)
        self.assertEqual(item["verification"]["copy"], item["effective_copy"])
        self.assertTrue(item["effective_copy"])
        self.assertEqual(item["action_label"], "Nudge usage / encourage next step")
        self.assertEqual(item["lead"]["recent_events"][0]["summary"], "Quote submitted")
        self.assertFalse(item["is_edited"])
        self.assertEqual(item["edited_copy"], "")  # "" never null -- section 9.11
        self.assertTrue(item["can_approve"])
        self.assertEqual(item["undo"], {"available": False, "expires_at": None})
        self.assertEqual(item["snooze"], {"until": None, "trigger": "", "activity_after": None})

    def test_verification_always_describes_effective_copy(self):
        """Section 9.2 -- spans computed against one string, rendered over another."""
        action = make_action()
        action.edited_copy = "Subject: Different\n\nHi.\n"
        action.save()

        item = self.client.get(reverse("queue-list")).data["items"][0]

        self.assertEqual(item["effective_copy"], action.edited_copy)
        self.assertEqual(item["verification"]["copy"], action.edited_copy)
        self.assertTrue(item["is_edited"])

    def test_recent_events_are_newest_first_and_capped_at_five(self):
        lead = make_lead()
        base = timezone.now()
        for offset in range(7):
            make_event(lead, timestamp=base - timedelta(hours=offset))
        make_action(lead)

        events = self.client.get(reverse("queue-list")).data["items"][0]["lead"]["recent_events"]

        self.assertEqual(len(events), 5)
        self.assertEqual(events, sorted(events, key=lambda e: e["timestamp"], reverse=True))

    def test_counts_drive_the_progress_header(self):
        make_action(make_lead("lead_001"))
        make_action(make_lead("lead_002"))
        make_action(
            make_lead("lead_003"),
            status=OutreachAction.STATUS_APPROVED,
            status_changed_at=timezone.now(),
        )
        make_action(
            make_lead("lead_004"),
            status=OutreachAction.STATUS_DISMISSED,
            status_changed_at=timezone.now(),
        )

        counts = self.client.get(reverse("queue-list")).data["counts"]

        self.assertEqual(counts["remaining"], 2)
        self.assertEqual(counts["approved_today"], 1)
        self.assertEqual(counts["dismissed_today"], 1)
        self.assertEqual(counts["snoozed_today"], 0)
        self.assertEqual(counts["done_today"], 2)
        self.assertEqual(counts["total_today"], 4)

    def test_yesterdays_decisions_do_not_count_toward_today(self):
        make_action(
            make_lead("lead_old"),
            status=OutreachAction.STATUS_APPROVED,
            status_changed_at=timezone.now() - timedelta(days=2),
        )

        counts = self.client.get(reverse("queue-list")).data["counts"]

        self.assertEqual(counts["approved_today"], 0)
        self.assertEqual(counts["total_today"], 0)

    def _make_actions(self, count):
        start = OutreachAction.objects.count()
        for index in range(start, start + count):
            lead = make_lead(f"lead_{index:04d}")
            make_event(lead)
            make_event(lead, type="quote_created")
            make_action(lead)

    def test_queue_query_count_is_constant(self):
        self._make_actions(3)
        with CaptureQueriesContext(connection) as small:
            self.client.get("/api/queue/")
        self._make_actions(297)
        with CaptureQueriesContext(connection) as large:
            self.client.get("/api/queue/")
        self.assertEqual(len(small), len(large))
        self.assertLessEqual(len(large), 8)  # absolute ceiling


@override_settings(TRIAGE_TIMEZONE="America/Denver")
class QueueTodayTests(AuthenticatedAPITestCase):
    """Section 9.5 -- the server decides the day boundary, not the browser."""

    def test_date_is_computed_in_the_triage_timezone(self):
        frozen = datetime(2026, 7, 29, 4, 0, tzinfo=dt_timezone.utc)  # 22:00 on the 28th
        make_action(
            make_lead(),
            status=OutreachAction.STATUS_APPROVED,
            status_changed_at=datetime(2026, 7, 29, 2, 0, tzinfo=dt_timezone.utc),
        )

        with patch("django.utils.timezone.now", return_value=frozen):
            resp = self.client.get(reverse("queue-list"))

        self.assertEqual(resp.data["date"], "2026-07-28")
        self.assertEqual(resp.data["timezone"], "America/Denver")
        # 02:00 UTC on the 29th is 20:00 local on the 28th -- same working day.
        self.assertEqual(resp.data["counts"]["approved_today"], 1)


class QueueDetailViewTests(AuthenticatedAPITestCase):
    def test_returns_one_item(self):
        action = make_action()
        resp = self.client.get(reverse("queue-detail", args=[action.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["id"], action.id)

    def test_unknown_id_is_a_contract_shaped_404(self):
        resp = self.client.get(reverse("queue-detail", args=[9999]))
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data["code"], "not_found")
        self.assertTrue(resp.data["detail"])

    def test_returns_items_in_any_status(self):
        action = make_action(status=OutreachAction.STATUS_DISMISSED)
        resp = self.client.get(reverse("queue-detail", args=[action.id]))
        self.assertEqual(resp.data["status"], "dismissed")


class QueueDoneViewTests(AuthenticatedAPITestCase):
    def _decided(self, lead_id, status_value, minutes_ago, **overrides):
        return make_action(
            make_lead(lead_id),
            status=status_value,
            status_changed_at=timezone.now() - timedelta(minutes=minutes_ago),
            **overrides,
        )

    def test_lists_todays_decisions_newest_first(self):
        first = self._decided("lead_001", OutreachAction.STATUS_APPROVED, 30)
        last = self._decided("lead_002", OutreachAction.STATUS_DISMISSED, 5)
        make_action(make_lead("lead_003"))  # still pending -> not in /done

        resp = self.client.get(reverse("queue-done"))

        self.assertEqual([item["id"] for item in resp.data["items"]], [last.id, first.id])
        self.assertEqual(resp.data["summary"]["total"], 2)
        self.assertEqual(resp.data["summary"]["approved"], 1)
        self.assertEqual(resp.data["summary"]["dismissed"], 1)

    def test_elapsed_and_pipeline_value(self):
        self._decided("lead_001", OutreachAction.STATUS_APPROVED, 20)
        self._decided("lead_002", OutreachAction.STATUS_APPROVED, 5)
        self._decided("lead_003", OutreachAction.STATUS_DISMISSED, 1)

        summary = self.client.get(reverse("queue-done")).data["summary"]

        # Approved items only: a dismissed lead is not pipeline.
        self.assertEqual(summary["pipeline_value_usd"], 2_000_000)
        self.assertEqual(summary["elapsed_seconds"], 19 * 60)
        self.assertIsNotNone(summary["first_action_at"])
        self.assertIsNotNone(summary["last_action_at"])

    def test_elapsed_is_null_below_two_items(self):
        self._decided("lead_001", OutreachAction.STATUS_APPROVED, 5)
        summary = self.client.get(reverse("queue-done")).data["summary"]
        self.assertIsNone(summary["elapsed_seconds"])

    def test_queue_cleared_needs_an_empty_queue_and_some_work(self):
        summary = self.client.get(reverse("queue-done")).data["summary"]
        # Nothing done yet: not "cleared", however empty the queue is.
        self.assertFalse(summary["queue_cleared"])
        self.assertEqual(summary["total"], 0)

        self._decided("lead_001", OutreachAction.STATUS_APPROVED, 5)
        self.assertTrue(self.client.get(reverse("queue-done")).data["summary"]["queue_cleared"])

        make_action(make_lead("lead_002"))  # one still pending
        self.assertFalse(self.client.get(reverse("queue-done")).data["summary"]["queue_cleared"])

    def test_requires_authentication(self):
        self.client.logout()
        self.assertIn(self.client.get(reverse("queue-done")).status_code, (401, 403))


class QueueEditViewTests(AuthenticatedAPITestCase):
    """POST /api/queue/{id}/edit/ -- CONTRACT section 5.2."""

    def setUp(self):
        super().setUp()
        self.action = make_action()

    def _edit(self, payload, action=None):
        return self.client.post(
            reverse("queue-edit", args=[(action or self.action).id]),
            payload,
            content_type="application/json",
        )

    def test_stores_the_edit_without_touching_the_original(self):
        original = self.action.suggested_copy

        resp = self._edit({"copy": "Subject: Mine\n\nHi there,\n\nBetter.\n"})

        self.assertEqual(resp.status_code, 200)
        self.action.refresh_from_db()
        # suggested_copy is immutable, forever: the diff IS the eval corpus.
        self.assertEqual(self.action.suggested_copy, original)
        self.assertEqual(self.action.edited_copy, "Subject: Mine\n\nHi there,\n\nBetter.\n")
        self.assertEqual(resp.data["effective_copy"], self.action.edited_copy)
        self.assertTrue(resp.data["is_edited"])

    def test_appends_an_edit_row_with_a_diff(self):
        self._edit({"copy": self.action.suggested_copy + " Extra."})

        edit = OutreachEdit.objects.get()
        self.assertEqual(edit.before_text, self.action.suggested_copy)
        self.assertEqual(edit.after_text, self.action.suggested_copy + " Extra.")
        self.assertEqual(edit.chars_added, 7)
        self.assertEqual(edit.editor, self.TEST_EMAIL)
        self.assertFalse(edit.committed)
        self.assertEqual(edit.diff_ops[0]["op"], "insert")

    def test_every_intermediate_edit_is_kept(self):
        self._edit({"copy": "Subject: One\n\nFirst.\n"})
        self._edit({"copy": "Subject: Two\n\nSecond.\n"})
        self.assertEqual(OutreachEdit.objects.count(), 2)
        # The corpus is append-only: the first draft is still there.
        self.assertEqual(OutreachEdit.objects.first().after_text, "Subject: One\n\nFirst.\n")

    def test_null_copy_reverts_to_the_original(self):
        self._edit({"copy": "Subject: Mine\n\nHi.\n"})

        resp = self._edit({"copy": None})

        self.action.refresh_from_db()
        self.assertEqual(self.action.edited_copy, "")
        self.assertEqual(resp.data["effective_copy"], self.action.suggested_copy)
        self.assertFalse(resp.data["is_edited"])
        # Reverting is itself a judgement worth keeping in the corpus.
        self.assertEqual(OutreachEdit.objects.last().after_text, self.action.suggested_copy)

    def test_line_endings_are_normalized_and_offsets_still_resolve(self):
        """Section 9.1c -- \\r\\n counts as two characters in Python."""
        resp = self._edit({"copy": "Subject: Hi\r\n\r\nHi there,\r\n\r\nA nudge.\r\n"})

        self.action.refresh_from_db()
        self.assertNotIn("\r", self.action.edited_copy)
        self.assertNotIn("\r", resp.data["verification"]["copy"])
        self.assertEqual(resp.data["verification"]["copy"], resp.data["effective_copy"])
        self.assertEqual(resp.data["verification"]["copy_length"], len(resp.data["effective_copy"]))

    def test_verification_is_rewritten_not_appended(self):
        resp = self._edit({"copy": "Subject: Mine\n\nHi.\n"})
        self.assertEqual(resp.data["verification"]["copy"], "Subject: Mine\n\nHi.\n")

    def test_empty_copy_is_rejected(self):
        resp = self._edit({"copy": "   \n  "})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["code"], "empty_copy")

    def test_missing_and_non_string_copy_are_rejected(self):
        self.assertEqual(self._edit({}).data["code"], "validation_error")
        self.assertEqual(self._edit({"copy": 42}).data["code"], "validation_error")

    def test_editing_an_approved_action_is_a_409(self):
        self.action.status = OutreachAction.STATUS_APPROVED
        self.action.save()

        resp = self._edit({"copy": "Subject: Mine\n\nHi.\n"})

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["code"], "invalid_transition")
        self.assertEqual(resp.data["detail"], 'Cannot edit an action with status "approved".')

    def test_a_snoozed_action_is_still_editable(self):
        self.action.status = OutreachAction.STATUS_SNOOZED
        self.action.save()
        self.assertEqual(self._edit({"copy": "Subject: Mine\n\nHi.\n"}).status_code, 200)

    def test_unknown_id_is_a_404(self):
        resp = self.client.post(
            reverse("queue-edit", args=[9999]), {"copy": "x"}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data["code"], "not_found")


class QueueVerifyViewTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.action = make_action()

    def _verify(self, payload):
        return self.client.post(
            reverse("queue-verify", args=[self.action.id]),
            payload,
            content_type="application/json",
        )

    def test_is_a_dry_run(self):
        resp = self._verify({"copy": "Subject: Draft\n\nHi.\n"})

        self.assertEqual(resp.status_code, 200)
        # The report envelope alone, not a QueueItem.
        self.assertNotIn("status", resp.data)
        self.assertEqual(resp.data["copy"], "Subject: Draft\n\nHi.\n")
        self.action.refresh_from_db()
        self.assertEqual(self.action.edited_copy, "")
        self.assertEqual(OutreachEdit.objects.count(), 0)

    def test_echoes_the_exact_copy_it_verified(self):
        # So a debounced out-of-order response can be discarded as stale
        # rather than rendered over newer text (section 9.2).
        resp = self._verify({"copy": "Subject: Draft\r\n\r\nHi.\n"})
        self.assertEqual(resp.data["copy"], "Subject: Draft\n\nHi.\n")

    def test_empty_copy_is_rejected(self):
        self.assertEqual(self._verify({"copy": "  "}).data["code"], "empty_copy")
        self.assertEqual(self._verify({}).data["code"], "empty_copy")


class QueueApproveViewTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.action = make_action()

    def _approve(self):
        return self.client.post(
            reverse("queue-approve", args=[self.action.id]), {}, content_type="application/json"
        )

    def test_approves_a_pending_action(self):
        resp = self._approve()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "approved")
        self.assertTrue(resp.data["undo"]["available"])
        self.assertIsNotNone(resp.data["undo"]["expires_at"])
        self.action.refresh_from_db()
        self.assertIsNotNone(self.action.status_changed_at)

    def test_commits_the_latest_edit(self):
        for text in ("Subject: One\n\nFirst.\n", "Subject: Two\n\nSecond.\n"):
            self.client.post(
                reverse("queue-edit", args=[self.action.id]),
                {"copy": text},
                content_type="application/json",
            )

        self._approve()

        edits = list(OutreachEdit.objects.order_by("created_at", "id"))
        self.assertFalse(edits[0].committed)
        self.assertTrue(edits[1].committed)  # what a human actually sent

    def test_unverified_claims_block_approval(self):
        """Section 4.6 -- the server is authoritative, not the frontend."""
        self.action.verification = {
            "version": 1,
            "copy": self.action.suggested_copy,
            "verified_count": 2,
            "unverified_count": 2,
            "checked_count": 4,
            "summary": "2 of 4 claims verified",
            "can_approve": False,
            "claims": [],
        }
        self.action.save()

        resp = self._approve()

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["code"], "unverified_claims")
        self.assertIn("2 of 4", resp.data["detail"])
        self.action.refresh_from_db()
        self.assertEqual(self.action.status, OutreachAction.STATUS_PENDING)

    def test_can_approve_mirrors_the_report(self):
        self.action.verification = {
            "copy": self.action.suggested_copy,
            "can_approve": False,
            "unverified_count": 1,
            "checked_count": 1,
        }
        self.action.save()

        item = self.client.get(reverse("queue-detail", args=[self.action.id])).data

        self.assertFalse(item["can_approve"])
        self.assertEqual(item["can_approve"], item["verification"]["can_approve"])

    def test_approving_a_dismissed_action_is_a_409(self):
        self.action.status = OutreachAction.STATUS_DISMISSED
        self.action.save()

        resp = self._approve()

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["code"], "invalid_transition")
        self.assertEqual(resp.data["detail"], 'Cannot approve an action with status "dismissed".')

    def test_a_snoozed_action_can_be_approved(self):
        self.action.status = OutreachAction.STATUS_SNOOZED
        self.action.save()
        self.assertEqual(self._approve().status_code, 200)


class QueueSnoozeViewTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.action = make_action()

    def _snooze(self, payload):
        return self.client.post(
            reverse("queue-snooze", args=[self.action.id]),
            payload,
            content_type="application/json",
        )

    def test_relative_triggers_return_at_0900(self):
        for trigger, days in (("tomorrow", 1), ("in_3_days", 3)):
            with self.subTest(trigger=trigger):
                resp = self._snooze({"trigger": trigger, "until": None})
                self.assertEqual(resp.status_code, 200)
                self.action.refresh_from_db()
                self.assertEqual(self.action.snooze_trigger, trigger)
                self.assertEqual(self.action.snooze_until.hour, 9)
                self.assertEqual(
                    self.action.snooze_until.date(),
                    timezone.now().date() + timedelta(days=days),
                )
                self.assertIsNone(self.action.snooze_activity_after)

    def test_next_week_lands_on_a_monday(self):
        self._snooze({"trigger": "next_week"})
        self.action.refresh_from_db()
        self.assertEqual(self.action.snooze_until.weekday(), 0)
        self.assertGreater(self.action.snooze_until, timezone.now())

    def test_custom_requires_a_future_timestamp(self):
        future = (timezone.now() + timedelta(days=5)).isoformat()

        resp = self._snooze({"trigger": "custom", "until": future})

        self.assertEqual(resp.status_code, 200)
        self.action.refresh_from_db()
        self.assertEqual(self.action.snooze_until.isoformat(), future)

    def test_custom_rejects_missing_and_past_timestamps(self):
        past = (timezone.now() - timedelta(days=1)).isoformat()
        for payload in ({"trigger": "custom"}, {"trigger": "custom", "until": past}):
            with self.subTest(payload=payload):
                resp = self._snooze(payload)
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.data["code"], "invalid_snooze")

    def test_on_activity_records_a_watermark_and_a_backstop(self):
        """Section 9.17 -- "when they do something" cannot mean "never"."""
        before = timezone.now()

        resp = self._snooze({"trigger": "on_activity"})

        self.assertEqual(resp.status_code, 200)
        self.action.refresh_from_db()
        self.assertGreaterEqual(self.action.snooze_activity_after, before)
        # snooze_until is non-NULL for EVERY snoozed row, on_activity included.
        self.assertIsNotNone(self.action.snooze_until)
        self.assertAlmostEqual(
            (self.action.snooze_until - self.action.snooze_activity_after).days, 14
        )

    def test_unknown_trigger_is_rejected(self):
        resp = self._snooze({"trigger": "next_year"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["code"], "invalid_snooze")

    def test_re_snoozing_refreshes_both_stamps(self):
        self._snooze({"trigger": "tomorrow"})
        self.action.refresh_from_db()
        first = self.action.snooze_until

        resp = self._snooze({"trigger": "next_week"})

        self.assertEqual(resp.status_code, 200)
        self.action.refresh_from_db()
        self.assertNotEqual(self.action.snooze_until, first)
        self.assertEqual(self.action.snooze_trigger, "next_week")

    def test_snoozing_an_approved_action_is_a_409(self):
        self.action.status = OutreachAction.STATUS_APPROVED
        self.action.save()
        resp = self._snooze({"trigger": "tomorrow"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["code"], "invalid_transition")


class QueueDismissViewTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.action = make_action()

    def _dismiss(self, payload=None):
        return self.client.post(
            reverse("queue-dismiss", args=[self.action.id]),
            payload if payload is not None else {},
            content_type="application/json",
        )

    def test_dismiss_writes_the_suppression_ledger(self):
        resp = self._dismiss({"reason": "not_a_fit"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "dismissed")
        self.assertEqual(resp.data["dismiss_reason"], "not_a_fit")
        key = DismissedOutreachKey.objects.get()
        self.assertEqual(key.dedupe_key, dedupe.dedupe_key(self.action.lead_id, "nudge_usage"))
        self.assertIsNone(key.revoked_at)
        self.assertEqual(key.dismissed_by, self.TEST_EMAIL)
        self.assertEqual(key.source_action_id, self.action.id)

    def test_reason_is_optional(self):
        self.assertEqual(self._dismiss().status_code, 200)
        self.assertEqual(DismissedOutreachKey.objects.get().reason, "")

    def test_unknown_reason_is_rejected(self):
        resp = self._dismiss({"reason": "because"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["code"], "invalid_reason")
        self.assertEqual(DismissedOutreachKey.objects.count(), 0)

    def test_dismissing_twice_is_idempotent_at_the_ledger(self):
        self._dismiss({"reason": "bad_timing"})
        other = make_action(self.action.lead, action_type="nudge_usage")
        self.client.post(
            reverse("queue-dismiss", args=[other.id]), {}, content_type="application/json"
        )
        self.assertEqual(DismissedOutreachKey.objects.count(), 1)

    def test_dismissing_an_approved_action_is_a_409(self):
        self.action.status = OutreachAction.STATUS_APPROVED
        self.action.save()
        self.assertEqual(self._dismiss().status_code, 409)


class QueueUndoViewTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.action = make_action()

    def _post(self, name, payload=None, action=None):
        return self.client.post(
            reverse(name, args=[(action or self.action).id]),
            payload if payload is not None else {},
            content_type="application/json",
        )

    def test_undo_of_approve_returns_to_pending(self):
        self._post("queue-approve")

        resp = self._post("queue-undo")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "pending")
        self.assertEqual(resp.data["undo"], {"available": False, "expires_at": None})

    def test_undo_of_snooze_clears_the_snooze(self):
        self._post("queue-snooze", {"trigger": "on_activity"})

        resp = self._post("queue-undo")

        self.assertEqual(resp.data["status"], "pending")
        self.assertEqual(
            resp.data["snooze"], {"until": None, "trigger": "", "activity_after": None}
        )

    def test_undo_does_not_discard_the_edit(self):
        self._post("queue-edit", {"copy": "Subject: Mine\n\nHi.\n"})
        self._post("queue-approve")

        resp = self._post("queue-undo")

        # Undoing a decision is not undoing the writing.
        self.assertEqual(resp.data["edited_copy"], "Subject: Mine\n\nHi.\n")
        self.assertEqual(OutreachEdit.objects.count(), 1)

    def test_undo_of_dismiss_revokes_the_suppression(self):
        """Section 9.7 -- the highest-consequence silent bug in this ticket.

        Asserting `status == pending` passes with the bug present. The only
        assertion that catches it is that a fresh planner run raises the
        recommendation again.
        """
        lead = make_lead("lead_undo", stage="demo_completed", signed_up_date=None)
        with patch("project.app.services.outreach.generate_copy", return_value=GOOD_COPY):
            plan_outreach()
        action = OutreachAction.objects.get(lead=lead)

        self._post("queue-dismiss", {"reason": "wrong_contact"}, action=action)
        self.assertEqual(DismissedOutreachKey.objects.filter(revoked_at=None).count(), 1)

        self._post("queue-undo", action=action)

        # The load-bearing assertion. The reviewer undoes, then approves; days
        # later the planner runs again and must be free to raise this
        # recommendation. Approving first is what makes the test real -- an
        # undone row is itself "open", so leaving it pending would satisfy
        # "an action exists" even with the suppression still in force.
        self._post("queue-approve", action=action)
        with patch("project.app.services.outreach.generate_copy", return_value=GOOD_COPY):
            plan_outreach()
        self.assertTrue(
            OutreachAction.objects.filter(
                lead=lead, action_type=action.action_type, status="pending"
            ).exists()
        )
        self.assertIsNotNone(DismissedOutreachKey.objects.get().revoked_at)

    def test_undo_outside_the_window_is_a_409(self):
        self._post("queue-approve")
        self.action.refresh_from_db()
        self.action.status_changed_at = timezone.now() - timedelta(seconds=301)
        self.action.save(update_fields=["status_changed_at"])

        resp = self._post("queue-undo")

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["code"], "undo_window_expired")
        self.assertIn("5-minute", resp.data["detail"])
        self.action.refresh_from_db()
        self.assertEqual(self.action.status, OutreachAction.STATUS_APPROVED)

    @override_settings(TRIAGE_UNDO_WINDOW_SECONDS=600)
    def test_the_window_is_configurable(self):
        self._post("queue-approve")
        self.action.refresh_from_db()
        self.action.status_changed_at = timezone.now() - timedelta(seconds=400)
        self.action.save(update_fields=["status_changed_at"])
        self.assertEqual(self._post("queue-undo").status_code, 200)

    def test_undoing_a_pending_action_is_a_409(self):
        resp = self._post("queue-undo")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["code"], "invalid_transition")
        self.assertIn("already pending", resp.data["detail"])


class QueueLifecycleTests(AuthenticatedAPITestCase):
    """The acceptance criterion: the whole lifecycle, driven from the API."""

    def test_edit_verify_approve_undo_dismiss(self):
        action = make_action()
        edit_url = reverse("queue-edit", args=[action.id])

        # 1. It shows up in the queue.
        self.assertEqual(len(self.client.get(reverse("queue-list")).data["items"]), 1)

        # 2. Verify a candidate without persisting it.
        dry = self.client.post(
            reverse("queue-verify", args=[action.id]),
            {"copy": "Subject: Draft\n\nHi.\n"},
            content_type="application/json",
        )
        self.assertEqual(dry.status_code, 200)

        # 3. Persist the edit, then approve.
        self.client.post(
            edit_url, {"copy": "Subject: Final\n\nHi.\n"}, content_type="application/json"
        )
        approved = self.client.post(
            reverse("queue-approve", args=[action.id]), {}, content_type="application/json"
        )
        self.assertEqual(approved.data["status"], "approved")

        # 4. It has left the queue and joined /done.
        self.assertEqual(self.client.get(reverse("queue-list")).data["counts"]["remaining"], 0)
        done = self.client.get(reverse("queue-done")).data
        self.assertEqual(done["summary"]["approved"], 1)
        self.assertTrue(done["summary"]["queue_cleared"])

        # 5. Undo puts it back, edit intact.
        undone = self.client.post(
            reverse("queue-undo", args=[action.id]), {}, content_type="application/json"
        )
        self.assertEqual(undone.data["status"], "pending")
        self.assertEqual(undone.data["effective_copy"], "Subject: Final\n\nHi.\n")

        # 6. Dismiss is final.
        self.client.post(
            reverse("queue-dismiss", args=[action.id]),
            {"reason": "already_handled"},
            content_type="application/json",
        )
        self.assertEqual(self.client.get(reverse("queue-list")).data["counts"]["remaining"], 0)
        self.assertEqual(DismissedOutreachKey.objects.filter(revoked_at=None).count(), 1)

    def test_mutations_require_authentication(self):
        action = make_action()
        self.client.logout()
        for name in ("queue-edit", "queue-approve", "queue-snooze", "queue-dismiss", "queue-undo"):
            with self.subTest(endpoint=name):
                resp = self.client.post(
                    reverse(name, args=[action.id]), {}, content_type="application/json"
                )
                self.assertIn(resp.status_code, (401, 403))
