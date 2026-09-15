"""User-defined outreach catalog: ``ActionType`` and ``OutreachRule``.

Pins the rules-catalog schema (migration 0010_user_rules_catalog): per-owner
action keys, the deterministic/inference kind <-> payload pairing, first-match
evaluation order, and the delete story (RESTRICT on the action FK, clean sweep
on owner delete).
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import RestrictedError
from django.test import TestCase

from project.app.models import ActionType, OutreachRule


def _user(username="planner@lockedin.example"):
    return get_user_model().objects.create_user(username=username)


def _action(owner, key="reward_power_user", **kwargs):
    kwargs.setdefault("label", "Reward power user (volume pricing)")
    return ActionType.objects.create(owner=owner, key=key, **kwargs)


def _deterministic_conditions(field="deals_closed", operator=">", threshold=20):
    """The brief's worked example: ``deals_closed > 20 -> reward_power_user``."""
    return {
        "version": OutreachRule.CONDITIONS_SCHEMA_VERSION,
        "operator": "all_of",
        "conditions": [
            {"field": field, "operator": operator, "threshold": threshold, "source": "lead"}
        ],
    }


class ActionTypeCatalogTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = _user()
        cls.other = _user("teammate@lockedin.example")

    def test_two_users_can_each_define_the_same_action_key(self):
        _action(self.user)
        _action(self.other)
        self.assertEqual(ActionType.objects.filter(key="reward_power_user").count(), 2)

    def test_a_duplicate_action_key_for_the_same_user_is_rejected_by_the_db(self):
        _action(self.user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            _action(self.user, label="Duplicate key, different label")

    def test_an_action_key_must_be_snake_case(self):
        action = ActionType(owner=self.user, key="Reward-Power-User", label="Bad key")
        with self.assertRaises(ValidationError) as ctx:
            action.full_clean()
        self.assertIn("key", ctx.exception.message_dict)

    def test_an_overlong_description_fails_validation(self):
        action = _action(self.user)
        action.description = "x" * (ActionType.DESCRIPTION_MAX_CHARS + 1)
        with self.assertRaises(ValidationError) as ctx:
            action.full_clean()
        self.assertIn("description", ctx.exception.message_dict)


class OutreachRuleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = _user()
        cls.action = _action(cls.user)

    def _rule(self, **kwargs):
        kwargs.setdefault("owner", self.user)
        kwargs.setdefault("action", self.action)
        kwargs.setdefault("name", "Reward power users")
        kwargs.setdefault("kind", OutreachRule.KIND_DETERMINISTIC)
        kwargs.setdefault("conditions", _deterministic_conditions())
        return OutreachRule.objects.create(**kwargs)

    def test_the_two_example_rules_from_the_brief_round_trip(self):
        deterministic = self._rule()
        appointment = _action(self.user, key="set_up_appointment", label="Set up an appointment")
        inference = self._rule(
            action=appointment,
            name="Offer help when they ask for it",
            kind=OutreachRule.KIND_INFERENCE,
            conditions={},
            inference_prompt=(
                "The hubspot notes show they need help with something: set up "
                "an appointment for us."
            ),
        )
        deterministic.full_clean()
        inference.full_clean()
        deterministic.refresh_from_db()
        self.assertEqual(deterministic.conditions, _deterministic_conditions())
        self.assertEqual(deterministic.action.key, "reward_power_user")
        inference.refresh_from_db()
        self.assertEqual(inference.action.key, "set_up_appointment")
        self.assertIn("need help", inference.inference_prompt)

    def test_rules_evaluate_in_order_then_id(self):
        second = self._rule(name="second", order=5)
        first = self._rule(name="first", order=0)
        first_tie_break = self._rule(name="also order five, created later", order=5)
        self.assertEqual(list(OutreachRule.objects.all()), [first, second, first_tie_break])

    def test_a_deterministic_rule_needs_conditions_and_no_inference_prompt(self):
        empty = OutreachRule(
            owner=self.user,
            action=self.action,
            name="no payload",
            kind=OutreachRule.KIND_DETERMINISTIC,
            conditions={},
        )
        with self.assertRaises(ValidationError) as ctx:
            empty.full_clean()
        self.assertIn("conditions", ctx.exception.message_dict)

        both = OutreachRule(
            owner=self.user,
            action=self.action,
            name="both payloads",
            kind=OutreachRule.KIND_DETERMINISTIC,
            conditions=_deterministic_conditions(),
            inference_prompt="also an inference?",
        )
        with self.assertRaises(ValidationError) as ctx:
            both.full_clean()
        self.assertIn("inference_prompt", ctx.exception.message_dict)

    def test_an_inference_rule_needs_its_predicate_and_no_conditions(self):
        blank = OutreachRule(
            owner=self.user,
            action=self.action,
            name="no predicate",
            kind=OutreachRule.KIND_INFERENCE,
            inference_prompt="   ",
        )
        with self.assertRaises(ValidationError) as ctx:
            blank.full_clean()
        self.assertIn("inference_prompt", ctx.exception.message_dict)

        both = OutreachRule(
            owner=self.user,
            action=self.action,
            name="both payloads",
            kind=OutreachRule.KIND_INFERENCE,
            inference_prompt="notes show they need help",
            conditions=_deterministic_conditions(),
        )
        with self.assertRaises(ValidationError) as ctx:
            both.full_clean()
        self.assertIn("conditions", ctx.exception.message_dict)

    def test_an_unknown_rule_kind_is_rejected_by_the_db(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._rule(name="mystery", kind="vibes")

    def test_an_overlong_inference_prompt_fails_validation(self):
        rule = OutreachRule(
            owner=self.user,
            action=self.action,
            name="too long",
            kind=OutreachRule.KIND_INFERENCE,
            inference_prompt="x" * (OutreachRule.INFERENCE_PROMPT_MAX_CHARS + 1),
        )
        with self.assertRaises(ValidationError) as ctx:
            rule.full_clean()
        self.assertIn("inference_prompt", ctx.exception.message_dict)

    def test_a_rule_cannot_select_another_users_action_type(self):
        other = _user("teammate@lockedin.example")
        their_action = _action(other)
        rule = OutreachRule(
            owner=self.user,
            action=their_action,
            name="reaching across accounts",
            kind=OutreachRule.KIND_DETERMINISTIC,
            conditions=_deterministic_conditions(),
        )
        with self.assertRaises(ValidationError) as ctx:
            rule.full_clean()
        self.assertIn("action", ctx.exception.message_dict)

    def test_deleting_an_action_type_still_selected_by_a_rule_is_refused(self):
        self._rule()
        with self.assertRaises(RestrictedError):
            self.action.delete()

    def test_deleting_a_user_sweeps_their_actions_and_rules_together(self):
        user = _user("leaver@lockedin.example")
        action = _action(user)
        OutreachRule.objects.create(
            owner=user,
            action=action,
            name="goes with its owner",
            kind=OutreachRule.KIND_DETERMINISTIC,
            conditions=_deterministic_conditions(),
        )
        # PROTECT must not wedge the owner cascade: the rule falls with the user.
        user.delete()
        self.assertFalse(ActionType.objects.filter(owner_id=action.owner_id).exists())
        self.assertFalse(OutreachRule.objects.filter(name="goes with its owner").exists())
