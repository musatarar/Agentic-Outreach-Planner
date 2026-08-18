from django.db import models
from django.db.models import CheckConstraint, Q


class Lead(models.Model):
    id = models.CharField(max_length=32, primary_key=True)  # "lead_001"
    agency_name = models.CharField(max_length=255)
    contact_name = models.CharField(max_length=255)
    contact_email = models.EmailField()
    contact_phone = models.CharField(max_length=32)
    state = models.CharField(max_length=2)
    num_producers = models.IntegerField()
    years_in_business = models.IntegerField()
    estimated_book_size_usd = models.BigIntegerField()
    stage = models.CharField(max_length=32)  # "active_trial" | "demo_completed"
    signed_up_date = models.DateField(null=True)
    last_login_date = models.DateField(null=True)
    quotes_created = models.IntegerField(default=0)
    quotes_submitted = models.IntegerField(default=0)
    deals_closed = models.IntegerField(default=0)
    last_contacted_date = models.DateField(null=True)
    hubspot_notes = models.TextField(blank=True)

    def __str__(self):
        return f"{self.id} - {self.agency_name}"


class Event(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="events")
    type = models.CharField(max_length=32)  # login, quote_created, quote_submitted,
    # deal_closed, call_logged, email_sent,
    # demo_completed, onboarding_call
    timestamp = models.DateTimeField()
    meta = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.lead_id} - {self.type} @ {self.timestamp:%Y-%m-%d}"


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


class LLMProvider(models.Model):
    """A supported LLM vendor (claude, chatgpt, deepseek, groq, ...).

    Seeded by ``manage.py seed_llm_catalog`` (idempotent); read-only via the API.
    """

    key = models.CharField(max_length=32, primary_key=True)  # "claude", "groq", ...
    label = models.CharField(max_length=100)  # "Anthropic Claude"
    api_key_url = models.URLField()  # where an operator gets a key for this provider
    api_key_label = models.CharField(max_length=100)  # "Anthropic API key"
    api_key_prefix = models.CharField(max_length=16, blank=True)  # "sk-ant-"
    sort_order = models.IntegerField(default=0)
    enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "key"]

    def __str__(self):
        return self.label


class LLMModel(models.Model):
    """A specific model offered by an :class:`LLMProvider`."""

    provider = models.ForeignKey(LLMProvider, on_delete=models.CASCADE, related_name="models")
    model_id = models.CharField(max_length=100)  # "claude-opus-5" -- the API-facing id
    label = models.CharField(max_length=100)  # "Opus 5"
    context_window = models.IntegerField()
    default_max_tokens = models.IntegerField(default=500)
    input_price_per_mtok_usd = models.DecimalField(max_digits=10, decimal_places=4)
    output_price_per_mtok_usd = models.DecimalField(max_digits=10, decimal_places=4)
    tier = models.CharField(max_length=32, blank=True)  # "flagship" | "balanced" | "fast" | ...
    notes = models.CharField(max_length=255, blank=True)
    sort_order = models.IntegerField(default=0)
    enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "model_id"]
        unique_together = ("provider", "model_id")

    def __str__(self):
        return f"{self.provider_id}:{self.model_id}"


class LLMConfiguration(models.Model):
    """Singleton row holding the active LLM provider/model/key selection.

    Enforced two ways: ``save()`` forces ``pk=1``, and a DB ``CheckConstraint``
    blocks any other pk.
    """

    provider = models.ForeignKey(LLMProvider, on_delete=models.PROTECT, related_name="+")
    model = models.ForeignKey(LLMModel, on_delete=models.PROTECT, related_name="+")
    max_tokens = models.IntegerField()
    # Fernet ciphertext (services/crypto.py). NULL -> callers fall back to the
    # provider's env var (services/llm/config.py).
    encrypted_api_key = models.BinaryField(null=True, blank=True)
    # Last 4 chars of the plaintext key, so "...x7fQ" can be shown without decrypting.
    key_last_four = models.CharField(max_length=4, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [CheckConstraint(check=Q(pk=1), name="single_llm_configuration")]

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls, **defaults):
        """Return the singleton row, creating it (with ``defaults``) if absent."""
        obj, _created = cls.objects.get_or_create(pk=1, defaults=defaults)
        return obj

    def __str__(self):
        return f"LLM config: {self.provider_id}/{self.model.model_id}"


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


# --- Magic-link auth (MUS-37) -------------------------------------------------


class LoginToken(models.Model):
    """A single-use, short-lived magic-link login token (MUS-37).

    Only ``sha256(token)`` is stored -- plain SHA-256 is fine for 256-bit
    CSPRNG output. Single-use is enforced by a conditional UPDATE
    (views_auth.py), so two concurrent consumes cannot both succeed.
    """

    email = models.EmailField(db_index=True)
    # sha256 hexdigest of the raw token; unique so a replayed insert fails at the DB.
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    # NULL until redeemed; set exactly once by the conditional update.
    consumed_at = models.DateTimeField(null=True, blank=True, default=None)
    # Best-effort audit only -- never used for authorization decisions.
    requested_ip = models.GenericIPAddressField(null=True, blank=True)
    requested_user_agent = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["email", "-created_at"], name="logintoken_email_recent"),
            models.Index(fields=["expires_at", "consumed_at"], name="logintoken_sweep"),
        ]

    def __str__(self):
        return f"LoginToken({self.email}, expires {self.expires_at:%Y-%m-%d %H:%M})"


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


# --- Agentic loop (MUS-29) ----------------------------------------------------


class AgentLeadRun(models.Model):
    """The per-lead resume unit of an agentic copy run (MUS-29).

    One row per (trace_run_id, lead). Ownership is taken by an epoch-CAS
    conditional UPDATE: rowcount 1 means the caller owns the run, so racing
    workers produce exactly one winner and dead-worker takeover still works.
    """

    STATUS_PENDING = "pending"
    STATUS_CLAIMED = "claimed"
    STATUS_GATHERING = "gathering"
    STATUS_DRAFTING = "drafting"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"
    STATUS_EXHAUSTED = "exhausted"
    NON_TERMINAL_STATUSES = (STATUS_PENDING, STATUS_CLAIMED, STATUS_GATHERING, STATUS_DRAFTING)

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="agent_runs")
    trace_run_id = models.CharField(max_length=36, db_index=True)
    status = models.CharField(max_length=16, default=STATUS_PENDING)
    claimed_by = models.CharField(max_length=32, blank=True, default="")  # worker token (uuid4 hex)
    claim_epoch = models.IntegerField(default=0)  # CAS counter: bumps on every claim
    steps_used = models.IntegerField(default=0)
    tool_calls_used = models.IntegerField(default=0)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True, default=None)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["trace_run_id", "lead"], name="alr_one_run_per_lead_per_trace"
            ),
        ]
        indexes = [
            models.Index(fields=["trace_run_id", "status"], name="alr_resume_scan"),
        ]

    def __str__(self):
        return f"agent run {self.trace_run_id}/{self.lead_id} [{self.status}]"


class AgentStep(models.Model):
    """Append-only step log: crash-resume checkpoint and reasoning trace in one.

    Payload shapes by ``kind`` are pinned in docs/contracts/agent-loop.md.
    """

    lead_run = models.ForeignKey(AgentLeadRun, on_delete=models.CASCADE, related_name="steps")
    seq = models.IntegerField()
    kind = models.CharField(max_length=16)  # llm_call | tool_result | final
    payload = models.JSONField(default=dict)
    # The same `genai.sha256_of` hashes the OTel spans carry (MUS-25), so a span
    # and a step cross-reference without leaking content.
    request_sha256 = models.CharField(max_length=64, blank=True, default="")
    result_sha256 = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["seq"]
        constraints = [
            models.UniqueConstraint(fields=["lead_run", "seq"], name="astep_one_seq_per_run"),
        ]

    def __str__(self):
        return f"step {self.seq} ({self.kind}) of run {self.lead_run_id}"


class AEAvailabilitySlot(models.Model):
    """Synthetic calendar slots backing the `check_ae_calendar` tool (MUS-29).

    Seeded only by `ingest_data` (idempotent delete-and-recreate of
    `synthetic=True` rows) -- never written by the agent loop.
    """

    ae_name = models.CharField(max_length=100)
    ae_email = models.EmailField()
    slot_start = models.DateTimeField()
    slot_end = models.DateTimeField()
    synthetic = models.BooleanField(default=True)

    class Meta:
        ordering = ["slot_start"]

    def __str__(self):
        return f"{self.ae_name}: {self.slot_start:%Y-%m-%d %H:%M}"


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


# --- Assess next action (MUS-70) ----------------------------------------------


class LeadAssessment(models.Model):
    """One press of "Assess next action": the rules' answer for a lead (MUS-70).

    Its own table, not an OutreachAction: outreach rows back the triage queue,
    so assessments there would pollute every queue query and the dedupe ledger.
    Assess neither suppresses nor is suppressed -- `open_outreach_action` and
    `dismissed` report the queue's state as context, they never gate the answer.
    Append-only: every press writes a row, the history is the point.
    """

    # Advisory (MUS-70 PR 2) statuses. The deterministic half is always correct
    # and always present, so a missing advisory degrades rather than refuses.
    ADVISORY_OK = "ok"
    ADVISORY_DISABLED = "disabled"  # not enabled, or no provider configured
    ADVISORY_UNGROUNDED = "ungrounded"  # verifier rejected it; see `verification`
    ADVISORY_PROVIDER_ERROR = "provider_error"  # never the raw provider text
    ADVISORY_STATUS_CHOICES = [
        (ADVISORY_OK, "Advisory present"),
        (ADVISORY_DISABLED, "Advisory not enabled"),
        (ADVISORY_UNGROUNDED, "Advisory dropped — ungrounded"),
        (ADVISORY_PROVIDER_ERROR, "Advisory dropped — provider error"),
    ]

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="assessments")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # The rules' answer. Authoritative: the advisory may never change these.
    action_type = models.CharField(max_length=64)
    priority = models.IntegerField()
    reason = models.TextField()

    # The whole v1 envelope from services/outreach.py::explain(). Never
    # recomputed: its relative figures are only true as of `rule_trace.today`.
    rule_trace = models.JSONField(default=dict, blank=True)

    advisory_text = models.TextField(blank=True, default="")
    advisory_status = models.CharField(
        max_length=32, choices=ADVISORY_STATUS_CHOICES, default=ADVISORY_DISABLED
    )
    # verify.py report for `advisory_text`; {} when no advisory was verified.
    verification = models.JSONField(default=dict, blank=True)

    # Cost and audit for the advisory call. Blank on a deterministic-only row.
    provider = models.CharField(max_length=32, blank=True, default="")
    model_id = models.CharField(max_length=100, blank=True, default="")
    trace_run_id = models.CharField(max_length=36, blank=True, default="", db_index=True)

    # The queue's state at assess time, reported not obeyed. SET_NULL: the
    # assessment outlives the row it observed.
    open_outreach_action = models.ForeignKey(
        OutreachAction, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    dismissed = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["lead", "-created_at"], name="la_lead_recent"),
        ]

    def __str__(self):
        return f"assessment of {self.lead_id} - {self.action_type} (p{self.priority})"
