"""The rules-catalog API: CRUD over the signed-in user's actions and rules.

Pins owner scoping (foreign rows read as 404, owner bound server-side),
model-backed validation surfacing as 400s, and the 409 on deleting an action
that rules still select.
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase

from project.app.models import ActionType, OutreachRule

ACTIONS_URL = "/api/rules/actions/"
RULES_URL = "/api/rules/"


def _conditions():
    return {
        "version": OutreachRule.CONDITIONS_SCHEMA_VERSION,
        "operator": "all_of",
        "conditions": [
            {"field": "deals_closed", "operator": ">", "threshold": 20, "source": "lead"}
        ],
    }


class RulesApiTestCase(TestCase):
    """DRF keeps throttle history in the default cache, which outlives a test."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.user = get_user_model().objects.create_user(username="planner@lockedin.example")
        self.other = get_user_model().objects.create_user(username="teammate@lockedin.example")
        self.client.force_login(self.user)

    def _action(self, owner=None, key="reward_power_user"):
        return ActionType.objects.create(
            owner=owner or self.user, key=key, label="Reward power user"
        )

    def _rule(self, action, **kwargs):
        kwargs.setdefault("owner", action.owner)
        kwargs.setdefault("name", "Reward power users")
        kwargs.setdefault("kind", OutreachRule.KIND_DETERMINISTIC)
        kwargs.setdefault("conditions", _conditions())
        return OutreachRule.objects.create(action=action, **kwargs)


class RulesApiAuthTests(RulesApiTestCase):
    def test_the_catalog_requires_a_signed_in_session(self):
        self.client.logout()
        response = self.client.get(RULES_URL)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "not_authenticated")


class ActionTypeApiTests(RulesApiTestCase):
    def test_creating_and_listing_actions_is_scoped_to_the_signed_in_user(self):
        self._action(owner=self.other, key="their_action")
        response = self.client.post(
            ACTIONS_URL,
            {"key": "set_up_appointment", "label": "Set up an appointment", "urgency": "high"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        created = response.json()
        self.assertEqual(created["key"], "set_up_appointment")
        self.assertEqual(ActionType.objects.get(pk=created["id"]).owner, self.user)
        listed = self.client.get(ACTIONS_URL).json()
        self.assertEqual([row["key"] for row in listed], ["set_up_appointment"])

    def test_a_non_snake_case_key_is_a_validation_error(self):
        response = self.client.post(
            ACTIONS_URL,
            {"key": "Set-Up-Appointment", "label": "Bad key"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "validation_error")

    def test_a_duplicate_key_for_the_same_user_is_a_validation_error(self):
        self._action()
        response = self.client.post(
            ACTIONS_URL,
            {"key": "reward_power_user", "label": "Duplicate"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_updating_and_deleting_someone_elses_action_reads_as_not_found(self):
        theirs = self._action(owner=self.other)
        patched = self.client.patch(
            f"{ACTIONS_URL}{theirs.pk}/", {"label": "Mine now"}, content_type="application/json"
        )
        self.assertEqual(patched.status_code, 404)
        deleted = self.client.delete(f"{ACTIONS_URL}{theirs.pk}/")
        self.assertEqual(deleted.status_code, 404)

    def test_deleting_an_action_that_rules_still_select_is_a_409(self):
        action = self._action()
        rule = self._rule(action)
        response = self.client.delete(f"{ACTIONS_URL}{action.pk}/")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "action_in_use")
        rule.delete()
        response = self.client.delete(f"{ACTIONS_URL}{action.pk}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(ActionType.objects.filter(pk=action.pk).exists())


class OutreachRuleApiTests(RulesApiTestCase):
    def test_creating_updating_and_deleting_a_rule_round_trips(self):
        action = self._action()
        created = self.client.post(
            RULES_URL,
            {
                "action": action.pk,
                "name": "Reward power users",
                "kind": "deterministic",
                "conditions": _conditions(),
                "weight": 3,
            },
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 201)
        rule_id = created.json()["id"]
        self.assertEqual(OutreachRule.objects.get(pk=rule_id).owner, self.user)

        patched = self.client.patch(
            f"{RULES_URL}{rule_id}/", {"weight": 1}, content_type="application/json"
        )
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.json()["weight"], 1)

        deleted = self.client.delete(f"{RULES_URL}{rule_id}/")
        self.assertEqual(deleted.status_code, 204)
        self.assertFalse(OutreachRule.objects.filter(pk=rule_id).exists())

    def test_an_inference_rule_carrying_conditions_is_a_validation_error(self):
        action = self._action()
        response = self.client.post(
            RULES_URL,
            {
                "action": action.pk,
                "name": "Needs help",
                "kind": "inference",
                "inference_prompt": "the notes say they need help",
                "conditions": _conditions(),
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "validation_error")

    def test_someone_elses_action_id_reads_as_nonexistent_on_create(self):
        theirs = self._action(owner=self.other)
        response = self.client.post(
            RULES_URL,
            {
                "action": theirs.pk,
                "name": "Reaching across accounts",
                "kind": "deterministic",
                "conditions": _conditions(),
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(OutreachRule.objects.count(), 0)

    def test_an_owner_in_the_payload_is_ignored(self):
        action = self._action()
        response = self.client.post(
            RULES_URL,
            {
                "action": action.pk,
                "owner": self.other.pk,
                "name": "Still mine",
                "kind": "deterministic",
                "conditions": _conditions(),
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(OutreachRule.objects.get(pk=response.json()["id"]).owner, self.user)

    def test_someone_elses_rule_is_invisible_to_list_and_detail(self):
        theirs = self._rule(self._action(owner=self.other))
        self.assertEqual(self.client.get(RULES_URL).json(), [])
        self.assertEqual(self.client.get(f"{RULES_URL}{theirs.pk}/").status_code, 404)
        self.assertEqual(
            self.client.patch(
                f"{RULES_URL}{theirs.pk}/", {"weight": 1}, content_type="application/json"
            ).status_code,
            404,
        )

    def test_the_rules_list_comes_back_heaviest_first(self):
        action = self._action()
        light = self._rule(action, name="modest momentum", weight=1)
        heavy = self._rule(action, name="dormant account", weight=3)
        listed = self.client.get(RULES_URL).json()
        self.assertEqual([row["id"] for row in listed], [heavy.pk, light.pk])

    def test_a_weight_outside_one_to_three_is_a_validation_error(self):
        action = self._action()
        response = self.client.post(
            RULES_URL,
            {
                "action": action.pk,
                "name": "Off the scale",
                "kind": "deterministic",
                "conditions": _conditions(),
                "weight": 9,
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "validation_error")
