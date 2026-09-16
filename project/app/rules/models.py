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

from project.app.rules import utils


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

    A ``deterministic`` rule is its ``conditions``: a structured, versioned
    payload (:mod:`project.app.rules.utils`) evaluated in-process. An
    ``inference`` rule adds ``inference_prompt``, a natural-language predicate
    the LLM seam evaluates against the lead's sanitized, fenced data, and may
    stand on that predicate alone. Conditions on an inference rule are
    optional and act as a gate: the model is asked only once they hold, so a
    lead the structured part already ruled out costs no provider call.

    Rules are not first-match: every one is evaluated, each rule that fires
    adds its ``weight`` to its action's tally, and the heaviest tally is the
    action proposed (``services.select_action``). Several rules may select the
    same action, so a strong signal and a weak one can agree and outweigh a
    lone rival. A tally of 1 — one weight-1 rule alone — proposes nothing, as
    does no rule firing at all: both route the lead to a human.
    """

    KIND_DETERMINISTIC = "deterministic"
    KIND_INFERENCE = "inference"
    KIND_CHOICES = [
        (KIND_DETERMINISTIC, "Deterministic"),
        (KIND_INFERENCE, "AI inference"),
    ]

    WEIGHT_LOW = 1
    WEIGHT_MEDIUM = 2
    WEIGHT_HIGH = 3
    WEIGHT_CHOICES = [
        (WEIGHT_LOW, "Low"),
        (WEIGHT_MEDIUM, "Medium"),
        (WEIGHT_HIGH, "High"),
    ]

    # The ``conditions`` schema, its vocabulary and its validator all live in
    # utils; fields reference the Lead/Event shape for now, since user-defined
    # data shapes are deliberately deferred.
    CONDITIONS_SCHEMA_VERSION = utils.SCHEMA_VERSION

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
    # Structured predicate. Required on a deterministic rule, since it is the
    # whole predicate; optional on an inference rule, where it gates the model.
    conditions = models.JSONField(default=dict, blank=True)
    # Inference predicate; "" on deterministic rules.
    inference_prompt = models.TextField(
        blank=True, default="", validators=[MaxLengthValidator(INFERENCE_PROMPT_MAX_CHARS)]
    )
    enabled = models.BooleanField(default=True)
    # How much this rule's firing argues for its action, 1 (weak) to 3 (strong).
    weight = models.PositiveSmallIntegerField(choices=WEIGHT_CHOICES, default=WEIGHT_MEDIUM)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Heaviest first for the editing surfaces; evaluation reads them all.
        ordering = ["-weight", "id"]
        indexes = [
            # The planner's fetch: one user's enabled rules.
            models.Index(fields=["owner", "enabled"], name="orule_owner_enabled"),
        ]
        constraints = [
            # Literal values: Meta cannot see the enclosing class namespace.
            models.CheckConstraint(
                check=Q(kind__in=("deterministic", "inference")),
                name="orule_kind_known",
            ),
            models.CheckConstraint(check=Q(weight__in=(1, 2, 3)), name="orule_weight_1_to_3"),
        ]

    def build_inference_prompt(self, label):
        """One line of the evaluation prompt: ``<predicate> ? "<label>"``.

        ``label`` is assigned by the caller for one prompt and mapped back
        server-side. Database ids never enter the prompt, so the model can only
        ever answer with a slot the caller put in front of it — a predicate
        that names some other id names nothing. ``clean()`` has already refused
        the characters that could forge a second line or answer slot.
        """
        if self.kind != self.KIND_INFERENCE:
            raise ValueError("Only inference rules build an inference prompt.")
        return f'{(self.inference_prompt or "").strip()} ? "{label}"'

    def clean(self):
        """Enforce the kind <-> payload pairing and same-owner action selection.

        Cross-table ownership cannot be a DB constraint, so editing surfaces
        must run ``full_clean()``.
        """
        problems = {}
        if self.action_id is not None and self.action.owner_id != self.owner_id:
            problems["action"] = "A rule can only select one of its owner's own action types."

        if self.conditions:
            try:
                utils.validate_conditions(self.conditions)
            except ValidationError as exc:
                problems["conditions"] = exc.messages

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
            else:
                try:
                    utils.validate_inference_predicate(prompt)
                except ValidationError as exc:
                    problems["inference_prompt"] = exc.messages
        if problems:
            raise ValidationError(problems)

    def __str__(self):
        return f"rule {self.name!r} ({self.kind}) of user {self.owner_id}"
