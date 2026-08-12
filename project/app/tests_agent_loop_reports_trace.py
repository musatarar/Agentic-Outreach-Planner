"""Component artifact: reports_trace (MUS-29).

Planted red by the skeleton PR — every test carries ``@unittest.expectedFailure``.
``setUpTestData`` guards its new-model seeding behind a hasattr check because a
class-setup error is not absorbed by per-test markers. The reports_trace
component PR strips the markers and takes this module (plus the frontend
artifact) to zero.
"""

import unittest

from project.app import models as app_models  # AgentLeadRun/AgentStep: lazy until Task 3 lands
from project.app.models import Lead, OutreachAction
from project.app.tests_auth_utils import AuthenticatedAPITestCase


class TraceEndpointTests(AuthenticatedAPITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.lead = Lead.objects.create(
            id="lead_tr1",
            agency_name="A",
            contact_name="C",
            contact_email="c@a.com",
            contact_phone="1",
            state="TX",
            num_producers=1,
            years_in_business=1,
            estimated_book_size_usd=1,
            stage="active_trial",
        )
        cls.action = OutreachAction.objects.create(
            lead=cls.lead,
            priority=1,
            action_type="trial_engagement_followup",
            reason="r",
            trace_run_id="run-tr-1",
        )
        if not hasattr(app_models, "AgentLeadRun"):
            return  # skeleton: class setup must not error; tests go red on their asserts
        run = app_models.AgentLeadRun.objects.create(
            lead=cls.lead, trace_run_id="run-tr-1", status="done", steps_used=2, tool_calls_used=1
        )
        app_models.AgentStep.objects.create(
            lead_run=run,
            seq=1,
            kind="llm_call",
            payload={
                "text": "",
                "tool_calls": [{"id": "c1", "name": "get_lead_history", "arguments": {}}],
            },
        )
        app_models.AgentStep.objects.create(
            lead_run=run,
            seq=2,
            kind="tool_result",
            payload={"tool_call_id": "c1", "name": "get_lead_history", "result": '{"events": []}'},
        )

    @unittest.expectedFailure
    def test_trace_returns_ordered_steps_for_an_agent_action(self):
        self.assertTrue(hasattr(app_models, "AgentLeadRun"))  # red at skeleton
        resp = self.client.get(f"/api/outreach/{self.action.pk}/trace/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual([s["seq"] for s in body["steps"]], [1, 2])
        self.assertEqual(body["steps"][0]["kind"], "llm_call")
        self.assertEqual(body["status"], "done")
        self.assertEqual(body["lead_id"], "lead_tr1")
        self.assertEqual(body["trace_run_id"], "run-tr-1")

    @unittest.expectedFailure
    def test_single_shot_actions_404_cleanly(self):
        # First statement calls into the NotImplementedError stub view at skeleton.
        bare = OutreachAction.objects.create(
            lead=self.lead, priority=2, action_type="trial_engagement_followup", reason="r"
        )
        resp = self.client.get(f"/api/outreach/{bare.pk}/trace/")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"], "no_agent_trace")
