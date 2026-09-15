"""User-defined outreach catalog: action types and the rules that select them.

The user outlines deterministic rules ("deals_closed > 20 ->
reward_power_user") and AI inferences ("notes show they need help -> set up an
appointment"); the planner evaluates them later — deterministic rules
in-process, inference rules via the LLM seam against sanitized, fenced lead
data.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator, RegexValidator
from django.db import models
from django.db.models import Q


class ActionType(models.Model):
    """One kind of outreach a user's rules can select (their action catalog).

    ``key`` is the machine token a firing rule writes into
    ``OutreachAction.action_type``, so it shares that field's length and
    snake_case shape.
    """

    URGENCY_LOW = "low"
    URGENCY_MEDIUM = "medium"
    URGENCY_HIGH = "high"
    URGENCY_CHOICES = [
        (URGENCY_LOW, "Low"),
        (URGENCY_MEDIUM, "Medium"),
        (URGENCY_HIGH, "High"),
    ]

    # ``description`` is prompt-bound (the copy prompt's "Planned action" line).
    DESCRIPTION_MAX_CHARS = 500

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="action_types"
    )
    # "reward_power_user" — unique per owner, not globally: two users may each
    # define their own "reward_power_user" without coordinating.
    key = models.CharField(
        max_length=64,
        validators=[
            RegexValidator(
                r"^[a-z][a-z0-9_]*$",
                "Use a snake_case key: lowercase letters, digits and underscores, "
                "starting with a letter (e.g. 'reward_power_user').",
            )
        ],
    )
    label = models.CharField(max_length=255)  # "Reward power user (volume pricing)"
    # What the action means, for reviewers and for the copy prompt.
    description = models.TextField(
        blank=True, default="", validators=[MaxLengthValidator(DESCRIPTION_MAX_CHARS)]
    )
    urgency = models.CharField(max_length=8, choices=URGENCY_CHOICES, default=URGENCY_MEDIUM)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "key"], name="atype_one_key_per_owner"),
        ]

    def __str__(self):
        return f"{self.key} (user {self.owner_id})"


class OutreachRule(models.Model):
    """A user-authored rule: when its predicate holds for a lead, propose
    ``action``.

    ``kind`` picks the predicate's engine, and exactly one payload field is
    populated (``clean()`` enforces the pairing):

    - ``deterministic`` -> ``conditions``: a structured, versioned payload
      evaluated in-process against lead/event fields.
    - ``inference`` -> ``inference_prompt``: a natural-language predicate
      ("hubspot notes show they need help with something") the LLM seam
      evaluates against the lead's sanitized, fenced data.

    Rules are checked in ``(order, id)`` and the first match wins; no user
    rule matching still falls through to the needs-human UNKNOWN path.
    """

    KIND_DETERMINISTIC = "deterministic"
    KIND_INFERENCE = "inference"
    KIND_CHOICES = [
        (KIND_DETERMINISTIC, "Deterministic"),
        (KIND_INFERENCE, "AI inference"),
    ]

    # ``conditions`` payload schema, version-pinned like the rule-trace
    # envelope and sharing its vocabulary so a stored rule and its recorded
    # evaluation read alike:
    #
    #   {
    #     "version": 1,
    #     "operator": "all_of" | "any_of",
    #     "conditions": [
    #       {"field": "deals_closed", "operator": ">", "threshold": 20,
    #        "source": "lead"},
    #       {"operator": "any_of", "conditions": [...]},  # one nested level max
    #     ],
    #   }
    #
    # Condition operators are the rules engine's evaluation set
    # (==, !=, >, >=, <, <=, in, exists, absent, contains); ``source`` is
    # lead | events | notes | derived. Fields reference the Lead/Event shape
    # for now — user-defined data shapes are deliberately deferred.
    CONDITIONS_SCHEMA_VERSION = 1

    # ``inference_prompt`` is prompt-bound (``build_inference_prompt``); the
    # cap bounds per-rule provider spend.
    INFERENCE_PROMPT_MAX_CHARS = 2000

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="outreach_rules"
    )
    # RESTRICT: a still-selected action cannot be deleted directly but falls
    # with its owner's cascade.
    action = models.ForeignKey(ActionType, on_delete=models.RESTRICT, related_name="rules")
    name = models.CharField(max_length=255)  # "Reward power users"
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    # Deterministic payload (schema above); {} on inference rules.
    conditions = models.JSONField(default=dict, blank=True)
    # Inference predicate; "" on deterministic rules.
    inference_prompt = models.TextField(
        blank=True, default="", validators=[MaxLengthValidator(INFERENCE_PROMPT_MAX_CHARS)]
    )
    enabled = models.BooleanField(default=True)
    # Evaluation position, first match wins; ties break on id so two rules can
    # never evaluate in different orders on different backends.
    order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "id"]
        indexes = [
            # The planner's fetch: one user's enabled rules in evaluation order.
            models.Index(fields=["owner", "enabled", "order"], name="orule_eval_order"),
        ]
        constraints = [
            # Literal kind strings: Meta cannot see the enclosing class namespace.
            models.CheckConstraint(
                check=Q(kind__in=("deterministic", "inference")),
                name="orule_kind_known",
            ),
        ]

    def build_inference_prompt(self):
        """One line of the evaluation prompt: ``<predicate> ? "<action id>"``.

        The planner embeds these lines in a larger prompt and the model
        answers with the quoted action id of the rule that applies.
        """
        if self.kind != self.KIND_INFERENCE:
            raise ValueError("Only inference rules build an inference prompt.")
        predicate = (self.inference_prompt or "").strip()[: self.INFERENCE_PROMPT_MAX_CHARS]
        return f'{predicate} ? "{self.action_id}"'

    def clean(self):
        """Enforce the kind <-> payload pairing and same-owner action selection.

        Cross-table ownership cannot be a DB constraint, so editing surfaces
        must run ``full_clean()``.
        """
        problems = {}
        if self.action_id is not None and self.action.owner_id != self.owner_id:
            problems["action"] = "A rule can only select one of its owner's own action types."
        prompt = (self.inference_prompt or "").strip()
        if self.kind == self.KIND_DETERMINISTIC:
            if not self.conditions:
                problems["conditions"] = "A deterministic rule needs a conditions payload."
            if prompt:
                problems["inference_prompt"] = (
                    "A deterministic rule must not carry an inference prompt."
                )
        elif self.kind == self.KIND_INFERENCE:
            if not prompt:
                problems["inference_prompt"] = (
                    "An inference rule needs its natural-language predicate."
                )
            if self.conditions:
                problems["conditions"] = "An inference rule must not carry structured conditions."
        if problems:
            raise ValidationError(problems)

    def __str__(self):
        return f"rule {self.name!r} ({self.kind}) of user {self.owner_id}"
