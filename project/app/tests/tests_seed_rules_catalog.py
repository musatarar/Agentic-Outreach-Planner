"""The ``seed_rules_catalog`` management command.

Pins the demo catalog seed: the compiled planner rules land as one user's
``ActionType``/``OutreachRule`` rows, idempotently, owned by the resolved
demo user.
"""

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from project.app.management.commands.seed_rules_catalog import DEFAULT_OWNER_EMAIL
from project.app.models import ActionType, OutreachRule
from project.app.services import actions

OWNER = "bd@lockedin.example"


def _seed(**kwargs):
    call_command("seed_rules_catalog", **kwargs)


class SeedRulesCatalogTests(TestCase):
    def test_seeding_creates_every_selectable_action_and_only_valid_rules(self):
        _seed(owner=OWNER)
        owner = get_user_model().objects.get(username=OWNER)
        self.assertEqual(
            set(ActionType.objects.filter(owner=owner).values_list("key", flat=True)),
            set(actions.SELECTABLE_ACTION_TYPES),
        )
        rules = list(OutreachRule.objects.filter(owner=owner))
        self.assertEqual(len(rules), 7)
        for rule in rules:
            rule.full_clean()
            self.assertEqual(rule.kind, OutreachRule.KIND_DETERMINISTIC)
            self.assertEqual(rule.conditions["version"], OutreachRule.CONDITIONS_SCHEMA_VERSION)

    def test_the_seeded_rules_keep_the_compiled_first_match_order(self):
        _seed(owner=OWNER)
        owner = get_user_model().objects.get(username=OWNER)
        first_to_last = [
            rule.action.key
            for rule in OutreachRule.objects.filter(owner=owner).select_related("action")
        ]
        self.assertEqual(
            first_to_last,
            [
                actions.COMPLETE_ONBOARDING,
                actions.POWER_USER_REWARD,
                actions.FOLLOW_UP_AFTER_HOLD,
                actions.REENGAGE_DORMANT,
                actions.NUDGE_USAGE,
                actions.NUDGE_USAGE,
                actions.NUDGE_USAGE,
            ],
        )

    def test_reseeding_resets_the_rules_without_duplicating_anything(self):
        _seed(owner=OWNER)
        owner = get_user_model().objects.get(username=OWNER)
        OutreachRule.objects.filter(owner=owner).delete()
        _seed(owner=OWNER)
        self.assertEqual(ActionType.objects.filter(owner=owner).count(), 5)
        self.assertEqual(OutreachRule.objects.filter(owner=owner).count(), 7)
        _seed(owner=OWNER)
        self.assertEqual(ActionType.objects.filter(owner=owner).count(), 5)
        self.assertEqual(OutreachRule.objects.filter(owner=owner).count(), 7)

    @override_settings(LOGIN_ALLOWED_EMAILS={"ae@lockedin.example"})
    def test_the_owner_defaults_to_the_allowlisted_login_email(self):
        _seed()
        owner = get_user_model().objects.get(username="ae@lockedin.example")
        self.assertEqual(OutreachRule.objects.filter(owner=owner).count(), 7)
        self.assertFalse(owner.has_usable_password())

    @override_settings(LOGIN_ALLOWED_EMAILS=set())
    def test_with_no_allowlist_the_owner_falls_back_to_the_demo_address(self):
        _seed()
        owner = get_user_model().objects.get(username=DEFAULT_OWNER_EMAIL)
        self.assertEqual(ActionType.objects.filter(owner=owner).count(), 5)
