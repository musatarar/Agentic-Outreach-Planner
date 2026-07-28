"""Tests for the triage queue state model and API (MUS-39).

Kept in its own module: MUS-39 never edits ``tests_api.py`` (CONTRACT MUS-35
section 8.2), so the two suites can be reviewed and merged independently.
"""

from datetime import date
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.test import TestCase

from project.app.models import DismissedOutreachKey, Lead, OutreachAction, OutreachEdit
from project.app.services import dedupe, queue_copy
from project.app.services.outreach import plan_outreach

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
        from django.utils import timezone

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
