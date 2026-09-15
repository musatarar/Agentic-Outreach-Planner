"""Rules-catalog services: seed one user's actions and rules (idempotent).

The seeded set reproduces the planner's compiled behavior as editable data —
the three "active but underusing" branches flatten into three consecutive
rules, and first-match-wins keeps the semantics — plus one AI-inference rule.
Re-running resets the owner's rules to this set.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction

from project.app.rules.models import ActionType, OutreachRule
from project.app.rules.utils import _all_of, _cond

# Used when no owner is given and LOGIN_ALLOWED_EMAILS is empty.
DEFAULT_OWNER_EMAIL = "demo@lockedin.example"

ACTIONS = [
    {
        "key": "power_user_reward",
        "label": "Reward power user (volume pricing)",
        "urgency": "medium",
    },
    {
        "key": "follow_up_after_hold",
        "label": "Follow up — hold period has passed",
        "urgency": "high",
    },
    {
        "key": "reengage_dormant",
        "label": "Re-engage dormant account",
        "urgency": "high",
    },
    {
        "key": "nudge_usage",
        "label": "Nudge usage / encourage next step",
        "urgency": "medium",
    },
    {
        "key": "complete_onboarding",
        "label": "Complete onboarding (demo done, never signed up)",
        "urgency": "high",
    },
    {
        "key": "set_up_appointment",
        "label": "Set up an appointment",
        "urgency": "high",
    },
]

# In evaluation order. "conditions" makes a deterministic rule, "inference" an
# AI-inference rule; derived fields name the engine's computed predicates.
RULES = [
    {
        "name": "Demo completed but never signed up",
        "action": "complete_onboarding",
        "conditions": _all_of(
            _cond("stage", "==", "demo_completed"),
            _cond("signed_up_date", "absent"),
        ),
    },
    {
        "name": "Power user near a reward / volume-pricing milestone",
        "action": "power_user_reward",
        "conditions": _all_of(
            _cond("deals_closed", ">=", 5),
            _cond("quotes_submitted", ">=", 10),
        ),
    },
    {
        "name": "Hold period has passed and the lead went quiet",
        "action": "follow_up_after_hold",
        "conditions": _all_of(
            _cond("hubspot_notes", "contains", "HOLD_PHRASES", source="notes"),
            _cond("gone_quiet", "==", True, source="derived"),
        ),
    },
    {
        "name": "Signed up but stopped using the portal",
        "action": "reengage_dormant",
        "conditions": _all_of(
            _cond("signed_up_date", "exists"),
            _cond("days_since_last_login", ">", 21, source="derived"),
        ),
    },
    {
        "name": "Active but underusing — created quotes, never submitted one",
        "action": "nudge_usage",
        "conditions": _all_of(
            _cond("days_since_last_login", "<=", 21, source="derived"),
            _cond("quotes_created", ">", 0),
            _cond("quotes_submitted", "==", 0),
        ),
    },
    {
        "name": "Active but underusing — short of the milestone in the notes",
        "action": "nudge_usage",
        "conditions": _all_of(
            _cond("days_since_last_login", "<=", 21, source="derived"),
            _cond("deals_closed", ">", 0),
            _cond("milestone_from_notes", "exists", source="derived"),
            _cond("deals_below_milestone", "==", True, source="derived"),
        ),
    },
    {
        "name": "Active but underusing — modest deal momentum",
        "action": "nudge_usage",
        "conditions": _all_of(
            _cond("days_since_last_login", "<=", 21, source="derived"),
            _cond("deals_closed", ">", 0),
            _cond("deals_closed", "<", 5),
        ),
    },
    {
        "name": "They need help with something — set up an appointment",
        "action": "set_up_appointment",
        "inference": "the hubspot notes say they need help with something",
    },
]


class SeedRulesCatalog:
    """Reset one user's catalog to the seeded actions and rules."""

    def handle(self, owner_email=None):
        email = self._resolve(owner_email)
        with transaction.atomic():
            owner = self._user_for(email)
            OutreachRule.objects.filter(owner=owner).delete()
            action_by_key = {}
            for spec in ACTIONS:
                action, _created = ActionType.objects.update_or_create(
                    owner=owner,
                    key=spec["key"],
                    defaults={"label": spec["label"], "urgency": spec["urgency"]},
                )
                action_by_key[action.key] = action
            rules = []
            for position, spec in enumerate(RULES):
                rule = OutreachRule(
                    owner=owner,
                    action=action_by_key[spec["action"]],
                    name=spec["name"],
                    kind=(
                        OutreachRule.KIND_INFERENCE
                        if "inference" in spec
                        else OutreachRule.KIND_DETERMINISTIC
                    ),
                    conditions=spec.get("conditions", {}),
                    inference_prompt=spec.get("inference", ""),
                    order=(position + 1) * 10,
                )
                rule.full_clean()
                rules.append(rule)
            OutreachRule.objects.bulk_create(rules)
        return {"owner": email, "actions": len(action_by_key), "rules": len(rules)}

    def _resolve(self, explicit):
        if explicit:
            return explicit.strip().lower()
        if settings.LOGIN_ALLOWED_EMAILS:
            return sorted(settings.LOGIN_ALLOWED_EMAILS)[0]
        return DEFAULT_OWNER_EMAIL

    def _user_for(self, email):
        """Fetch or create the owner, matching the magic-link sign-in
        convention: username == email, unusable password."""
        user_model = get_user_model()
        user = user_model.objects.filter(username=email).first()
        if user is not None:
            return user
        user = user_model(username=email, email=email)
        user.set_unusable_password()
        user.save()
        return user
