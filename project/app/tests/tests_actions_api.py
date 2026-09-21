"""The proposed-actions API: what the engine chose, and copy for one on demand.

Pins owner scoping (a foreign job reads as 404, someone else's lead is not my
proposal), that only a job which actually chose an action is a proposal, and
that the two refusals answer 409 without spending a provider call.
"""

import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings

from project.app.actions import services
from project.app.actions.models import ActionJob
from project.app.models import ActionType, DismissedOutreachKey, Lead, OutreachAction, OutreachRule
from project.app.rules.utils import _all_of, _cond
from project.app.services import dedupe
from project.app.services.llm import LLMClient, LLMResult, LLMTimeoutError
from project.app.services.llm.structured import StructuredResult
from project.app.tests.tests_auth_utils import AuthenticatedAPITestCase
from project.app.tests.tests_shape_utils import shape_for

ACTIONS_URL = "/api/actions/"
INBOX_URL = "/api/outreach/"
TODAY = datetime.date(2026, 6, 12)

COPY = (
    "Subject: A quick idea for Summit Risk Advisors\n\n"
    "Hi Priya,\n\n"
    "Summit Risk Advisors has been working steadily through the portal, and I "
    "wanted to share one small change that usually helps agencies of this size "
    "get more quotes over the line. It takes about fifteen minutes to walk "
    "through, and your producers can start using it the same day. I would "
    "rather show you than write it all out here, since the useful part is "
    "seeing it against your own book. Would you have time for a short call "
    "this week?\n\n"
    "Best,\nDana"
)


class _ProviderStub(LLMClient):
    """A real ``LLMClient`` recording every prompt it is asked to complete, so
    "no provider call" is an assertion rather than an assumption."""

    provider_name = "stub"

    def __init__(self, text=COPY, error=None):
        super().__init__(model="stub-1")
        self.text = text
        self.error = error
        self.prompts = []

    def generate(self, prompt, max_tokens=None, timeout=None):
        raise AssertionError("copy generation must take the structured path")

    def generate_structured(self, input, schema_model, *, max_tokens=None, timeout=None):
        self.prompts.append(input)
        if self.error is not None:
            raise self.error
        subject, _, body = self.text.partition("\n\n")
        parsed = schema_model(subject=subject.removeprefix("Subject: "), body=body)
        return StructuredResult(
            parsed=parsed,
            result=LLMResult(
                text=parsed.model_dump_json(), provider=self.provider_name, model=self.model
            ),
        )


class ProposedActionsTestCase(AuthenticatedAPITestCase):
    """DRF keeps throttle history in the default cache, which outlives a test."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.other = get_user_model().objects.create_user(username="teammate@lockedin.example")
        self.shape = shape_for(self.user)
        self.other_shape = shape_for(self.other)
        self.lead = self._lead(owner=self.user)
        self.action = self._action()
        self.stub = _ProviderStub()

    def _lead(self, lead_id="lead_001", *, owner, **kwargs):
        """``owner`` is never defaulted: whose book the lead sits in is the
        thing these tests are about."""
        data = dict(
            agency_name="Summit Risk Advisors",
            contact_name="Priya Nair",
            contact_email="priya@summitrisk.example.com",
            contact_phone="555-0100",
            state="CO",
            num_producers=4,
            years_in_business=9,
            estimated_book_size_usd=1_400_000,
            stage="active_trial",
            signed_up_date=(TODAY - datetime.timedelta(days=50)).isoformat(),
            last_login_date=(TODAY - datetime.timedelta(days=2)).isoformat(),
            last_contacted_date=(TODAY - datetime.timedelta(days=5)).isoformat(),
            quotes_created=10,
            quotes_submitted=6,
            deals_closed=3,
            hubspot_notes="",
        )
        data.update(kwargs)
        return Lead.objects.create(id=lead_id, owner=owner, data=data)

    def _action(self, owner=None, key="reward_power_user", urgency=ActionType.URGENCY_HIGH):
        return ActionType.objects.create(
            owner=owner or self.user, key=key, label="Reward power user", urgency=urgency
        )

    def _rule(self, action, name="Closed more than two deals"):
        return OutreachRule.objects.create(
            owner=action.owner,
            action=action,
            name=name,
            kind=OutreachRule.KIND_DETERMINISTIC,
            conditions=_all_of(_cond("deals_closed", ">", 2)),
            weight=OutreachRule.WEIGHT_HIGH,
        )

    def _proposal(self, lead=None, action=None, name="Closed more than two deals"):
        """One job run through the real engine, so its decision payload is the
        one the engine writes rather than a hand-made lookalike."""
        lead = lead or self.lead
        self._rule(action or self.action, name=name)
        job = services.enqueue_lead(lead)
        services.claim(job)
        services.run_job(job, today=TODAY)
        job.refresh_from_db()
        assert job.status == ActionJob.STATUS_DETERMINISTIC_ACTION_CHOSEN, job.status
        return job

    def _generate(self, job):
        with patch("project.app.services.outreach.get_llm_client", return_value=self.stub):
            return self.client.post(f"{ACTIONS_URL}{job.pk}/generate/", {}, format="json")

    def _rows(self):
        return self.client.get(ACTIONS_URL).json()["results"]


class ProposedActionListTests(ProposedActionsTestCase):
    def test_the_list_requires_a_signed_in_session(self):
        self.client.logout()
        response = self.client.get(ACTIONS_URL)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "not_authenticated")

    def test_a_proposal_carries_its_lead_its_action_and_the_rules_that_fired(self):
        job = self._proposal()

        rows = self._rows()

        self.assertEqual([row["id"] for row in rows], [job.pk])
        row = rows[0]
        self.assertEqual(row["lead"]["id"], self.lead.id)
        self.assertEqual(row["lead"]["data"]["agency_name"], "Summit Risk Advisors")
        self.assertEqual(
            row["action"],
            {"key": "reward_power_user", "label": "Reward power user", "urgency": "high"},
        )
        self.assertEqual(row["reasons"], ["Closed more than two deals"])
        self.assertEqual(row["weight"], OutreachRule.WEIGHT_HIGH)
        self.assertIsNone(row["draft_id"])
        self.assertIsNotNone(row["decided_at"])

    def test_the_engines_workings_never_cross_the_wire(self):
        # `decision` carries rule ids and the inference pass's verdicts.
        self._proposal()

        self.assertNotIn("decision", self._rows()[0])

    def test_a_job_that_chose_no_action_is_not_a_proposal(self):
        ActionJob.objects.create(lead=self.lead, status=ActionJob.STATUS_NO_ACTION)

        self.assertEqual(self._rows(), [])

    def test_a_failed_job_is_not_a_proposal(self):
        ActionJob.objects.create(
            lead=self.lead, status=ActionJob.STATUS_FAILED, selected_action=self.action
        )

        self.assertEqual(self._rows(), [])

    def test_a_job_the_engine_has_not_decided_yet_is_not_a_proposal(self):
        for status in ActionJob.OPEN_STATUSES:
            with self.subTest(status=status):
                ActionJob.objects.filter(lead=self.lead).delete()
                ActionJob.objects.create(lead=self.lead, status=status, selected_action=self.action)

                self.assertEqual(self._rows(), [])

    def test_the_list_is_scoped_to_the_leads_i_own(self):
        theirs = self._lead("lead_002", owner=self.other, agency_name="Harbor Insurance")
        self._proposal(lead=theirs, action=self._action(owner=self.other))

        self.assertEqual(self._rows(), [])

    def test_an_unowned_lead_proposes_nothing_to_anybody(self):
        orphan = self._lead("lead_003", owner=None)
        ActionJob.objects.create(
            lead=orphan,
            status=ActionJob.STATUS_DETERMINISTIC_ACTION_CHOSEN,
            selected_action=self.action,
        )

        self.assertEqual(self._rows(), [])


@override_settings(COPY_VERIFY_LEVEL="off")
class GenerateCopyTests(ProposedActionsTestCase):
    """The grounding level is pinned off: what this endpoint owes is a draft
    through the gates, not the verifier's opinion of one fixture's prose."""

    def test_generating_writes_one_draft_for_the_catalog_action(self):
        job = self._proposal()

        response = self._generate(job)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(self.stub.prompts), 1)
        draft = OutreachAction.objects.get()
        self.assertEqual(draft.pk, response.json()["id"])
        self.assertEqual(draft.lead_id, self.lead.id)
        self.assertEqual(draft.action_type, "reward_power_user")
        self.assertEqual(draft.reason, "Closed more than two deals")
        self.assertEqual(draft.suggested_copy, COPY)
        self.assertEqual(draft.dedupe_key, dedupe.dedupe_key(self.lead.id, "reward_power_user"))

    def test_the_draft_is_visible_in_the_inbox(self):
        job = self._proposal()

        created = self._generate(job).json()["id"]

        listed = self.client.get(INBOX_URL).json()["results"]
        self.assertEqual([row["id"] for row in listed], [created])
        self.assertEqual(listed[0]["action_type"], "reward_power_user")

    def test_the_draft_carries_the_verification_snapshot_the_approval_gate_reads(self):
        job = self._proposal()

        self._generate(job)

        draft = OutreachAction.objects.get()
        self.assertEqual(draft.verification["copy"], draft.suggested_copy)
        self.assertEqual(draft.verification["version"], 1)

    def test_the_drafts_priority_is_the_urgency_its_owner_declared(self):
        job = self._proposal(action=self._action(key="nudge_usage", urgency="low"))

        self._generate(job)

        self.assertEqual(OutreachAction.objects.get().priority, 3)

    def test_a_drafted_proposal_carries_its_draft_id_and_refuses_a_second_call(self):
        job = self._proposal()
        created = self._generate(job).json()["id"]

        self.assertEqual(self._rows()[0]["draft_id"], created)

        again = self._generate(job)

        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()["code"], "no_new_recommendation")
        self.assertEqual(len(self.stub.prompts), 1)
        self.assertEqual(OutreachAction.objects.count(), 1)

    def test_a_dismissed_key_is_refused_before_the_provider_call(self):
        job = self._proposal()
        DismissedOutreachKey.objects.create(
            dedupe_key=dedupe.dedupe_key(self.lead.id, "reward_power_user"),
            lead=self.lead,
            action_type="reward_power_user",
        )

        response = self._generate(job)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "no_new_recommendation")
        self.assertEqual(self.stub.prompts, [])
        self.assertFalse(OutreachAction.objects.exists())

    def test_someone_elses_proposal_reads_as_not_found(self):
        theirs = self._lead("lead_002", owner=self.other, agency_name="Harbor Insurance")
        job = self._proposal(lead=theirs, action=self._action(owner=self.other))

        response = self._generate(job)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.stub.prompts, [])
        self.assertFalse(OutreachAction.objects.exists())

    def test_a_job_that_chose_nothing_cannot_be_drafted(self):
        job = ActionJob.objects.create(lead=self.lead, status=ActionJob.STATUS_NO_ACTION)

        response = self._generate(job)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(OutreachAction.objects.exists())

    def test_a_provider_failure_lands_as_a_row_a_reviewer_can_act_on(self):
        job = self._proposal()
        self.stub = _ProviderStub(error=LLMTimeoutError("timed out", provider="stub"))

        response = self._generate(job)

        self.assertEqual(response.status_code, 201)
        draft = OutreachAction.objects.get()
        self.assertEqual(draft.suggested_copy, "")
        self.assertTrue(draft.needs_human)
        self.assertIn("timeouts", draft.further_action)

    def test_a_failed_attempt_does_not_hold_the_key_against_a_retry(self):
        job = self._proposal()
        self.stub = _ProviderStub(error=LLMTimeoutError("timed out", provider="stub"))
        self._generate(job)
        self.stub = _ProviderStub()

        response = self._generate(job)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(OutreachAction.objects.get(pk=response.json()["id"]).suggested_copy, COPY)

    def test_a_provider_bug_lands_as_a_row_too_rather_than_a_dead_endpoint(self):
        # Not an LLMError at all: the adapter itself misbehaved.
        job = self._proposal()
        self.stub = _ProviderStub(error=ValueError("adapter blew up"))

        response = self._generate(job)

        self.assertEqual(response.status_code, 201)
        draft = OutreachAction.objects.get()
        self.assertEqual(draft.suggested_copy, "")
        self.assertTrue(draft.needs_human)
        self.assertIn("reward_power_user", draft.further_action)
