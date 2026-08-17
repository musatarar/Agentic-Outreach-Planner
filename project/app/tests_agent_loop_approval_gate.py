"""Component artifact: approval_gate (MUS-29).

Nothing sends without a recorded human approval: the hash-bound one-shot dispatch gate,
queue-recorded send decisions, and the serializer that keeps them out of triage.
"""

import hashlib
import itertools
from unittest import mock

from django.utils import timezone

from project.app import models as app_models  # OutboundSend: lazy until Task 3 lands
from project.app.models import Lead, OutreachAction, ReviewDecision
from project.app.services import dispatch
from project.app.tests_auth_utils import AuthenticatedAPITestCase

_ids = itertools.count(1)


def _pending_action():
    """Fixture identical in shape to tests_queue.py's make_lead/make_action rows:
    default copy verifies cleanly, so /approve/ returns 200."""
    n = next(_ids)
    lead = Lead.objects.create(
        id=f"lead_ag{n}",
        agency_name=f"Agency lead_ag{n}",
        contact_name=f"Contact lead_ag{n}",
        contact_email=f"lead_ag{n}@example.com",
        contact_phone="555-0100",
        state="CA",
        num_producers=3,
        years_in_business=5,
        estimated_book_size_usd=1_000_000,
        stage="active_trial",
    )
    return OutreachAction.objects.create(
        lead=lead,
        priority=2,
        action_type="nudge_usage",
        reason="Underusing the portal.",
        suggested_copy="Subject: Hello\n\nHi there,\n\nA nudge.\n\nBest,\nDana",
    )


class DispatchGateTests(AuthenticatedAPITestCase):
    def _approve(self, action):
        return self.client.post(
            f"/api/queue/{action.pk}/approve/", data={}, content_type="application/json"
        )

    def _dismiss(self, action):
        return self.client.post(
            f"/api/queue/{action.pk}/dismiss/", data={}, content_type="application/json"
        )

    def _undo(self, action):
        return self.client.post(
            f"/api/queue/{action.pk}/undo/", data={}, content_type="application/json"
        )

    def test_dispatch_hard_raises_without_an_approve_send_decision(self):
        self.assertTrue(hasattr(app_models, "OutboundSend"))  # red at skeleton
        action = _pending_action()
        action.status = OutreachAction.STATUS_APPROVED
        action.save(update_fields=["status"])
        with self.assertRaises(dispatch.DispatchBlocked):
            dispatch.dispatch(action)
        self.assertFalse(app_models.OutboundSend.objects.filter(outreach_action=action).exists())

    def test_queue_approve_records_reviewer_hash_and_copy_snapshot(self):
        self.assertTrue(hasattr(ReviewDecision, "KIND_APPROVE_SEND"))  # red at skeleton
        action = _pending_action()
        resp = self._approve(action)
        self.assertEqual(resp.status_code, 200)
        decision = ReviewDecision.objects.get(
            outreach_action=action, kind=ReviewDecision.KIND_APPROVE_SEND
        )
        self.assertEqual(decision.status, ReviewDecision.STATUS_RESOLVED)
        self.assertNotEqual(decision.reviewer, "")  # session identity, never free text
        self.assertEqual(decision.approved_copy, action.effective_copy)
        self.assertEqual(
            decision.approved_body_sha256,
            hashlib.sha256(action.effective_copy.encode("utf-8")).hexdigest(),
        )

    def test_post_approval_edit_voids_the_send_via_hash_mismatch(self):
        self.assertTrue(hasattr(ReviewDecision, "KIND_APPROVE_SEND"))  # red at skeleton
        action = _pending_action()
        self._approve(action)
        action.refresh_from_db()
        OutreachAction.objects.filter(pk=action.pk).update(
            edited_copy="tampered after approval"  # simulate any post-approval change
        )
        action.refresh_from_db()
        with self.assertRaises(dispatch.DispatchBlocked):
            dispatch.dispatch(action)

    def test_send_is_one_shot(self):
        self.assertTrue(hasattr(app_models, "OutboundSend"))  # red at skeleton
        action = _pending_action()
        self._approve(action)
        action.refresh_from_db()
        record = dispatch.dispatch(action)
        action.refresh_from_db()
        self.assertEqual(action.status, OutreachAction.STATUS_SENT)
        self.assertEqual(
            record.body_sha256, hashlib.sha256(action.effective_copy.encode()).hexdigest()
        )
        self.assertEqual(app_models.OutboundSend.objects.filter(outreach_action=action).count(), 1)
        with self.assertRaises(dispatch.DispatchBlocked):
            dispatch.dispatch(action)  # CAS finds no approved row

    def test_serializer_rejects_send_kinds(self):
        self.assertTrue(hasattr(ReviewDecision, "SEND_KINDS"))  # red at skeleton
        action = _pending_action()
        OutreachAction.objects.filter(pk=action.pk).update(needs_human=True)
        for kind in ("approve_send", "reject_send"):
            with self.subTest(kind=kind):
                resp = self.client.post(
                    "/api/review-decisions/",
                    data={"outreach_action": action.pk, "kind": kind},
                    content_type="application/json",
                )
                self.assertEqual(resp.status_code, 400)
                # The error names the queue endpoints that own send decisions.
                self.assertIn("queue", str(resp.data).lower())

    def test_undo_voids_the_approval_and_a_voided_approval_never_authorizes(self):
        self.assertTrue(hasattr(ReviewDecision, "KIND_APPROVE_SEND"))  # red at skeleton
        action = _pending_action()
        self._approve(action)
        resp = self._undo(action)
        self.assertEqual(resp.status_code, 200)
        decision = ReviewDecision.objects.get(
            outreach_action=action, kind=ReviewDecision.KIND_APPROVE_SEND
        )
        self.assertIsNotNone(decision.voided_at)  # kept for audit, never authorizes
        OutreachAction.objects.filter(pk=action.pk).update(status=OutreachAction.STATUS_APPROVED)
        action.refresh_from_db()
        with self.assertRaises(dispatch.DispatchBlocked):
            dispatch.dispatch(action)

    def test_dismiss_records_a_reject_send_decision(self):
        """Dismissal records a reviewer-attributed rejection, not just a status flip."""
        self.assertTrue(hasattr(ReviewDecision, "KIND_REJECT_SEND"))  # red at skeleton
        action = _pending_action()
        resp = self._dismiss(action)
        self.assertEqual(resp.status_code, 200)
        decision = ReviewDecision.objects.get(
            outreach_action=action, kind=ReviewDecision.KIND_REJECT_SEND
        )
        self.assertEqual(decision.status, ReviewDecision.STATUS_RESOLVED)
        self.assertNotEqual(decision.reviewer, "")
        self.assertIsNone(decision.voided_at)
        self.assertEqual(decision.approved_copy, "")  # nothing is authorized by a rejection

    def test_undo_of_dismiss_voids_the_rejection_and_reapprove_creates_a_fresh_decision(self):
        self.assertTrue(hasattr(ReviewDecision, "SEND_KINDS"))  # red at skeleton
        action = _pending_action()
        self._dismiss(action)
        self._undo(action)
        reject = ReviewDecision.objects.get(
            outreach_action=action, kind=ReviewDecision.KIND_REJECT_SEND
        )
        self.assertIsNotNone(reject.voided_at)
        resp = self._approve(action)  # rd_one_live_send_per_action permits a fresh live row
        self.assertEqual(resp.status_code, 200)
        live = ReviewDecision.objects.get(
            outreach_action=action,
            kind=ReviewDecision.KIND_APPROVE_SEND,
            voided_at__isnull=True,
        )
        self.assertEqual(live.status, ReviewDecision.STATUS_RESOLVED)

    def test_a_send_is_never_recorded_against_an_approval_voided_before_it_commits(self):
        """A send is attributed to the approval live when it commits, over the bytes
        current when it commits — undo/edit/re-approve interleaved inside the window."""
        action = _pending_action()
        self.assertEqual(self._approve(action).status_code, 200)
        action.refresh_from_db()
        voided = ReviewDecision.objects.get(
            outreach_action=action, kind=ReviewDecision.KIND_APPROVE_SEND
        )
        recopy = "Subject: Hello again\n\nHi there,\n\nA second nudge.\n\nBest,\nDana"

        fired = []

        def reviewer_cycle_then_atomic(*args, **kwargs):
            # Only the outermost call is the send transaction; the views the
            # cycle drives open their own, and must delegate untouched.
            if not fired:
                fired.append(True)
                self.assertEqual(self._undo(action).status_code, 200)
                OutreachAction.objects.filter(pk=action.pk).update(edited_copy=recopy)
                self.assertEqual(self._approve(action).status_code, 200)
            return real_atomic(*args, **kwargs)

        real_atomic = dispatch.transaction.atomic
        with mock.patch.object(
            dispatch.transaction, "atomic", side_effect=reviewer_cycle_then_atomic
        ):
            record = dispatch.dispatch(action)

        self.assertTrue(fired)  # the interleave really happened
        voided.refresh_from_db()
        self.assertIsNotNone(voided.voided_at)
        live = ReviewDecision.objects.get(
            outreach_action=action,
            kind=ReviewDecision.KIND_APPROVE_SEND,
            voided_at__isnull=True,
        )
        # A retracted approval authorizes nothing, and the digest on the record
        # is the digest of what is actually there to send.
        self.assertNotEqual(record.decision_id, voided.pk)
        self.assertEqual(record.decision_id, live.pk)
        self.assertEqual(record.body_sha256, hashlib.sha256(recopy.encode("utf-8")).hexdigest())
        self.assertEqual(record.body_sha256, live.approved_body_sha256)

    def test_a_void_with_no_reapproval_blocks_the_send_and_rolls_the_cas_back(self):
        """A void with no fresh approval leaves nothing live, so the CAS rolls back
        rather than marking the action sent."""
        action = _pending_action()
        self.assertEqual(self._approve(action).status_code, 200)
        action.refresh_from_db()

        fired = []

        def void_then_atomic(*args, **kwargs):
            if not fired:
                fired.append(True)
                # Void alone — the status flip that normally accompanies undo is
                # left off, so the CAS still finds `approved` and wins.
                ReviewDecision.objects.filter(
                    outreach_action=action, kind__in=ReviewDecision.SEND_KINDS
                ).update(voided_at=timezone.now())
            return real_atomic(*args, **kwargs)

        real_atomic = dispatch.transaction.atomic
        with mock.patch.object(dispatch.transaction, "atomic", side_effect=void_then_atomic):
            with self.assertRaises(dispatch.DispatchBlocked):
                dispatch.dispatch(action)

        self.assertTrue(fired)
        self.assertFalse(app_models.OutboundSend.objects.filter(outreach_action=action).exists())
        action.refresh_from_db()
        self.assertEqual(action.status, OutreachAction.STATUS_APPROVED)  # CAS rolled back

    def test_dispatch_binds_the_send_to_the_live_decision_it_reverified(self):
        """An untouched approval still sends, and the ``OutboundSend`` points at the
        decision that authorized it."""
        action = _pending_action()
        self.assertEqual(self._approve(action).status_code, 200)
        action.refresh_from_db()
        live = ReviewDecision.objects.get(
            outreach_action=action,
            kind=ReviewDecision.KIND_APPROVE_SEND,
            voided_at__isnull=True,
        )

        record = dispatch.dispatch(action)

        self.assertEqual(record.decision_id, live.pk)
        self.assertEqual(record.body_sha256, live.approved_body_sha256)

    def test_unverified_claims_still_block_approval_with_a_409(self):
        """A fail-closed approve 409s and records no decision."""
        self.assertTrue(hasattr(ReviewDecision, "KIND_APPROVE_SEND"))  # red at skeleton
        action = _pending_action()
        OutreachAction.objects.filter(pk=action.pk).update(
            verification={
                "version": 1,
                "copy": action.suggested_copy,
                "verified_count": 2,
                "unverified_count": 2,
                "checked_count": 4,
                "summary": "2 of 4 claims verified",
                "can_approve": False,
                "claims": [],
            }
        )
        resp = self._approve(action)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["code"], "unverified_claims")
        self.assertFalse(
            ReviewDecision.objects.filter(
                outreach_action=action, kind__in=ReviewDecision.SEND_KINDS
            ).exists()
        )
