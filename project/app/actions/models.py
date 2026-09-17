"""The actions engine's own tables: the work queue, and whose rules a tenant runs.

A job is one lead's trip through the engine: queued with the events it was
queued for, claimed by the cron, then resolved by the deterministic pass or,
failing that, the inference pass. Every status write is a conditional UPDATE in
:mod:`project.app.actions.services`, so two crons cannot run the same job.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q


class TenantCatalog(models.Model):
    """Whose rules catalog the engine evaluates for a tenant's leads.

    Rules are owned by a user (``OutreachRule.owner``) and leads carry an
    opaque ``tenant``; nothing joined the two, so this is that join. A tenant
    with no row here has no rules, and its jobs resolve to no action.
    """

    tenant = models.CharField(max_length=64, blank=True, default="", db_index=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tenant_catalogs"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["tenant", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "owner"], name="tcatalog_one_row_per_tenant_owner"
            ),
        ]

    def __str__(self):
        return f"tenant {self.tenant!r} runs user {self.owner_id}'s catalog"


class ActionJob(models.Model):
    """One queued lead, the events it was queued for, and what the engine chose.

    ``events`` is a snapshot taken at enqueue rather than ``lead.events``: the
    job is evaluated against the activity it was queued for, so a lead that
    logs in mid-run does not change a decision already in flight.
    """

    STATUS_QUEUED = "queued"
    STATUS_PROCESSING = "processing"
    STATUS_DETERMINISTIC_ACTION_CHOSEN = "deterministic_action_chosen"
    STATUS_INFERRING = "inferring"
    STATUS_INFERRED_ACTION_CHOSEN = "inferred_action_chosen"
    STATUS_NO_ACTION = "no_action"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_DETERMINISTIC_ACTION_CHOSEN, "Deterministic action chosen"),
        (STATUS_INFERRING, "Inferring"),
        (STATUS_INFERRED_ACTION_CHOSEN, "Inferred action chosen"),
        (STATUS_NO_ACTION, "No action"),
        (STATUS_FAILED, "Failed"),
    ]

    # Statuses that still owe work, so re-enqueuing the lead is a no-op.
    OPEN_STATUSES = (STATUS_QUEUED, STATUS_PROCESSING, STATUS_INFERRING)

    # The state machine. A transition runs as a conditional UPDATE from the
    # status named here, never a read-then-check.
    ALLOWED_TRANSITIONS = {
        STATUS_QUEUED: (STATUS_PROCESSING, STATUS_FAILED),
        STATUS_PROCESSING: (
            STATUS_DETERMINISTIC_ACTION_CHOSEN,
            STATUS_INFERRING,
            STATUS_NO_ACTION,
            STATUS_FAILED,
        ),
        STATUS_INFERRING: (STATUS_INFERRED_ACTION_CHOSEN, STATUS_NO_ACTION, STATUS_FAILED),
    }

    lead = models.ForeignKey("app.Lead", on_delete=models.CASCADE, related_name="action_jobs")
    # Snapshot of the lead's tenant at enqueue: the job keeps running against
    # the catalog it was queued for.
    tenant = models.CharField(max_length=64, blank=True, default="", db_index=True)
    events = models.ManyToManyField("app.Event", blank=True, related_name="action_jobs")
    status = models.CharField(
        max_length=32, choices=STATUS_CHOICES, default=STATUS_QUEUED, db_index=True
    )
    # The chosen action, NULL until a pass chooses one (and on no_action/failed).
    selected_action = models.ForeignKey(
        "app.ActionType", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # The tally's workings: which rules fired, their weight, the stub's TODO.
    # Rule names only -- no lead text, no prompts.
    decision = models.JSONField(default=dict, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True, default=None)
    finished_at = models.DateTimeField(null=True, blank=True, default=None)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            # The cron's fetch: the oldest queued jobs first.
            models.Index(fields=["status", "created_at"], name="ajob_queue_order"),
        ]
        constraints = [
            # Literal values: Meta cannot see the enclosing class namespace.
            models.CheckConstraint(
                check=Q(
                    status__in=(
                        "queued",
                        "processing",
                        "deterministic_action_chosen",
                        "inferring",
                        "inferred_action_chosen",
                        "no_action",
                        "failed",
                    )
                ),
                name="ajob_status_known",
            ),
            # One open job per lead, enforced by the database: the enqueue path
            # inserts and handles the refusal rather than looking first.
            models.UniqueConstraint(
                fields=["lead"],
                condition=Q(status__in=("queued", "processing", "inferring")),
                name="ajob_one_open_per_lead",
            ),
        ]

    def can_transition_to(self, new_status):
        """True when ``new_status`` is a legal next state from the current one."""
        return new_status in self.ALLOWED_TRANSITIONS.get(self.status, ())

    def __str__(self):
        return f"job {self.pk} for {self.lead_id} ({self.status})"
