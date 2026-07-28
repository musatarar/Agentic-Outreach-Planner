"""Tests for the triage queue state model and API (MUS-39).

Kept in its own module: MUS-39 never edits ``tests_api.py`` (CONTRACT MUS-35
section 8.2), so the two suites can be reviewed and merged independently.
"""

from datetime import date

from django.db import IntegrityError, transaction
from django.test import TestCase

from project.app.models import DismissedOutreachKey, Lead, OutreachAction, OutreachEdit


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
