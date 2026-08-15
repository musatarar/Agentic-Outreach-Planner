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
    # Set on EVERY status write, including a re-snooze of an already-snoozed
    # item. Drives both the undo window and the reverse-chron /done ordering.
    status_changed_at = models.DateTimeField(null=True, blank=True, default=None)

    # The reviewer's edit of `suggested_copy`. "" means never edited.
    # `suggested_copy` is IMMUTABLE -- the eval corpus depends on being able to
    # diff the model's original output against what a human actually sent.
    edited_copy = models.TextField(blank=True, default="")

    # Always non-NULL while status == "snoozed", including for on_activity
    # (which uses a backstop -- see TRIAGE_SNOOZE_ON_ACTIVITY_BACKSTOP_DAYS),
    # so the unsnooze sweep and the FE ordering never need a NULL branch.
    snooze_until = models.DateTimeField(null=True, blank=True, default=None, db_index=True)
    snooze_trigger = models.CharField(
        max_length=16, choices=SNOOZE_TRIGGER_CHOICES, blank=True, default=""
    )
    # Watermark for trigger == "on_activity": the item returns to the queue as
    # soon as the lead has an Event with timestamp > this value. Captured at
    # snooze time so later events (not historical ones) are what wake it.
    snooze_activity_after = models.DateTimeField(null=True, blank=True, default=None)

    dismiss_reason = models.CharField(max_length=64, blank=True, default="")

    # Stable identity of "this recommendation for this lead", used to suppress
    # a permanently dismissed recommendation on later plan_outreach() runs and
    # to stop a re-run duplicating an already-open item. See DismissedOutreachKey.
    dedupe_key = models.CharField(max_length=128, blank=True, default="", db_index=True)

    # The planner run that produced this row -- a UUID, also carried on that
    # run's trace as `outreach.run.id` (MUS-25). One indexed column, and it
    # exists to resolve a genuine ordering problem rather than for reporting.
    #
    # A lead's span has to close when that lead's work ends: at concurrency 8
    # over 200 leads, holding every lead span open until the run finishes would
    # give the lead processed at t=0 a 45-second span and destroy the per-lead
    # latency signal entirely. But the span wants to reference the row it
    # produced, and no primary key exists until the write at the *end* of the
    # run. `trace_run_id` is the identity that is known *before* the row exists:
    # the span records `outreach_action:{trace_run_id}:{lead_id}` and closes on
    # time, and the row is resolvable later by
    # `.get(trace_run_id=..., lead_id=...)`.
    #
    # Blank on rows written before this field existed, and on any write that
    # does not come from a planner run.
    trace_run_id = models.CharField(max_length=36, blank=True, default="", db_index=True)

    # Structured rule trace snapshot (schema v1, section 3), produced by
    # MUS-42's services/outreach.py::explain(). A SNAPSHOT: never recomputed
    # after creation, because every relative figure in it ("28d since last
    # contact") is only true as of `trace.today`.
    rule_trace = models.JSONField(default=dict, blank=True)

    # Verification report (schema v1, section 4) for `effective_copy`, produced
    # by MUS-42's services/verify.py::verify_spans(). Rewritten on every /edit/
    # and /approve/; regenerated, not appended.
    verification = models.JSONField(default=dict, blank=True)

    # The state machine, in one place. Anything not listed here is a 409
    # `invalid_transition` -- never a silent
    # no-op, so "approve a dismissed action" is an error the caller sees.
    #   pending    -> approved | snoozed | dismissed   (triage decisions)
    #   snoozed    -> approved | snoozed | dismissed   (re-snooze is allowed)
    #              -> pending                          (undo / unsnooze_due)
    #   approved   -> pending                          (undo only)
    #              -> sent                             (dispatch, MUS-29)
    #   dismissed  -> pending                          (undo only, and it must
    #                                                   revoke the suppression)
    #   sent       -> (nothing)                        (terminal -- no undo
    #                                                   past a send)
    ALLOWED_TRANSITIONS = {
        STATUS_PENDING: (STATUS_APPROVED, STATUS_SNOOZED, STATUS_DISMISSED),
        STATUS_SNOOZED: (STATUS_APPROVED, STATUS_SNOOZED, STATUS_DISMISSED, STATUS_PENDING),
        STATUS_APPROVED: (STATUS_PENDING, STATUS_SENT),
        STATUS_DISMISSED: (STATUS_PENDING,),
        STATUS_SENT: (),
    }

    # Statuses whose copy a reviewer may still change. Editing is not a status
    # transition, so it needs its own guard.
    EDITABLE_STATUSES = (STATUS_PENDING, STATUS_SNOOZED)

    def can_transition_to(self, new_status):
        """True when `new_status` is a legal next state from the current one."""
        return new_status in self.ALLOWED_TRANSITIONS.get(self.status, ())

    @property
    def effective_copy(self):
        """The copy that would actually be sent: the edit if there is one.

        Computed server-side and serialized as its own field so no client ever
        has to write ``edited_copy or suggested_copy``.
        """
        return self.edited_copy or self.suggested_copy

    class Meta:
        indexes = [
            models.Index(fields=["status", "priority", "lead"], name="oa_queue_order"),
            models.Index(fields=["status", "-status_changed_at"], name="oa_done_order"),
        ]
        constraints = [
            # A trace's `outreach.output.ref` promises that
            # `.get(trace_run_id=..., lead_id=...)` resolves to one row. It is
            # true by construction -- one WorkItem per lead per run -- but a
            # promise a reader relies on should be enforced by the schema
            # rather than by an invariant several modules away.
            #
            # Partial, because every row written before MUS-25 has
            # trace_run_id="" and they are not unique per lead: an
            # unconditional constraint would fail to apply on any existing
            # database.
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

    Seeded by ``manage.py seed_llm_catalog`` (idempotent). Read-only from the
    API's point of view — the catalog endpoint just serializes these rows.
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

    Enforced as a singleton two ways: ``save()`` always forces ``pk=1``, and a
    DB-level ``CheckConstraint`` blocks any row with a different pk (defence in
    depth against a direct/bulk insert bypassing ``save()``).
    """

    provider = models.ForeignKey(LLMProvider, on_delete=models.PROTECT, related_name="+")
    model = models.ForeignKey(LLMModel, on_delete=models.PROTECT, related_name="+")
    max_tokens = models.IntegerField()
    # Fernet ciphertext of the provider API key (see services/crypto.py). NULL
    # means no key stored in the DB -- callers fall back to the provider's env
    # var (see services/llm/config.py's precedence rules).
    encrypted_api_key = models.BinaryField(null=True, blank=True)
    # Last 4 characters of the plaintext key, kept alongside the ciphertext so
    # the API/admin can show "...x7fQ" without ever decrypting for display.
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

    # ForeignKey, not OneToOne (MUS-29): an action must be able to hold BOTH a
    # resolution decision ("which action type is this?") and a send decision
    # ("may this copy leave the building?"). Per-category uniqueness moved to
    # the two partial constraints below, so duplicate/racing submissions still
    # die at the DB rather than in a read-then-write.
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
    # Snapshot of `effective_copy` at decision time: the exact bytes the human
    # approved, hash-bound so dispatch can refuse to send anything else.
    approved_copy = models.TextField(blank=True, default="")
    approved_body_sha256 = models.CharField(max_length=64, blank=True, default="")
    # Stamped by undo. A voided approval is kept for audit and never
    # authorizes a send.
    voided_at = models.DateTimeField(null=True, blank=True, default=None)

    class Meta:
        constraints = [
            # One resolution per action, ever -- preserves the guarantee the
            # old OneToOne gave the triage flow. Literal kind strings: Meta
            # cannot see the enclosing class namespace. Partial-constraint
            # precedent: oa_one_row_per_lead_per_run.
            models.UniqueConstraint(
                fields=["outreach_action"],
                condition=Q(kind__in=("select_existing", "propose_new")),
                name="rd_one_resolution_per_action",
            ),
            # One LIVE send decision per action: undo voids rather than
            # deletes, so the audit trail stays while the slot frees up.
            models.UniqueConstraint(
                fields=["outreach_action"],
                condition=Q(kind__in=("approve_send", "reject_send")) & Q(voided_at__isnull=True),
                name="rd_one_live_send_per_action",
            ),
        ]


# --- Magic-link auth (MUS-37) -------------------------------------------------


class LoginToken(models.Model):
    """A single-use, short-lived magic-link login token (MUS-37).

    The raw token is NEVER stored: ``secrets.token_urlsafe(32)`` is generated,
    emailed/printed, and only ``sha256(token).hexdigest()`` is persisted. A
    plain SHA-256 (not a slow KDF) is correct here because the token is 256
    bits of CSPRNG output -- there is no dictionary to attack, and the verify
    path must stay cheap enough to be rate-limited rather than DoS'd.

    Single-use is enforced by a conditional UPDATE (see views_auth.py), not by
    read-then-write, so two concurrent consumes cannot both succeed.
    """

    email = models.EmailField(db_index=True)
    # sha256 hexdigest of the raw token -- 64 chars, unique so a replayed
    # insert fails loudly at the DB rather than silently.
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

    Dismiss means "never show me this again". `plan_outreach()` consults this
    table BEFORE generating copy, so a dismissed recommendation costs no LLM
    call on a re-run and creates no row to filter out downstream.

    TABLE, not a JSONField on Lead, because:
      * it needs a UNIQUE index on `dedupe_key` -- the whole point is that a
        second dismissal of the same recommendation is a no-op, enforced by
        the DB rather than by read-modify-write;
      * `plan_outreach()` reads the entire active set once per run
        (`values_list("dedupe_key", flat=True)`) -- one query, O(1) in leads;
      * undo must REVOKE a dismissal, and a revocation needs its own timestamp
        and audit trail, which a blob cannot carry;
      * it outlives the OutreachAction row that created it (SET_NULL), so
        pruning old actions cannot silently resurrect dismissed work.
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
    # Set by undo-of-dismiss. A revoked row no longer suppresses, but is kept
    # so the audit trail shows the dismissal happened and was reversed.
    revoked_at = models.DateTimeField(null=True, blank=True, default=None)

    class Meta:
        ordering = ["-dismissed_at"]

    def __str__(self):
        return f"dismissed {self.lead_id}/{self.action_type}"


class OutreachEdit(models.Model):
    """Append-only log of reviewer edits to generated copy (MUS-39).

    This is the eval corpus: "what did the model write, what did the human
    actually send, and what changed" is the only honest signal we have about
    copy quality, and it is only capturable at the moment of editing.

    TABLE, not a JSONField on OutreachAction, because:
      * it is 1:N -- a reviewer typically edits, re-verifies, edits again, then
        approves, and every intermediate state is corpus-worthy;
      * it is exported by date range for offline scoring, which is a query,
        not a blob scan;
      * the queue list endpoint has a CONSTANT-query-count requirement, and
        diffs are large -- keeping them off the hot row means the queue payload
        never carries them.

    The `diff_ops` payload itself IS a JSONField: it is opaque to SQL, always
    read whole, and its internal shape is versioned rather than queried.
    """

    outreach_action = models.ForeignKey(
        OutreachAction, on_delete=models.CASCADE, related_name="edits"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    editor = models.EmailField(blank=True, default="")

    # Full before/after so the corpus is self-contained and survives any later
    # change to the diff algorithm.
    before_text = models.TextField()
    after_text = models.TextField()

    # difflib.SequenceMatcher opcodes, non-"equal" only. Schema v1:
    # [{"op":"replace","a0":312,"a1":330,"b0":312,"b1":341,
    #   "before":"volume pricing","after":"volume pricing tiers"}, ...]
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

    One row per (trace_run_id, lead) -- the same UUID the planner stamps on
    `OutreachAction.trace_run_id`, so run, action, and span all join on one
    identity. A worker takes ownership with an epoch-CAS conditional UPDATE
    (the same single-winner shape as `LoginToken` consume): read `claim_epoch`,
    then `filter(pk, claim_epoch=seen, status__in=NON_TERMINAL_STATUSES)
    .update(claim_epoch=seen + 1, claimed_by=token, status="claimed")` --
    rowcount 1 means the caller owns the run. Sequential re-claim after a
    crash succeeds (dead-worker takeover); two workers racing from the same
    read epoch produce exactly one winner.
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

    Payload shapes by `kind` (schema in docs/contracts/agent-loop.md):
      llm_call:    {"text", "tool_calls": [{"id", "name", "arguments"}],
                    "provider", "model", "input_tokens", "output_tokens",
                    "raw_finish_reason", "latency_s"}
      tool_result: {"tool_call_id", "name", "result"}  # sanitized + capped
      final:       {"text"}
    """

    lead_run = models.ForeignKey(AgentLeadRun, on_delete=models.CASCADE, related_name="steps")
    seq = models.IntegerField()
    kind = models.CharField(max_length=16)  # llm_call | tool_result | final
    payload = models.JSONField(default=dict)
    # The same `genai.sha256_of` hashes the OTel spans carry (MUS-25), so a
    # span and a step cross-reference without either leaking content.
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

    The dispatch CAS on `OutreachAction.status` is the winner-picker; this
    OneToOne is the schema-level guarantee behind it -- a second send row for
    the same action cannot exist. PROTECT on both FKs: rows that record real
    outbound mail must outlive everything that produced them, so deleting an
    action or decision with a send on it is an error, not a cascade.
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


# --- Run Composer (MUS-47) ----------------------------------------------------


class PlannerRun(models.Model):
    """One operator-driven composer run: scope -> classify -> read -> generate.

    Durable because the product's premise is that the person paying may stop
    between stages, and the gaps are long enough that no session or
    request-scoped object survives them. `GET /api/runs/active/` is what turns
    "I closed the tab" into a resume, which is why "active" is a queryable
    property of the row rather than something the client remembers.

    See docs/adr/run-composer-state.md for why the single-active rule lives in
    the schema and why there are six statuses rather than four.
    """

    STATUS_DRAFT = "draft"
    STATUS_CLASSIFIED = "classified"
    STATUS_READ = "read"
    STATUS_GENERATED = "generated"
    STATUS_COMPLETED = "completed"
    STATUS_DISCARDED = "discarded"

    # The partition the active slot reasons about: every status is in exactly
    # one of these two, and `pr_sentinel_matches_status` below is the schema
    # saying so. `generated` is deliberately ACTIVE -- failed rows stay
    # selectable for retry, so generation cannot be the thing that ends a run.
    # The human closes it.
    ACTIVE_STATUSES = (STATUS_DRAFT, STATUS_CLASSIFIED, STATUS_READ, STATUS_GENERATED)
    TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_DISCARDED)

    # A working run may re-enter the stage it is in -- re-classify after a scope
    # edit, re-read after a provider change, re-generate after selecting more
    # leads -- or move forward, never back. Discard is reachable from every
    # working stage; only a `generated` run may complete. Terminal runs accept
    # nothing, their own status included: reopening one would need the active
    # slot back, and nothing hands it back.
    ALLOWED_TRANSITIONS = {
        STATUS_DRAFT: (STATUS_CLASSIFIED, STATUS_DISCARDED),
        STATUS_CLASSIFIED: (STATUS_CLASSIFIED, STATUS_READ, STATUS_GENERATED, STATUS_DISCARDED),
        STATUS_READ: (STATUS_READ, STATUS_GENERATED, STATUS_DISCARDED),
        STATUS_GENERATED: (STATUS_GENERATED, STATUS_COMPLETED, STATUS_DISCARDED),
        STATUS_COMPLETED: (),
        STATUS_DISCARDED: (),
    }

    status = models.CharField(max_length=16, default=STATUS_DRAFT, db_index=True)
    scope = models.JSONField(default=dict)  # validated through compose.scope.validate_scope

    # True while the run is active, NULL once it is terminal -- two-valued, never
    # False. NULLs are distinct from each other in a unique index on both
    # backends, so `pr_one_active_run` below admits unbounded finished history
    # under NULL while at most one row can hold True. A `False` row would sit
    # outside the partial index entirely: invisible to the exclusion it is
    # supposed to obey, which is why the check constraint rejects it.
    active_sentinel = models.BooleanField(null=True, default=True)

    created_by = models.EmailField(blank=True, default="")  # audit only, never authorization
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True, default=None)
    finished_by = models.EmailField(blank=True, default="")

    # Minted at generate and stamped on every OutreachAction the run writes, so
    # a composer run joins to its rows exactly the way a planner run does.
    trace_run_id = models.CharField(max_length=36, blank=True, default="", db_index=True)
    classify_ms = models.IntegerField(null=True, blank=True, default=None)

    # The provider/model each paid stage actually ran on -- recorded per stage
    # because a run may read on a cheap model and generate on a flagship one.
    read_provider = models.CharField(max_length=32, blank=True, default="")
    read_model = models.CharField(max_length=128, blank=True, default="")
    generate_provider = models.CharField(max_length=32, blank=True, default="")
    generate_model = models.CharField(max_length=128, blank=True, default="")

    # Money is Decimal, never float. NULL rather than 0 is load-bearing: "not
    # priced yet" and "priced at zero" are different answers and the estimate
    # view renders them differently.
    read_cost_estimate_usd = models.DecimalField(
        max_digits=10, decimal_places=4, null=True, blank=True
    )
    read_cost_actual_usd = models.DecimalField(
        max_digits=10, decimal_places=4, null=True, blank=True
    )
    generate_cost_estimate_usd = models.DecimalField(
        max_digits=10, decimal_places=4, null=True, blank=True
    )
    generate_cost_actual_usd = models.DecimalField(
        max_digits=10, decimal_places=4, null=True, blank=True
    )

    # Suggestions `validate_suggestion` threw away, counted so the read summary
    # can say how much of what was paid for did not survive validation.
    discarded_suggestions = models.IntegerField(default=0)

    def can_transition_to(self, new_status):
        """True when `new_status` is a legal next state from the current one.

        `.get(..., ())` rather than `[...]`: a row can carry a status this build
        does not know about (downgrade, hand-edited data), and the failure mode
        there has to be "nothing is legal", not a 500.
        """
        return new_status in self.ALLOWED_TRANSITIONS.get(self.status, ())

    class Meta:
        constraints = [
            # THE single-active rule, as a fact about the database rather than a
            # convention in services/compose/runs.py. `create_run` inserts
            # unconditionally and turns the IntegrityError into a 409 carrying
            # the active run's id; the read-then-write it replaces is the exact
            # race `plan_outreach`'s open_keys and `LoginToken` consume were
            # already bitten by.
            models.UniqueConstraint(
                fields=["active_sentinel"],
                condition=Q(active_sentinel=True),
                name="pr_one_active_run",
            ),
            # Rules out the half-written close: status moved but the sentinel
            # left set (a finished run still holding the slot, which wedges the
            # product with no UI path out), or its mirror, a NULL-sentinel run
            # still reporting itself active to every stage guard while a second
            # run is created alongside it.
            #
            # `active_sentinel__isnull=False` is load-bearing, not redundant.
            # Without it the first disjunct evaluates to NULL for a NULL
            # sentinel, `NULL OR FALSE` is NULL, and SQL rejects a row only when
            # a CHECK is FALSE -- so the mirror half would not exist at all.
            #
            # Statuses are spelled out: Meta cannot see the enclosing class
            # namespace (same reason ReviewDecision's constraints inline their
            # kind strings).
            CheckConstraint(
                check=(
                    Q(
                        active_sentinel=True,
                        active_sentinel__isnull=False,
                        status__in=("draft", "classified", "read", "generated"),
                    )
                    | Q(active_sentinel__isnull=True, status__in=("completed", "discarded"))
                ),
                name="pr_sentinel_matches_status",
            ),
        ]

    def __str__(self):
        return f"run {self.pk} [{self.status}]"


class RunLead(models.Model):
    """One lead's row inside a run: what the rules said, beside what stuck.

    The rules/effective split is the feature, not a normalization accident.
    `rules_*` is written once by classify and never again, so the audit answer
    this product owes -- what the deterministic rules decided, next to what a
    human approved on top of it -- survives every later decision. Deriving
    `effective_priority` from `rules_priority` would make an accepted
    suggestion unrepresentable.
    """

    SUGGESTION_NONE = "none"
    SUGGESTION_PROPOSED = "proposed"
    SUGGESTION_ACCEPTED = "accepted"
    SUGGESTION_REJECTED = "rejected"

    run = models.ForeignKey(PlannerRun, on_delete=models.CASCADE, related_name="run_leads")
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="+")

    # ---- Written once, at classify. Never written again (pinned by a test) ---
    rules_priority = models.IntegerField()
    rules_action = models.CharField(max_length=64)
    rules_reason = models.TextField()
    # `outreach.explain()`'s schema-v1 envelope -- a mapping, the same shape
    # OutreachAction.rule_trace holds, which is what lets MUS-40's RuleTrace
    # component render a composer row unchanged. `default=dict`, not list.
    rule_trace = models.JSONField(default=dict)
    dedupe_key = models.CharField(max_length=128, db_index=True)

    # ---- The rules values, unless a human accepted a suggestion -------------
    effective_priority = models.IntegerField()
    effective_action = models.CharField(max_length=64)
    effective_reason = models.TextField()

    # An open OutreachAction already shares this dedupe_key, so generating again
    # would double-queue the same recommendation.
    already_queued = models.BooleanField(default=False)
    selected = models.BooleanField(default=False)
    generated_action = models.OneToOneField(
        OutreachAction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="run_lead",
    )
    # outreach.failure_kind. Set on a lead whose generation failed; the row
    # stays selectable so retry is a re-run, not a re-classify.
    generation_error = models.CharField(max_length=64, blank=True, default="")

    # The validated suggestion shape, or {} when nothing was ever proposed.
    suggestion = models.JSONField(default=dict, blank=True)
    # A state string, never Python None: a lead the read never reached, one
    # whose provider call failed, and one whose suggestion was discarded all
    # land in "none", so no reader branches three ways on a two-way fact.
    suggestion_state = models.CharField(max_length=16, default=SUGGESTION_NONE)
    suggestion_decided_at = models.DateTimeField(null=True, blank=True, default=None)
    suggestion_decided_by = models.EmailField(blank=True, default="")

    class Meta:
        constraints = [
            # Per (run, lead), NOT per lead: the same lead appearing in this
            # week's run and last week's is the normal case. Re-classify is
            # specified to REPLACE a run's rows rather than add to them, and
            # selection counts, estimates and the generate loop all assume one
            # row per lead -- a duplicate would bill the operator twice for one
            # lead.
            models.UniqueConstraint(fields=["run", "lead"], name="rl_one_row_per_lead_per_run"),
        ]
        indexes = [
            # The one query every stage after classify runs: this run's rows,
            # priority 1 first, tie-broken by lead so the selection table is
            # stable between polls. Same column shape `oa_queue_order` gives the
            # triage queue. `run` leads or the index is no use as a prefix for
            # the per-run filter, and the sort key is `effective_priority`
            # because an accepted suggestion moves a lead and the order follows.
            models.Index(fields=["run", "effective_priority", "lead"], name="rl_selection_order"),
        ]

    def __str__(self):
        return f"run {self.run_id}/{self.lead_id} (p{self.effective_priority})"


class SavedScope(models.Model):
    """A named lead filter an operator can re-select when starting a run.

    `filters` goes through `compose.scope.validate_scope` on write, so a stored
    scope cannot smuggle an unknown key past the validator later.
    """

    # The handle the frontend saves and re-selects by, so two rows called
    # "Dormant CA" would make "load my saved scope" ambiguous with no tiebreak
    # the user can see.
    name = models.CharField(max_length=64, unique=True)
    filters = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.EmailField(blank=True, default="")

    class Meta:
        # Keeps the picker stable without every view remembering to sort.
        ordering = ["name"]

    def __str__(self):
        return self.name
