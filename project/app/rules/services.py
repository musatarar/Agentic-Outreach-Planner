"""Business logic for the rules entity.

Two jobs: owner-scoped reads and the single validated write path for actions
and rules (every write runs ``full_clean()``, so the model's pairing,
ownership and uniqueness rules hold whatever calls in), and the weight tally
that turns the rules a lead matched into the action to propose.

Django-only on purpose — no DRF here; the HTTP layer translates these
exceptions.
"""

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import RestrictedError

from project.app.rules.models import ActionType, OutreachRule


class ActionInUse(Exception):
    """An action cannot be deleted while rules still select it."""


# A tally below this proposes nothing: one weak rule firing is a hint, not a
# case for spending a provider call and a reviewer's attention on outreach.
# Two weak rules agreeing do clear it.
MIN_ACTIONABLE_WEIGHT = 2


# --------------------------------------------------------------------------
# reads — every queryset is scoped to one owner
# --------------------------------------------------------------------------


def actions_for(owner):
    return ActionType.objects.filter(owner=owner)


def rules_for_owners(owners):
    """Several owners' rules, heaviest first, with actions joined.

    Callers tally ``rule.action``; without the join that is one query per rule.
    """
    return OutreachRule.objects.filter(owner__in=owners).select_related("action")


def rules_for(owner):
    """One owner's rules, heaviest first, with actions joined."""
    return rules_for_owners([owner])


def enabled_rules_for_owners(owners):
    """What an engine evaluates: enabled rules selecting enabled actions."""
    return rules_for_owners(owners).filter(enabled=True, action__enabled=True)


def enabled_rules_for(owner):
    """What the planner evaluates: enabled rules selecting enabled actions."""
    return rules_for(owner).filter(enabled=True, action__enabled=True)


def action_for(owner, pk):
    """One owned action, or ``None`` — someone else's id is indistinguishable
    from a missing one, so callers cannot probe another user's catalog."""
    return actions_for(owner).filter(pk=pk).first()


def rule_for(owner, pk):
    return rules_for(owner).filter(pk=pk).first()


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------


def _save(instance, fields):
    """Apply ``fields`` and save through ``full_clean()``.

    Raises ``django.core.exceptions.ValidationError`` — the model's own
    verdict, not a re-derived one.
    """
    for field, value in fields.items():
        setattr(instance, field, value)
    instance.full_clean()
    try:
        instance.save()
    except IntegrityError as exc:
        # full_clean checks uniqueness and the check constraints with SELECTs,
        # so a concurrent writer can still win the race and leave the database
        # to refuse this INSERT. That refusal is an answer about the data, not
        # a server fault, so it reads as one.
        raise ValidationError(
            "That change collided with a concurrent write; re-read the catalog and retry."
        ) from exc
    return instance


def create_action(owner, fields):
    return _save(ActionType(owner=owner), fields)


def update_action(action, fields):
    return _save(action, fields)


def delete_action(action):
    """Delete an owned action, refusing while rules still select it."""
    try:
        action.delete()
    except RestrictedError as exc:
        raise ActionInUse(action.key) from exc


def create_rule(owner, fields):
    return _save(OutreachRule(owner=owner), fields)


def update_rule(rule, fields):
    return _save(rule, fields)


def delete_rule(rule):
    rule.delete()


# --------------------------------------------------------------------------
# selection — matched rules in, one action out
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionScore:
    """One action's case: the rules that fired for it and their total weight."""

    action: ActionType
    weight: int
    rules: tuple

    @property
    def actionable(self):
        """Whether this case is strong enough to propose on its own."""
        return self.weight >= MIN_ACTIONABLE_WEIGHT

    @property
    def reasons(self):
        """The firing rules' names, heaviest first — what a reviewer reads."""
        return [rule.name for rule in self.rules]


def score_actions(matched_rules):
    """Tally matched rules onto their actions, strongest case first.

    Ties break on the action key so two equally-argued actions resolve the
    same way on every backend and every run.
    """
    by_action = {}
    for rule in matched_rules:
        score = by_action.setdefault(rule.action_id, {"action": rule.action, "rules": []})
        score["rules"].append(rule)
    scores = [
        ActionScore(
            action=score["action"],
            weight=sum(rule.weight for rule in score["rules"]),
            rules=tuple(sorted(score["rules"], key=lambda rule: (-rule.weight, rule.pk))),
        )
        for score in by_action.values()
    ]
    scores.sort(key=lambda score: (-score.weight, score.action.key))
    return scores


def select_action(matched_rules):
    """The single action to propose, or ``None`` when nothing argues hard
    enough — no rule fired, or the best tally is under
    :data:`MIN_ACTIONABLE_WEIGHT`. Either way the caller routes the lead to a
    human rather than proposing outreach."""
    scores = score_actions(matched_rules)
    if not scores or scores[0].weight < MIN_ACTIONABLE_WEIGHT:
        return None
    return scores[0]
