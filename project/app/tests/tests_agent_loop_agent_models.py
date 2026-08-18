"""Component artifact: agent_models (MUS-29).

Pins the agent-loop schema (migration 0007_agent_loop): ``AgentLeadRun`` epoch-CAS
claims, append-only ``AgentStep``, ``ReviewDecision`` send kinds, and terminal ``sent``.
"""

from django.db import IntegrityError, transaction
from django.test import TestCase

from project.app import models as app_models
from project.app.models import Lead, OutreachAction, ReviewDecision


def _lead(lead_id="lead_am1"):
    return Lead.objects.create(
        id=lead_id,
        agency_name="A",
        contact_name="C",
        contact_email="c@a.com",
        contact_phone="1",
        state="TX",
        num_producers=3,
        years_in_business=4,
        estimated_book_size_usd=1000000,
        stage="active_trial",
    )


class ReviewDecisionSendKindTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.action = OutreachAction.objects.create(
            lead=_lead(),
            priority=1,
            action_type="trial_engagement_followup",
            reason="r",
            needs_human=True,
        )

    def test_action_holds_one_resolution_and_one_send_decision(self):
        self.assertTrue(hasattr(ReviewDecision, "KIND_APPROVE_SEND"))  # red at skeleton
        ReviewDecision.objects.create(
            outreach_action=self.action,
            kind=ReviewDecision.KIND_SELECT,
            status=ReviewDecision.STATUS_RESOLVED,
            selected_action_type="trial_engagement_followup",
        )
        ReviewDecision.objects.create(
            outreach_action=self.action,
            kind=ReviewDecision.KIND_APPROVE_SEND,
            status=ReviewDecision.STATUS_RESOLVED,
            reviewer="bd@lockedin.example",
            approved_copy="Subject: x\n\nbody",
            approved_body_sha256="0" * 64,
        )
        self.assertEqual(self.action.review_decisions.count(), 2)

    def test_second_live_send_decision_is_rejected_by_the_db(self):
        self.assertTrue(hasattr(ReviewDecision, "KIND_APPROVE_SEND"))
        ReviewDecision.objects.create(
            outreach_action=self.action,
            kind=ReviewDecision.KIND_APPROVE_SEND,
            status=ReviewDecision.STATUS_RESOLVED,
            reviewer="a@x.example",
            approved_body_sha256="0" * 64,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ReviewDecision.objects.create(
                outreach_action=self.action,
                kind=ReviewDecision.KIND_REJECT_SEND,
                status=ReviewDecision.STATUS_RESOLVED,
                reviewer="b@x.example",
            )

    def test_kind_constants_partition_into_resolution_and_send(self):
        self.assertTrue(hasattr(ReviewDecision, "SEND_KINDS"))
        self.assertEqual(
            ReviewDecision.RESOLUTION_KINDS,
            (ReviewDecision.KIND_SELECT, ReviewDecision.KIND_PROPOSE),
        )
        self.assertEqual(
            ReviewDecision.SEND_KINDS,
            (ReviewDecision.KIND_APPROVE_SEND, ReviewDecision.KIND_REJECT_SEND),
        )


class AgentRunSchemaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.lead = _lead("lead_am2")

    def test_second_step_with_same_seq_is_rejected(self):
        self.assertTrue(hasattr(app_models, "AgentStep"))  # red at skeleton
        run = app_models.AgentLeadRun.objects.create(lead=self.lead, trace_run_id="run-am-seq")
        app_models.AgentStep.objects.create(lead_run=run, seq=1, kind="llm_call", payload={})
        with self.assertRaises(IntegrityError), transaction.atomic():
            app_models.AgentStep.objects.create(lead_run=run, seq=1, kind="final", payload={})

    def test_one_run_per_lead_per_trace(self):
        self.assertTrue(hasattr(app_models, "AgentLeadRun"))
        app_models.AgentLeadRun.objects.create(lead=self.lead, trace_run_id="run-am-uniq")
        with self.assertRaises(IntegrityError), transaction.atomic():
            app_models.AgentLeadRun.objects.create(lead=self.lead, trace_run_id="run-am-uniq")

    def test_claim_epoch_cas_yields_exactly_one_winner(self):
        """Two claim attempts computed from the same read epoch: rowcount picks
        the winner — pure ORM, no threads, portable to both backends."""
        self.assertTrue(hasattr(app_models, "AgentLeadRun"))
        alr = app_models.AgentLeadRun
        run = alr.objects.create(lead=self.lead, trace_run_id="run-am-cas")
        seen = alr.objects.get(pk=run.pk).claim_epoch

        def attempt(token):
            return alr.objects.filter(
                pk=run.pk,
                claim_epoch=seen,
                status__in=alr.NON_TERMINAL_STATUSES,
            ).update(claim_epoch=seen + 1, claimed_by=token, status="claimed")

        self.assertEqual(attempt("worker-a"), 1)
        self.assertEqual(attempt("worker-b"), 0)
        row = alr.objects.get(pk=run.pk)
        self.assertEqual(row.claimed_by, "worker-a")
        self.assertEqual(row.claim_epoch, seen + 1)

    def test_approved_to_sent_is_allowed_and_sent_is_terminal(self):
        self.assertTrue(hasattr(OutreachAction, "STATUS_SENT"))  # red at skeleton
        action = OutreachAction.objects.create(
            lead=self.lead, priority=1, action_type="trial_engagement_followup", reason="r"
        )
        action.status = OutreachAction.STATUS_APPROVED
        self.assertTrue(action.can_transition_to(OutreachAction.STATUS_SENT))
        self.assertTrue(action.can_transition_to(OutreachAction.STATUS_PENDING))
        action.status = OutreachAction.STATUS_SENT
        for target in (
            OutreachAction.STATUS_PENDING,
            OutreachAction.STATUS_APPROVED,
            OutreachAction.STATUS_SNOOZED,
            OutreachAction.STATUS_DISMISSED,
        ):
            self.assertFalse(action.can_transition_to(target))
