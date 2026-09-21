"""The ``seed_rules_catalog`` management command.

Pins the demo catalog seed: deterministic rules plus the AI-inference ones
land as one user's ``ActionType``/``OutreachRule`` rows, weighted and
idempotent, owned by the resolved demo user.
"""

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from project.app.management.commands.seed_rules_catalog import DEFAULT_OWNER_EMAIL
from project.app.models import ActionType, Lead, OutreachRule

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
            agency_name="Summit Risk Advisors",
            contact_name="Priya Nair",
            contact_email="priya@summitrisk.example.com",
            contact_phone="555-0100",
            state="CO",
            num_producers=4,
            years_in_business=9,
            estimated_book_size_usd=1_400_000,
            stage="active_trial",
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
