"""The ``seed_rules_catalog`` management command.

Pins the demo catalog seed: the demo shape, then deterministic rules plus the
AI-inference ones, landing as one user's ``ActionType``/``OutreachRule`` rows,
weighted and idempotent, owned by the resolved demo user.
"""

import datetime

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from project.app.actions import services
from project.app.actions.models import ActionJob
from project.app.management.commands.seed_rules_catalog import (
    DEFAULT_OWNER_EMAIL,
    EVENT_COLUMNS,
    LEAD_COLUMNS,
    ROLES,
)
from project.app.models import ActionType, Lead, OutreachRule, Shape

OWNER = "bd@lockedin.example"

SEEDED_ACTION_KEYS = {
    "complete_onboarding",
    "follow_up_after_hold",
    "nudge_usage",
    "power_user_reward",
    "reengage_dormant",
    "set_up_appointment",
}


def _seed(**kwargs):
    call_command("seed_rules_catalog", **kwargs)


def _owner(email=OWNER):
    return get_user_model().objects.get(username=email)


class SeedRulesCatalogTests(TestCase):
    def test_seeding_creates_every_action_and_only_valid_rules(self):
        _seed(owner=OWNER)
        owner = _owner()
        self.assertEqual(
            set(ActionType.objects.filter(owner=owner).values_list("key", flat=True)),
            SEEDED_ACTION_KEYS,
        )
        rules = list(OutreachRule.objects.filter(owner=owner))
        self.assertEqual(len(rules), 8)
        for rule in rules:
            rule.full_clean()

    def test_what_only_a_model_can_read_is_seeded_as_an_inference_rule(self):
        _seed(owner=OWNER)
        inference = OutreachRule.objects.filter(owner=_owner(), kind=OutreachRule.KIND_INFERENCE)
        self.assertEqual(
            {rule.action.key for rule in inference}, {"set_up_appointment", "nudge_usage"}
        )
        self.assertIn("need help", inference.get(action__key="set_up_appointment").inference_prompt)

    def test_the_milestone_rule_gates_its_provider_call_behind_conditions(self):
        _seed(owner=OWNER)
        milestone = OutreachRule.objects.get(
            owner=_owner(), kind=OutreachRule.KIND_INFERENCE, action__key="nudge_usage"
        )
        self.assertIn("deal target", milestone.inference_prompt)
        self.assertTrue(milestone.conditions["conditions"])

    def test_three_weighted_rules_argue_for_a_usage_nudge(self):
        _seed(owner=OWNER)
        nudges = OutreachRule.objects.filter(owner=_owner(), action__key="nudge_usage")
        self.assertEqual(
            sorted(nudges.values_list("weight", flat=True)),
            [OutreachRule.WEIGHT_LOW, OutreachRule.WEIGHT_MEDIUM, OutreachRule.WEIGHT_MEDIUM],
        )

    def test_every_seeded_rule_weighs_between_one_and_three(self):
        _seed(owner=OWNER)
        weights = OutreachRule.objects.filter(owner=_owner()).values_list("weight", flat=True)
        self.assertTrue(all(1 <= weight <= 3 for weight in weights), list(weights))

    def test_reseeding_resets_the_rules_without_duplicating_anything(self):
        _seed(owner=OWNER)
        owner = _owner()
        OutreachRule.objects.filter(owner=owner).delete()
        _seed(owner=OWNER)
        self.assertEqual(ActionType.objects.filter(owner=owner).count(), 6)
        self.assertEqual(OutreachRule.objects.filter(owner=owner).count(), 8)
        _seed(owner=OWNER)
        self.assertEqual(ActionType.objects.filter(owner=owner).count(), 6)
        self.assertEqual(OutreachRule.objects.filter(owner=owner).count(), 8)

    @override_settings(LOGIN_ALLOWED_EMAILS={"ae@lockedin.example"})
    def test_the_owner_defaults_to_the_allowlisted_login_email(self):
        _seed()
        owner = _owner("ae@lockedin.example")
        self.assertEqual(OutreachRule.objects.filter(owner=owner).count(), 8)
        self.assertFalse(owner.has_usable_password())

    @override_settings(LOGIN_ALLOWED_EMAILS=set())
    def test_with_no_allowlist_the_owner_falls_back_to_the_demo_address(self):
        _seed()
        self.assertEqual(ActionType.objects.filter(owner=_owner(DEFAULT_OWNER_EMAIL)).count(), 6)


class LeadOwnershipTests(TestCase):
    """A lead with no owner has no rules, so the seed puts the book in the
    catalog owner's hands."""

    def _lead(self, lead_id):
        return Lead.objects.create(
            id=lead_id,
            data={
                "agency_name": "Summit Risk Advisors",
                "contact_name": "Priya Nair",
                "contact_email": "priya@summitrisk.example.com",
                "contact_phone": "555-0100",
                "state": "CO",
                "num_producers": 4,
                "years_in_business": 9,
                "estimated_book_size_usd": 1_400_000,
                "stage": "active_trial",
            },
        )

    def test_seeding_puts_every_lead_in_the_owners_book(self):
        self._lead("lead_001")
        self._lead("lead_002")

        _seed(owner=OWNER)

        self.assertEqual(_owner().leads.count(), 2)
        self.assertFalse(Lead.objects.filter(owner__isnull=True).exists())

    def test_seeding_for_another_user_moves_the_book_with_the_catalog(self):
        self._lead("lead_001")
        _seed(owner=OWNER)

        _seed(owner="ae@lockedin.example")

        self.assertEqual(_owner().leads.count(), 0)
        self.assertEqual(_owner("ae@lockedin.example").leads.count(), 1)

    def test_deleting_the_owner_leaves_their_leads_in_the_database(self):
        self._lead("lead_001")
        _seed(owner=OWNER)

        _owner().delete()

        self.assertEqual(Lead.objects.count(), 1)
        self.assertIsNone(Lead.objects.get(id="lead_001").owner)


class DemoShapeTests(TestCase):
    def test_seeding_declares_the_demo_shape_before_the_rules(self):
        _seed(owner=OWNER)
        shape = Shape.objects.get(owner=_owner())
        self.assertEqual(shape.lead_columns, LEAD_COLUMNS)
        self.assertEqual(shape.event_columns, EVENT_COLUMNS)
        self.assertEqual(shape.roles, ROLES)

    def test_the_seeded_shape_is_one_the_model_accepts(self):
        _seed(owner=OWNER)
        Shape.objects.get(owner=_owner()).full_clean()

    def test_the_lead_writes_exactly_one_of_the_demo_columns(self):
        _seed(owner=OWNER)
        shape = Shape.objects.get(owner=_owner())
        self.assertEqual([c["name"] for c in shape.authored()], ["hubspot_notes"])

    def test_reseeding_re_declares_rather_than_adding_a_second_shape(self):
        _seed(owner=OWNER)
        Shape.objects.filter(owner=_owner()).update(event_columns=[])
        _seed(owner=OWNER)
        self.assertEqual(Shape.objects.filter(owner=_owner()).count(), 1)
        self.assertEqual(Shape.objects.get(owner=_owner()).event_columns, EVENT_COLUMNS)


# What the engine decides for `raw_data/` on a frozen date, so ingest, the
# seeded shape and the rules stay in step: change any one of them and this
# says which lead moved.
TODAY = datetime.date(2026, 6, 12)

RAW_DATA_DECISIONS = {
    "lead_001": "power_user_reward",
    "lead_002": "follow_up_after_hold",
    "lead_003": "complete_onboarding",
    "lead_004": "nudge_usage",
    "lead_005": None,
    "lead_006": "reengage_dormant",
    "lead_007": None,
    "lead_008": None,
    "lead_009": None,
    "lead_010": None,
    "lead_011": "nudge_usage",
    "lead_012": "follow_up_after_hold",
}


class RawDataEndToEndTests(TestCase):
    """Ingest, seed, then one engine run over the committed raw files."""

    @classmethod
    def setUpTestData(cls):
        call_command("ingest_data", verbosity=0)
        _seed(owner=OWNER)
        for job in services.enqueue_pending_leads(today=TODAY):
            if services.claim(job):
                services.run_job(job, today=TODAY)

    def _decisions(self):
        return {
            job.lead_id: (job.selected_action.key if job.selected_action else None)
            for job in ActionJob.objects.select_related("selected_action")
        }

    def test_the_run_reaches_a_verdict_for_every_ingested_lead(self):
        self.assertEqual(ActionJob.objects.count(), Lead.objects.count())
        self.assertFalse(ActionJob.objects.exclude(status__in=ActionJob.DECIDED_STATUSES).exists())

    def test_the_engine_chooses_the_same_action_for_every_raw_lead(self):
        self.assertEqual(self._decisions(), RAW_DATA_DECISIONS)

    def test_no_deterministic_rule_is_unevaluable_against_the_seeded_shape(self):
        for job in ActionJob.objects.all():
            with self.subTest(job.lead_id):
                self.assertEqual(job.decision["deterministic"].get("unevaluable_rule_ids", []), [])
