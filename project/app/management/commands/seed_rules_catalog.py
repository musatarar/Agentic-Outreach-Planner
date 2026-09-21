"""Seed the demo user's shape and outreach rules catalog.

This command is the one place under ``project/app/`` that names a column: it
writes the demo owner's shape — what a lead and an event are, which columns the
lead authors, and which two hold the contact and the agency — and the rules
follow from it.

The seeded set is the demo user's starting catalog, weighted: strong, unambiguous signals carry 3, softer ones 2 or 1. Several
rules select the same action on purpose — three separate signals argue for a
usage nudge, and a dormant account argues harder than modest momentum — so
the tally decides rather than evaluation position.

Re-running RESETS the owner's catalog to this set: rules they authored
themselves are deleted, and the seeded actions' label and urgency are
restored. Safe to re-run, but not a merge. It also puts every lead in that
owner's book, since a lead with no owner has no rules and this is the command
that knows who the demo owner is.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from project.app.models import Lead, Shape
from project.app.rules.models import ActionType, OutreachRule
from project.app.rules.utils import _all_of, _any_of, _cond

# Used when no --owner is given and LOGIN_ALLOWED_EMAILS is empty.
DEFAULT_OWNER_EMAIL = "demo@lockedin.example"

# What a lead is, for this demo: the CRM export's own columns. `hubspot_notes`
# is the one the lead writes, so it is never read as a corroborator.
LEAD_COLUMNS = [
    {"name": "agency_name", "type": "text", "lead_authored": False},
    {"name": "contact_name", "type": "text", "lead_authored": False},
    {"name": "contact_email", "type": "text", "lead_authored": False},
    {"name": "contact_phone", "type": "text", "lead_authored": False},
    {"name": "state", "type": "text", "lead_authored": False},
    {"name": "num_producers", "type": "number", "lead_authored": False},
    {"name": "years_in_business", "type": "number", "lead_authored": False},
    {"name": "estimated_book_size_usd", "type": "number", "lead_authored": False},
    {"name": "stage", "type": "text", "lead_authored": False},
    {"name": "signed_up_date", "type": "date", "lead_authored": False},
    {"name": "last_login_date", "type": "date", "lead_authored": False},
    {"name": "quotes_created", "type": "number", "lead_authored": False},
    {"name": "quotes_submitted", "type": "number", "lead_authored": False},
    {"name": "deals_closed", "type": "number", "lead_authored": False},
    {"name": "last_contacted_date", "type": "date", "lead_authored": False},
    {"name": "hubspot_notes", "type": "text", "lead_authored": True},
]

# What an event is. `timestamp` is not here: the table carries it.
EVENT_COLUMNS = [
    {"name": "type", "type": "text"},
    {"name": "client", "type": "text"},
    {"name": "premium", "type": "number"},
    {"name": "notes", "type": "text"},
    {"name": "subject", "type": "text"},
    {"name": "outcome", "type": "text"},
    {"name": "lock_term_months", "type": "number"},
]

# The two columns the prompts and the verifier need by meaning.
ROLES = {"contact_name": "contact_name", "agency_name": "agency_name"}

# Phrases (lowercase) suggesting the lead asked to be contacted later. Seed
# data, not engine data: a user edits these on the rule like any other.
HOLD_PHRASES = [
    "waiting on",
    "waiting for",
    "budget approval",
    "budget",
    "follow up in",
    "get back",
    "circle back",
    "touch base in",
    "next quarter",
]

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


def _rules(shape):
    """The demo catalog, against the shape seeded above.

    "conditions" makes a deterministic rule, "inference" an AI-inference one.
    Each condition's source is resolved from the shape (`utils.source_for`), so
    a trusted column reads as `lead`, a lead-authored one as `notes`, and a
    date column's `days_since_` twin as `derived`.
    """

    def cond(field, operator, threshold=None):
        return _cond(field, operator, threshold, shape=shape)

    return [
        {
            "name": "Demo completed but never signed up",
            "action": "complete_onboarding",
            "weight": OutreachRule.WEIGHT_HIGH,
            "conditions": _all_of(
                cond("stage", "==", "demo_completed"),
                cond("signed_up_date", "absent"),
            ),
        },
        {
            "name": "Power user near a reward / volume-pricing milestone",
            "action": "power_user_reward",
            "weight": OutreachRule.WEIGHT_HIGH,
            "conditions": _all_of(
                cond("deals_closed", ">=", 5),
                cond("quotes_submitted", ">=", 10),
            ),
        },
        {
            "name": "Hold period has passed and the lead went quiet",
            "action": "follow_up_after_hold",
            "weight": OutreachRule.WEIGHT_HIGH,
            "conditions": _all_of(
                _any_of(*(cond("hubspot_notes", "contains", p) for p in HOLD_PHRASES)),
                # The corroborator: a hold phrase alone can never fire this rule.
                cond("days_since_last_contacted_date", ">=", 14),
            ),
        },
        {
            "name": "Signed up but stopped using the portal",
            "action": "reengage_dormant",
            "weight": OutreachRule.WEIGHT_HIGH,
            "conditions": _all_of(
                cond("signed_up_date", "exists"),
                cond("days_since_last_login_date", ">", 21),
            ),
        },
        {
            "name": "Created quotes but never submitted one",
            "action": "nudge_usage",
            "weight": OutreachRule.WEIGHT_MEDIUM,
            "conditions": _all_of(
                cond("days_since_last_login_date", "<=", 21),
                cond("quotes_created", ">", 0),
                cond("quotes_submitted", "==", 0),
            ),
        },
        {
            # The milestone is a number a lead wrote in prose, so the model reads
            # it; the conditions gate it, and only a gated lead costs a call.
            "name": "Short of the deal milestone in the notes",
            "action": "nudge_usage",
            "weight": OutreachRule.WEIGHT_MEDIUM,
            "conditions": _all_of(
                cond("days_since_last_login_date", "<=", 21),
                cond("deals_closed", ">", 0),
            ),
            "inference": "the hubspot notes name a deal target this lead is still short of",
        },
        {
            "name": "Modest deal momentum",
            "action": "nudge_usage",
            "weight": OutreachRule.WEIGHT_LOW,
            "conditions": _all_of(
                cond("days_since_last_login_date", "<=", 21),
                cond("deals_closed", ">", 0),
                cond("deals_closed", "<", 5),
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
        "Seed one user's shape, action/rule catalog and put every lead in their book "
        "(idempotent; resets that user's rules). Owner: --owner, else the "
        "first LOGIN_ALLOWED_EMAILS entry, else " + DEFAULT_OWNER_EMAIL + "."
    )

    def add_arguments(self, parser):
        parser.add_argument("--owner", help="Email of the user who owns the catalog.")

    @transaction.atomic
    def handle(self, *args, **options):
        email = self._resolve(options.get("owner"))
        owner = self._user_for(email)

        OutreachRule.objects.filter(owner=owner).delete()
        # The shape first: the rules' vocabulary is read off it.
        shape = Shape.objects.filter(owner=owner).first() or Shape(owner=owner)
        shape.lead_columns = LEAD_COLUMNS
        shape.event_columns = EVENT_COLUMNS
        shape.roles = ROLES
        shape.full_clean()
        shape.save()

        action_by_key = {}
        for spec in ACTIONS:
            action, _created = ActionType.objects.update_or_create(
                owner=owner,
                key=spec["key"],
                defaults={"label": spec["label"], "urgency": spec["urgency"]},
            )
            action_by_key[action.key] = action

        rules = []
        for spec in _rules(shape):
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

        # A lead with no owner has no rules; this is the only command that
        # knows which user the demo runs as.
        leads = Lead.objects.update(owner=owner)

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded a shape, {len(action_by_key)} action types and "
                f"{len(rules)} rules for {email}, and put {leads} lead(s) in their book."
            )
        )

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
