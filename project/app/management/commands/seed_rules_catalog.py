"""Seed the demo user's outreach rules catalog.

The seeded set reproduces the planner's compiled behavior as editable data,
weighted: strong, unambiguous signals carry 3, softer ones 2 or 1. Several
rules select the same action on purpose — three separate signals argue for a
usage nudge, and a dormant account argues harder than modest momentum — so
the tally decides rather than evaluation position.

Re-running RESETS the owner's catalog to this set: rules they authored
themselves are deleted, and the seeded actions' label and urgency are
restored. Safe to re-run, but not a merge.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from project.app.actions.models import TenantCatalog
from project.app.rules.models import ActionType, OutreachRule
from project.app.rules.utils import _all_of, _cond
from project.app.services.owners import DEFAULT_OWNER_EMAIL, resolve_owner

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

# "conditions" makes a deterministic rule, "inference" an AI-inference one.
# Each condition's source is resolved from the field name (`utils.source_for`),
# so a lead column reads as `lead`, an event column as `events`, and the
# engine's computed figures as `derived` or `notes`.
RULES = [
    {
        "name": "Demo completed but never signed up",
        "action": "complete_onboarding",
        "weight": OutreachRule.WEIGHT_HIGH,
        "conditions": _all_of(
            _cond("stage", "==", "demo_completed"),
            _cond("signed_up_date", "absent"),
        ),
    },
    {
        "name": "Power user near a reward / volume-pricing milestone",
        "action": "power_user_reward",
        "weight": OutreachRule.WEIGHT_HIGH,
        "conditions": _all_of(
            _cond("deals_closed", ">=", 5),
            _cond("quotes_submitted", ">=", 10),
        ),
    },
    {
        "name": "Hold period has passed and the lead went quiet",
        "action": "follow_up_after_hold",
        "weight": OutreachRule.WEIGHT_HIGH,
        "conditions": _all_of(
            _cond("hubspot_notes", "contains", "HOLD_PHRASES"),
            _cond("gone_quiet", "==", True),
        ),
    },
    {
        "name": "Signed up but stopped using the portal",
        "action": "reengage_dormant",
        "weight": OutreachRule.WEIGHT_HIGH,
        "conditions": _all_of(
            _cond("signed_up_date", "exists"),
            _cond("days_since_last_login", ">", 21),
        ),
    },
    {
        "name": "Created quotes but never submitted one",
        "action": "nudge_usage",
        "weight": OutreachRule.WEIGHT_MEDIUM,
        "conditions": _all_of(
            _cond("days_since_last_login", "<=", 21),
            _cond("quotes_created", ">", 0),
            _cond("quotes_submitted", "==", 0),
        ),
    },
    {
        "name": "Short of the deal milestone in the notes",
        "action": "nudge_usage",
        "weight": OutreachRule.WEIGHT_MEDIUM,
        "conditions": _all_of(
            _cond("days_since_last_login", "<=", 21),
            _cond("deals_closed", ">", 0),
            _cond("milestone_from_notes", "exists"),
            _cond("deals_below_milestone", "==", True),
        ),
    },
    {
        "name": "Modest deal momentum",
        "action": "nudge_usage",
        "weight": OutreachRule.WEIGHT_LOW,
        "conditions": _all_of(
            _cond("days_since_last_login", "<=", 21),
            _cond("deals_closed", ">", 0),
            _cond("deals_closed", "<", 5),
        ),
    },
    {
        # No conditions: the predicate stands alone, as in the brief. Adding
        # conditions here would gate the model behind them, which is how a
        # rule avoids spending a provider call on every lead.
        "name": "They need help with something — set up an appointment",
        "action": "set_up_appointment",
        "weight": OutreachRule.WEIGHT_HIGH,
        "inference": "the hubspot notes say they need help with something",
    },
]


class Command(BaseCommand):
    help = (
        "Seed one user's action/rule catalog (idempotent; resets that user's "
        "rules). Owner: --owner, else the first LOGIN_ALLOWED_EMAILS entry, "
        "else " + DEFAULT_OWNER_EMAIL + "."
    )

    def add_arguments(self, parser):
        parser.add_argument("--owner", help="Email of the user who owns the catalog.")
        parser.add_argument(
            "--tenant",
            default="",
            help="Tenant whose leads run this catalog (default: the blank tenant every demo lead carries).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        owner = resolve_owner(options.get("owner"))
        email = owner.username

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
        for spec in RULES:
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
                weight=spec["weight"],
            )
            rule.full_clean()
            rules.append(rule)
        OutreachRule.objects.bulk_create(rules)

        # Without this join the actions engine finds no rules for the lead's tenant.
        tenant = options.get("tenant") or ""
        TenantCatalog.objects.get_or_create(tenant=tenant, owner=owner)

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(action_by_key)} action types and {len(rules)} rules for {email}, "
                f"run by tenant {tenant!r}."
            )
        )
