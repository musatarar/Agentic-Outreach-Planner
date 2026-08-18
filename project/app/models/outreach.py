"""The outreach decision audit trail: planner output through the send record."""

from django.db import models
from django.db.models import Q

from .lead import Lead


class OutreachAction(models.Model):  # what the planner decided/did
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="outreach_actions")
    created_at = models.DateTimeField(auto_now_add=True)
    priority = models.IntegerField()  # 1 highest, 3 lowest
    action_type = models.CharField(max_length=64)  # see ACTION_TYPES
    reason = models.TextField()  # why this lead, why now
    suggested_copy = models.TextField(blank=True)  # Claude-generated email/message
    needs_human = models.BooleanField(default=False)  # unknown action -> report to BD
    further_action = models.TextField(blank=True)  # what ops/AE should do next

    # ---- Triage queue (MUS-39) --------------------------------------------
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_SNOOZED = "snoozed"
    STATUS_DISMISSED = "dismissed"
    STATUS_SENT = "sent"  # terminal: recorded outbound mail exists (MUS-29)
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_SNOOZED, "Snoozed"),
        (STATUS_DISMISSED, "Dismissed"),
        (STATUS_SENT, "Sent"),
    ]

    TRIGGER_TOMORROW = "tomorrow"
    TRIGGER_IN_3_DAYS = "in_3_days"
    TRIGGER_NEXT_WEEK = "next_week"
    TRIGGER_CUSTOM = "custom"
    TRIGGER_ON_ACTIVITY = "on_activity"
    SNOOZE_TRIGGER_CHOICES = [
        (TRIGGER_TOMORROW, "Tomorrow"),
        (TRIGGER_IN_3_DAYS, "In 3 days"),
        (TRIGGER_NEXT_WEEK, "Next week"),
        (TRIGGER_CUSTOM, "Custom date"),
        (TRIGGER_ON_ACTIVITY, "When they do something"),
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
    # Set on every status write; drives the undo window and the /done ordering.
    status_changed_at = models.DateTimeField(null=True, blank=True, default=None)

    # Reviewer's edit; "" means never edited. `suggested_copy` is IMMUTABLE --
    # the eval corpus diffs it against what a human actually sent.
    edited_copy = models.TextField(blank=True, default="")

    # Non-NULL whenever status == "snoozed" (on_activity uses a backstop --
    # see TRIAGE_SNOOZE_ON_ACTIVITY_BACKSTOP_DAYS).
    snooze_until = models.DateTimeField(null=True, blank=True, default=None, db_index=True)
    snooze_trigger = models.CharField(
        max_length=16, choices=SNOOZE_TRIGGER_CHOICES, blank=True, default=""
    )
    # Watermark for trigger == "on_activity": only events after this wake the item.
    snooze_activity_after = models.DateTimeField(null=True, blank=True, default=None)

    dismiss_reason = models.CharField(max_length=64, blank=True, default="")

    # Stable identity of "this recommendation for this lead". See DismissedOutreachKey.
    dedupe_key = models.CharField(max_length=128, blank=True, default="", db_index=True)

    # Planner run UUID, also on that run's trace as `outreach.run.id` (MUS-25).
    # Known before the row exists, so a span can reference the row via
    # (trace_run_id, lead_id). Blank on rows not written by a planner run.
    trace_run_id = models.CharField(max_length=36, blank=True, default="", db_index=True)

    # Rule trace snapshot (schema v1) from services/outreach.py::explain().
    # Never recomputed: its relative figures are only true as of `trace.today`.
    rule_trace = models.JSONField(default=dict, blank=True)

    # Verification report (schema v1) for `effective_copy`; rewritten on every
    # /edit/ and /approve/.
    verification = models.JSONField(default=dict, blank=True)

    # The state machine. Anything not listed is a 409 `invalid_transition`,
    # never a silent no-op. `sent` is terminal; undo of a dismissal must also
    # revoke the suppression.
    ALLOWED_TRANSITIONS = {
        STATUS_PENDING: (STATUS_APPROVED, STATUS_SNOOZED, STATUS_DISMISSED),
        STATUS_SNOOZED: (STATUS_APPROVED, STATUS_SNOOZED, STATUS_DISMISSED, STATUS_PENDING),
        STATUS_APPROVED: (STATUS_PENDING, STATUS_SENT),
        STATUS_DISMISSED: (STATUS_PENDING,),
        STATUS_SENT: (),
    }

    # Editing is not a status transition, so it needs its own guard.
    EDITABLE_STATUSES = (STATUS_PENDING, STATUS_SNOOZED)

    def can_transition_to(self, new_status):
        """True when `new_status` is a legal next state from the current one."""
        return new_status in self.ALLOWED_TRANSITIONS.get(self.status, ())

    @property
    def effective_copy(self):
        """The copy that would actually be sent: the edit if there is one."""
        return self.edited_copy or self.suggested_copy

    class Meta:
        indexes = [
            models.Index(fields=["status", "priority", "lead"], name="oa_queue_order"),
            models.Index(fields=["status", "-status_changed_at"], name="oa_done_order"),
        ]
        constraints = [
            # Traces promise (trace_run_id, lead_id) resolves to one row.
            # Partial: rows written before MUS-25 have trace_run_id="".
            models.UniqueConstraint(
                fields=["trace_run_id", "lead"],
                condition=~Q(trace_run_id=""),
                name="oa_one_row_per_lead_per_run",
            ),
        ]

    def __str__(self):
        return f"{self.lead_id} - {self.action_type} (p{self.priority})"


class ReviewDecision(models.Model):
    KIND_SELECT = "select_existing"
    KIND_PROPOSE = "propose_new"
    KIND_APPROVE_SEND = "approve_send"  # written by QueueApproveView (MUS-29)
    KIND_REJECT_SEND = "reject_send"  # written by QueueDismissView (MUS-29)
    RESOLUTION_KINDS = (KIND_SELECT, KIND_PROPOSE)
    SEND_KINDS = (KIND_APPROVE_SEND, KIND_REJECT_SEND)
    STATUS_RESOLVED = "resolved"
    STATUS_PENDING = "pending_engineering"

    # ForeignKey, not OneToOne (MUS-29): an action can hold both a resolution
    # and a send decision; per-category uniqueness is the partial constraints below.
    outreach_action = models.ForeignKey(
        OutreachAction, on_delete=models.CASCADE, related_name="review_decisions"
    )
    # select_existing | propose_new | approve_send | reject_send
    kind = models.CharField(max_length=32)
    status = models.CharField(max_length=32)  # resolved | pending_engineering
    selected_action_type = models.CharField(max_length=64, blank=True)
    proposed_name = models.CharField(max_length=255, blank=True)
    proposed_what = models.TextField(blank=True)
    proposed_when = models.TextField(blank=True)
    reviewer = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # ---- Send-decision fields (MUS-29); blank on resolution kinds ----------
    # Snapshot of `effective_copy` at decision time, hash-bound so dispatch
    # refuses to send anything else.
    approved_copy = models.TextField(blank=True, default="")
    approved_body_sha256 = models.CharField(max_length=64, blank=True, default="")
    # Stamped by undo; a voided approval is kept for audit and never authorizes a send.
    voided_at = models.DateTimeField(null=True, blank=True, default=None)

    class Meta:
        constraints = [
            # One resolution per action, ever. Literal kind strings: Meta
            # cannot see the enclosing class namespace.
            models.UniqueConstraint(
                fields=["outreach_action"],
                condition=Q(kind__in=("select_existing", "propose_new")),
                name="rd_one_resolution_per_action",
            ),
            # One LIVE send decision per action: undo voids rather than deletes.
            models.UniqueConstraint(
                fields=["outreach_action"],
                condition=Q(kind__in=("approve_send", "reject_send")) & Q(voided_at__isnull=True),
                name="rd_one_live_send_per_action",
            ),
        ]


class DismissedOutreachKey(models.Model):
    """Permanent suppression ledger for dismissed recommendations (MUS-39).

    Consulted by `plan_outreach()` BEFORE generating copy, so a dismissed
    recommendation costs no LLM call on a re-run. Outlives the OutreachAction
    that created it (SET_NULL); undo revokes rather than deletes.
    """

    # sha256("v1|{lead_id}|{action_type}").hexdigest(). See services/dedupe.py.
    dedupe_key = models.CharField(max_length=128, unique=True, db_index=True)
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="dismissed_keys")
    action_type = models.CharField(max_length=64)
    reason = models.CharField(max_length=64, blank=True, default="")
    dismissed_at = models.DateTimeField(auto_now_add=True)
    dismissed_by = models.EmailField(blank=True, default="")
    source_action = models.ForeignKey(
        OutreachAction, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # Set by undo-of-dismiss; a revoked row no longer suppresses but is kept for audit.
    revoked_at = models.DateTimeField(null=True, blank=True, default=None)

    class Meta:
        ordering = ["-dismissed_at"]

    def __str__(self):
        return f"dismissed {self.lead_id}/{self.action_type}"


class OutreachEdit(models.Model):
    """Append-only log of reviewer edits to generated copy (MUS-39).

    This is the eval corpus: what the model wrote vs. what a human actually
    sent. Its own table keeps large diffs off the constant-query-count queue payload.
    """

    outreach_action = models.ForeignKey(
        OutreachAction, on_delete=models.CASCADE, related_name="edits"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    editor = models.EmailField(blank=True, default="")

    # Full before/after so the corpus survives any change to the diff algorithm.
    before_text = models.TextField()
    after_text = models.TextField()

    # difflib.SequenceMatcher opcodes, non-"equal" only (schema v1).
    diff_ops = models.JSONField(default=list, blank=True)

    chars_added = models.IntegerField(default=0)
    chars_removed = models.IntegerField(default=0)
    # difflib.SequenceMatcher(None, before, after).ratio(), 0.0-1.0.
    similarity = models.FloatField(default=1.0)
    # True for the edit that was in place at the moment of approval.
    committed = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"edit of action {self.outreach_action_id} @ {self.created_at:%Y-%m-%d %H:%M}"


class OutboundSend(models.Model):
    """The single send record: the DB backstop against double-send (MUS-29).

    The OneToOne makes a second send row impossible. PROTECT on both FKs:
    records of real outbound mail must outlive everything that produced them.
    """

    outreach_action = models.OneToOneField(
        OutreachAction, on_delete=models.PROTECT, related_name="outbound_send"
    )
    decision = models.ForeignKey(ReviewDecision, on_delete=models.PROTECT, related_name="+")
    body_sha256 = models.CharField(max_length=64)
    channel = models.CharField(max_length=16, default="console")
    sent_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"send for action {self.outreach_action_id}"
