"""The outreach decision audit trail: planner output through reviewer decisions."""

from django.db import models
from django.db.models import Q

from project.app.rules.models import ActionType

from .lead import Lead

# Cap on the stored subject line, matching `services.outreach.MAX_SUBJECT_CHARS`.
SUBJECT_MAX_CHARS = 120


class OutreachGeneratedCopy(models.Model):  # copy awaiting review
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="generated_copy")
    created_at = models.DateTimeField(auto_now_add=True)
    # Null on an engine draft, which derives it from `action.urgency` instead.
    priority = models.IntegerField(null=True, blank=True)  # 1 highest, 3 lowest
    action_type = models.CharField(max_length=64)  # see ACTION_TYPES
    # The catalog action this was generated for; null on a planner-written row.
    action = models.ForeignKey(
        ActionType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        db_index=False,
    )
    reason = models.TextField()  # why this lead, why now
    suggested_copy = models.TextField(blank=True)  # LLM-generated email/message
    # The provider's two fields behind `suggested_copy`; "" when nothing was
    # generated. `suggested_copy` stays the string every span offset indexes.
    subject = models.CharField(max_length=SUBJECT_MAX_CHARS, blank=True, default="")
    body = models.TextField(blank=True, default="")
    needs_human = models.BooleanField(default=False)  # unknown action -> report to BD
    further_action = models.TextField(blank=True)  # what ops/AE should do next

    # ---- Review flow -------------------------------------------------------
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_DISMISSED = "dismissed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_DISMISSED, "Dismissed"),
    ]

    DISMISS_REASONS = [
        "not_a_fit",
        "bad_timing",
        "wrong_contact",
        "already_handled",
        "copy_unusable",
        "other",
    ]

    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True
    )
    # Set on every status write: when this row was decided.
    status_changed_at = models.DateTimeField(null=True, blank=True, default=None)

    # Reviewer's edit; "" means never edited. `suggested_copy` is IMMUTABLE --
    # the reviewer's edit is a separate column so the two can always be diffed.
    edited_copy = models.TextField(blank=True, default="")
    # The edit's two fields. Blank on an edit sent as a whole string with no
    # `Subject:` line, so these are not constrained the way the generated pair is.
    edited_subject = models.CharField(max_length=SUBJECT_MAX_CHARS, blank=True, default="")
    edited_body = models.TextField(blank=True, default="")

    # Stable identity of "this recommendation for this lead". See DismissedOutreachKey.
    dedupe_key = models.CharField(max_length=128, blank=True, default="", db_index=True)

    # Verification report (schema v1) for `effective_copy`; rewritten on every
    # /edit/ and /approve/.
    verification = models.JSONField(default=dict, blank=True)

    # The state machine. Anything not listed is a 409 `invalid_transition`,
    # never a silent no-op. Reopening a dismissal must also revoke the
    # suppression row.
    ALLOWED_TRANSITIONS = {
        STATUS_PENDING: (STATUS_APPROVED, STATUS_DISMISSED),
        STATUS_APPROVED: (STATUS_PENDING,),
        STATUS_DISMISSED: (STATUS_PENDING,),
    }

    # Editing is not a status transition, so it needs its own guard.
    EDITABLE_STATUSES = (STATUS_PENDING,)

    def can_transition_to(self, new_status):
        """True when `new_status` is a legal next state from the current one."""
        return new_status in self.ALLOWED_TRANSITIONS.get(self.status, ())

    @property
    def effective_copy(self):
        """The copy that would actually be sent: the edit if there is one."""
        return self.edited_copy or self.suggested_copy

    @property
    def effective_subject(self):
        """The subject of `effective_copy`; the edit's if there is one."""
        return self.edited_subject if self.edited_copy else self.subject

    @property
    def effective_body(self):
        """The body of `effective_copy`; the edit's if there is one."""
        return self.edited_body if self.edited_copy else self.body

    class Meta:
        indexes = [
            models.Index(fields=["status", "priority", "lead"], name="oa_queue_order"),
        ]
        constraints = [
            # Generated copy is always `render_email` of a validated pair, so a
            # draft with no parts behind it is a row no producer can write.
            models.CheckConstraint(
                check=Q(suggested_copy="") | (~Q(subject="") & ~Q(body="")),
                name="ogc_parts_present_with_copy",
            ),
        ]

    def __str__(self):
        return f"{self.lead_id} - {self.action_type} (p{self.priority})"


class DismissedOutreachKey(models.Model):
    """Permanent suppression ledger for dismissed recommendations.

    Consulted by `plan_outreach()` BEFORE generating copy, so a dismissed
    recommendation costs no LLM call on a re-run. Outlives the row that created
    it (SET_NULL); reopening revokes rather than deletes.
    """

    # sha256("v1|{lead_id}|{action_type}").hexdigest(). See services/dedupe.py.
    dedupe_key = models.CharField(max_length=128, unique=True, db_index=True)
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="dismissed_keys")
    action_type = models.CharField(max_length=64)
    reason = models.CharField(max_length=64, blank=True, default="")
    dismissed_at = models.DateTimeField(auto_now_add=True)
    dismissed_by = models.EmailField(blank=True, default="")
    source_action = models.ForeignKey(
        OutreachGeneratedCopy, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # Set by reopening a dismissal; a revoked row no longer suppresses but is
    # kept for audit.
    revoked_at = models.DateTimeField(null=True, blank=True, default=None)

    class Meta:
        ordering = ["-dismissed_at"]

    def __str__(self):
        return f"dismissed {self.lead_id}/{self.action_type}"
