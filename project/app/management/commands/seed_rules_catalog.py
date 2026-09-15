"""Seed the demo user's outreach rules catalog (idempotent, safe to re-run).

Mirrors the hardcoded action vocabulary (services/actions.py) and the compiled
rule order of ``determine_action`` (services/outreach.py) as one user's
``ActionType``/``OutreachRule`` rows. R5's three nudge branches flatten into
three consecutive rules; first-match-wins keeps the semantics. Re-running
resets the owner's rules to the seeded set.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from project.app.models import ActionType, OutreachRule
from project.app.services import actions
from project.app.services.outreach import (
    DORMANT_DAYS,
    POWER_USER_DEALS,
    POWER_USER_SUBMISSIONS,
)

# Used when no --owner is given and LOGIN_ALLOWED_EMAILS is empty.
DEFAULT_OWNER_EMAIL = "demo@lockedin.example"

ACTIONS = [
    {
        "key": key,
        "label": actions.ACTION_META[key]["label"],
        "urgency": actions.ACTION_META[key]["urgency"],
    }
    for key in actions.SELECTABLE_ACTION_TYPES
]


def _cond(field, operator, threshold=None, source="lead"):
    condition = {"field": field, "operator": operator, "source": source}
    if threshold is not None:
        condition["threshold"] = threshold
    return condition


def _all_of(*conditions):
    return {
        "version": OutreachRule.CONDITIONS_SCHEMA_VERSION,
        "operator": "all_of",
        "conditions": list(conditions),
    }


# In ``determine_action``'s evaluation order; derived fields name the engine's
# computed predicates exactly as the rule trace records them.
RULES = [
    (
        "Demo completed but never signed up",
        actions.COMPLETE_ONBOARDING,
        _all_of(
            _cond("stage", "==", "demo_completed"),
            _cond("signed_up_date", "absent"),
        ),
    ),
    (
        "Power user near a reward / volume-pricing milestone",
        actions.POWER_USER_REWARD,
        _all_of(
            _cond("deals_closed", ">=", POWER_USER_DEALS),
            _cond("quotes_submitted", ">=", POWER_USER_SUBMISSIONS),
        ),
    ),
    (
        "Hold period has passed and the lead went quiet",
        actions.FOLLOW_UP_AFTER_HOLD,
        _all_of(
            _cond("hubspot_notes", "contains", "HOLD_PHRASES", source="notes"),
            _cond("gone_quiet", "==", True, source="derived"),
        ),
    ),
    (
        "Signed up but stopped using the portal",
        actions.REENGAGE_DORMANT,
        _all_of(
            _cond("signed_up_date", "exists"),
            _cond("days_since_last_login", ">", DORMANT_DAYS, source="derived"),
        ),
    ),
    (
        "Active but underusing — created quotes, never submitted one",
        actions.NUDGE_USAGE,
        _all_of(
            _cond("days_since_last_login", "<=", DORMANT_DAYS, source="derived"),
            _cond("quotes_created", ">", 0),
            _cond("quotes_submitted", "==", 0),
        ),
    ),
    (
        "Active but underusing — short of the milestone in the notes",
        actions.NUDGE_USAGE,
        _all_of(
            _cond("days_since_last_login", "<=", DORMANT_DAYS, source="derived"),
            _cond("deals_closed", ">", 0),
            _cond("milestone_from_notes", "exists", source="derived"),
            _cond("deals_below_milestone", "==", True, source="derived"),
        ),
    ),
    (
        "Active but underusing — modest deal momentum",
        actions.NUDGE_USAGE,
        _all_of(
            _cond("days_since_last_login", "<=", DORMANT_DAYS, source="derived"),
            _cond("deals_closed", ">", 0),
            _cond("deals_closed", "<", POWER_USER_DEALS),
        ),
    ),
]


class Command(BaseCommand):
    help = (
        "Seed one user's ActionType/OutreachRule catalog mirroring the compiled "
        "planner rules (idempotent; resets that user's rules). Owner: --owner, "
        "else the first LOGIN_ALLOWED_EMAILS entry, else " + DEFAULT_OWNER_EMAIL + "."
    )

    def add_arguments(self, parser):
        parser.add_argument("--owner", help="Email of the user who owns the catalog.")

    @transaction.atomic
    def handle(self, *args, **options):
        email = self._resolve_owner_email(options.get("owner"))
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
        for position, (name, action_key, conditions) in enumerate(RULES):
            rule = OutreachRule(
                owner=owner,
                action=action_by_key[action_key],
                name=name,
                kind=OutreachRule.KIND_DETERMINISTIC,
                conditions=conditions,
                order=(position + 1) * 10,
            )
            rule.full_clean()
            rules.append(rule)
        OutreachRule.objects.bulk_create(rules)

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(action_by_key)} action types and {len(rules)} rules for {email}."
            )
        )

    def _resolve_owner_email(self, explicit):
        if explicit:
            return explicit.strip().lower()
        if settings.LOGIN_ALLOWED_EMAILS:
            return sorted(settings.LOGIN_ALLOWED_EMAILS)[0]
        return DEFAULT_OWNER_EMAIL

    def _user_for(self, email):
        """Fetch or create the owner, matching the magic-link sign-in convention
        (views/auth.py): username == email, unusable password."""
        user_model = get_user_model()
        user = user_model.objects.filter(username=email).first()
        if user is not None:
            return user
        user = user_model(username=email, email=email)
        user.set_unusable_password()
        user.save()
        return user
