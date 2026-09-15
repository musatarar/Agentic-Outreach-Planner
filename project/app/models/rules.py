"""User-defined outreach catalog: action types and the rules that select them.

Today the action vocabulary is hardcoded (services/actions.py) and the rules
that pick an action are compiled into services/outreach.py. These models move
both into data owned by a user: the user outlines deterministic rules
("deals_closed > 20 -> reward_power_user") and AI inferences ("notes show they
need help -> set up an appointment"), and the planner evaluates them later —
deterministic rules in-process, inference rules via the LLM seam with all
lead-controlled text sanitized and fenced (never inside the rule's own prompt
region).

Two invariants these models deliberately do NOT relax:

- Every outbound send stays behind the global human approval gate
  (services/dispatch.py). A rule chooses *what to propose*, never whether a
  send may skip review, so there is no "auto-send" flag here to weaken.
- OutreachAction audit rows keep snapshotting the action *key string*
  (``OutreachAction.action_type``), not an FK into this catalog: audit rows
  must stay self-contained across catalog renames and deletions.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator, RegexValidator
from django.db import models
from django.db.models import Q


class ActionType(models.Model):
    """One kind of outreach a user's rules can select (their action catalog).

    The per-user replacement for the hardcoded ``ACTION_TYPES``/``ACTION_META``
    (services/actions.py). ``key`` is the machine token a firing rule writes
    into ``OutreachAction.action_type``, so it shares that field's length and
    the snake_case shape of the existing constants.
    """

    URGENCY_LOW = "low"
    URGENCY_MEDIUM = "medium"
    URGENCY_HIGH = "high"
    URGENCY_CHOICES = [
        (URGENCY_LOW, "Low"),
        (URGENCY_MEDIUM, "Medium"),
        (URGENCY_HIGH, "High"),
    ]

    # Cap on ``description``: it is prompt-bound (the copy prompt's "Planned
    # action" line), so unbounded operator text is unbounded prompt spend.
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
    # Operator-authored elaboration of what the action means, for reviewers and
    # for the copy prompt. Authored by the authenticated user — NOT
    # lead-controlled — but prompt-bound, hence the cap.
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

    Rules are checked in ``(order, id)`` and the first match wins, exactly like
    the compiled rule tuple this replaces (``ACTION_RULES`` in
    services/outreach.py); no user rule matching still falls through to the
    needs-human UNKNOWN path.
    """

    KIND_DETERMINISTIC = "deterministic"
    KIND_INFERENCE = "inference"
    KIND_CHOICES = [
        (KIND_DETERMINISTIC, "Deterministic"),
        (KIND_INFERENCE, "AI inference"),
    ]

    # ``conditions`` payload schema, pinned like the rule-trace envelope
    # (TRACE_SCHEMA_VERSION in services/outreach.py). Version 1, mirroring the
    # trace vocabulary so a stored rule and its recorded evaluation read alike:
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
    # Condition operators are services/outreach.py's ``_evaluate`` set
    # (==, !=, >, >=, <, <=, in, exists, absent, contains); ``source`` is the
    # trace's lead | events | notes | derived. Fields reference the Lead/Event
    # shape for now — user-defined data shapes are deliberately deferred.
    CONDITIONS_SCHEMA_VERSION = 1

    # Cap on ``inference_prompt``: operator-authored but prompt-bound, so the
    # cap bounds per-rule provider spend. Services re-enforce this at prompt
    # build time; the validator catches it at the editing surface.
    INFERENCE_PROMPT_MAX_CHARS = 2000

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="outreach_rules"
    )
    # RESTRICT, not PROTECT: deleting a catalog entry that live rules still
    # select must be an explicit two-step (delete/repoint the rules first),
    # never a silent sweep — but an owner delete may sweep both together,
    # which PROTECT would wedge even though the rules fall in the same cascade.
    action = models.ForeignKey(ActionType, on_delete=models.RESTRICT, related_name="rules")
    name = models.CharField(max_length=255)  # "Reward power users"
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    # Deterministic payload (schema above); {} on inference rules.
    conditions = models.JSONField(default=dict, blank=True)
    # Inference predicate; "" on deterministic rules. Evaluated *against* fenced
    # lead data — never interpolate lead-controlled text into this region.
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

    def clean(self):
        """Enforce the kind <-> payload pairing and same-owner action selection.

        A rule carrying the wrong payload would silently never fire (or fire on
        stale leftovers); a rule selecting another user's action type would fire
        someone else's catalog entry and wedge that owner's delete against our
        RESTRICT (the foreign rule is outside their cascade). Cross-table
        ownership cannot be a DB constraint, so editing surfaces must run
        ``full_clean()``.
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
