"""Rules-entity business logic: owner-scoped reads, validated writes, and the
weight tally that turns matched rules into the action to propose."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from project.app.models import ActionType, OutreachRule
from project.app.rules import services
from project.app.rules.utils import _all_of, _cond


class RulesServiceTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(username="planner@lockedin.example")
        self.other = get_user_model().objects.create_user(username="teammate@lockedin.example")

    def _action(self, key, owner=None):
        return ActionType.objects.create(owner=owner or self.user, key=key, label=key)

    def _rule(self, action, name, weight, **kwargs):
        kwargs.setdefault("owner", action.owner)
        kwargs.setdefault("kind", OutreachRule.KIND_DETERMINISTIC)
        kwargs.setdefault("conditions", _all_of(_cond("deals_closed", ">", 20)))
        return OutreachRule.objects.create(action=action, name=name, weight=weight, **kwargs)


class SelectActionTests(RulesServiceTestCase):
    def test_no_rule_firing_selects_nothing(self):
        self.assertIsNone(services.select_action([]))

    def test_a_single_firing_rule_selects_its_own_action(self):
        rule = self._rule(self._action("nudge_usage"), "created quotes, never submitted", 2)
        selected = services.select_action([rule])
        self.assertEqual(selected.action.key, "nudge_usage")
        self.assertEqual(selected.weight, 2)
        self.assertEqual(selected.reasons, ["created quotes, never submitted"])

    def test_one_weak_rule_on_its_own_proposes_nothing(self):
        rule = self._rule(self._action("nudge_usage"), "modest momentum", 1)
        self.assertIsNone(services.select_action([rule]))
        # The tally is still reported — it just does not clear the floor.
        [score] = services.score_actions([rule])
        self.assertEqual(score.weight, 1)
        self.assertFalse(score.actionable)

    def test_two_weak_rules_agreeing_clear_the_floor(self):
        nudge = self._action("nudge_usage")
        selected = services.select_action(
            [
                self._rule(nudge, "modest momentum", 1),
                self._rule(nudge, "no deal closed in a few days", 1),
            ]
        )
        self.assertEqual(selected.action.key, "nudge_usage")
        self.assertEqual(selected.weight, 2)

    def test_a_weak_rule_cannot_win_by_being_the_only_one_that_fired(self):
        weak = self._rule(self._action("nudge_usage"), "modest momentum", 1)
        strong = self._rule(self._action("reengage_dormant"), "dormant for weeks", 3)
        self.assertIsNone(services.select_action([weak]))
        self.assertEqual(services.select_action([weak, strong]).action.key, "reengage_dormant")

    def test_the_heavier_rule_wins_when_two_actions_compete(self):
        dormant = self._rule(self._action("reengage_dormant"), "dormant for weeks", 3)
        nudge = self._rule(self._action("nudge_usage"), "no deal closed lately", 1)
        self.assertEqual(services.select_action([nudge, dormant]).action.key, "reengage_dormant")

    def test_two_agreeing_rules_outweigh_a_single_heavier_rival(self):
        nudge = self._action("nudge_usage")
        dormancy = self._rule(nudge, "dormant for a few days", 2)
        no_deals = self._rule(nudge, "no deal closed in a few days", 1)
        rival = self._rule(self._action("power_user_reward"), "power user", 2)

        selected = services.select_action([dormancy, no_deals, rival])
        self.assertEqual(selected.action.key, "nudge_usage")
        self.assertEqual(selected.weight, 3)
        # Heaviest contributor first, so the reviewer reads the strongest reason first.
        self.assertEqual(
            selected.reasons, ["dormant for a few days", "no deal closed in a few days"]
        )

    def test_an_equal_tally_breaks_on_the_action_key(self):
        first = self._rule(self._action("aaa_first_alphabetically"), "a", 2)
        second = self._rule(self._action("zzz_last_alphabetically"), "z", 2)
        self.assertEqual(
            services.select_action([second, first]).action.key, "aaa_first_alphabetically"
        )

    def test_scoring_ranks_every_action_that_had_a_rule_fire(self):
        nudge = self._action("nudge_usage")
        scores = services.score_actions(
            [
                self._rule(nudge, "dormant", 2),
                self._rule(nudge, "no deals", 1),
                self._rule(self._action("power_user_reward"), "power user", 2),
            ]
        )
        self.assertEqual(
            [(score.action.key, score.weight) for score in scores],
            [("nudge_usage", 3), ("power_user_reward", 2)],
        )


class OwnerScopedReadTests(RulesServiceTestCase):
    def test_reads_never_cross_between_owners(self):
        mine = self._rule(self._action("nudge_usage"), "mine", 2)
        theirs = self._rule(self._action("nudge_usage", owner=self.other), "theirs", 2)

        self.assertEqual(list(services.rules_for(self.user)), [mine])
        self.assertIsNone(services.rule_for(self.user, theirs.pk))
        self.assertIsNone(services.action_for(self.user, theirs.action_id))
        self.assertEqual(services.rule_for(self.user, mine.pk), mine)

    def test_the_planner_reads_only_enabled_rules_on_enabled_actions(self):
        live_action = self._action("nudge_usage")
        live = self._rule(live_action, "live", 2)
        self._rule(live_action, "switched off", 2, enabled=False)
        self._rule(self._action("retired_action", owner=self.user), "action retired", 2)
        ActionType.objects.filter(key="retired_action").update(enabled=False)

        self.assertEqual(list(services.enabled_rules_for(self.user)), [live])


class ValidatedWriteTests(RulesServiceTestCase):
    def test_creating_a_rule_binds_the_owner_and_validates_the_payload(self):
        action = self._action("nudge_usage")
        rule = services.create_rule(
            self.user,
            {
                "action": action,
                "name": "Nudge them",
                "kind": OutreachRule.KIND_DETERMINISTIC,
                "conditions": _all_of(_cond("deals_closed", ">", 20)),
                "weight": 2,
            },
        )
        self.assertEqual(rule.owner, self.user)

        with self.assertRaises(ValidationError) as ctx:
            services.create_rule(
                self.user,
                {
                    "action": action,
                    "name": "No payload",
                    "kind": OutreachRule.KIND_DETERMINISTIC,
                    "conditions": {},
                },
            )
        self.assertIn("conditions", ctx.exception.message_dict)

    def test_updating_a_rule_runs_the_models_validation_too(self):
        rule = self._rule(self._action("nudge_usage"), "Nudge them", 2)
        self.assertEqual(services.update_rule(rule, {"weight": 3}).weight, 3)
        with self.assertRaises(ValidationError):
            services.update_rule(rule, {"weight": 7})

    def test_deleting_an_action_is_refused_while_a_rule_selects_it(self):
        rule = self._rule(self._action("nudge_usage"), "Nudge them", 2)
        with self.assertRaises(services.ActionInUse):
            services.delete_action(rule.action)
        services.delete_rule(rule)
        services.delete_action(ActionType.objects.get(key="nudge_usage"))
        self.assertFalse(ActionType.objects.filter(key="nudge_usage").exists())
