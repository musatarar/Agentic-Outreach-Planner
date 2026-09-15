"""The ``seed_rules_catalog`` management command and its backing service.

Pins the demo catalog seed: the compiled planner rules plus one AI-inference
rule land as one user's ``ActionType``/``OutreachRule`` rows, idempotently,
owned by the resolved demo user.
"""

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from project.app.models import ActionType, OutreachRule
from project.app.rules.services import DEFAULT_OWNER_EMAIL

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


class SeedRulesCatalogTests(TestCase):
    def test_seeding_creates_every_action_and_only_valid_rules(self):
        _seed(owner=OWNER)
        owner = get_user_model().objects.get(username=OWNER)
        self.assertEqual(
            set(ActionType.objects.filter(owner=owner).values_list("key", flat=True)),
            SEEDED_ACTION_KEYS,
        )
        rules = list(OutreachRule.objects.filter(owner=owner))
        self.assertEqual(len(rules), 8)
        for rule in rules:
            rule.full_clean()

    def test_the_seeded_rules_keep_first_match_order_with_the_inference_rule_last(self):
        _seed(owner=OWNER)
        owner = get_user_model().objects.get(username=OWNER)
        rules = list(OutreachRule.objects.filter(owner=owner).select_related("action"))
        self.assertEqual(
            [rule.action.key for rule in rules],
            [
                "complete_onboarding",
                "power_user_reward",
                "follow_up_after_hold",
                "reengage_dormant",
                "nudge_usage",
                "nudge_usage",
                "nudge_usage",
                "set_up_appointment",
            ],
        )
        deterministic, inference = rules[:-1], rules[-1]
        self.assertEqual({rule.kind for rule in deterministic}, {OutreachRule.KIND_DETERMINISTIC})
        self.assertEqual(inference.kind, OutreachRule.KIND_INFERENCE)
        self.assertIn("need help", inference.inference_prompt)

    def test_reseeding_resets_the_rules_without_duplicating_anything(self):
        _seed(owner=OWNER)
        owner = get_user_model().objects.get(username=OWNER)
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
        owner = get_user_model().objects.get(username="ae@lockedin.example")
        self.assertEqual(OutreachRule.objects.filter(owner=owner).count(), 8)
        self.assertFalse(owner.has_usable_password())

    @override_settings(LOGIN_ALLOWED_EMAILS=set())
    def test_with_no_allowlist_the_owner_falls_back_to_the_demo_address(self):
        _seed()
        owner = get_user_model().objects.get(username=DEFAULT_OWNER_EMAIL)
        self.assertEqual(ActionType.objects.filter(owner=owner).count(), 6)
