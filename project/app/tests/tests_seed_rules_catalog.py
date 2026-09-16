"""The ``seed_rules_catalog`` management command.

Pins the demo catalog seed: the planner's compiled rules plus one AI-inference
rule land as one user's ``ActionType``/``OutreachRule`` rows, weighted and
idempotent, owned by the resolved demo user.
"""

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from project.app.management.commands.seed_rules_catalog import DEFAULT_OWNER_EMAIL
from project.app.models import ActionType, OutreachRule

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

    def test_the_seed_includes_one_inference_rule_for_the_appointment_action(self):
        _seed(owner=OWNER)
        inference = OutreachRule.objects.filter(
            owner=_owner(), kind=OutreachRule.KIND_INFERENCE
        ).get()
        self.assertEqual(inference.action.key, "set_up_appointment")
        self.assertIn("need help", inference.inference_prompt)

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
