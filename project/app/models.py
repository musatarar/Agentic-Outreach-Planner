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
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_SNOOZED, "Snoozed"),
        (STATUS_DISMISSED, "Dismissed"),
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
    # `invalid_transition` (CONTRACT MUS-35 section 5.3) -- never a silent
    # no-op, so "approve a dismissed action" is an error the caller sees.
    #   pending    -> approved | snoozed | dismissed   (triage decisions)
    #   snoozed    -> approved | snoozed | dismissed   (re-snooze is allowed)
    #              -> pending                          (undo / unsnooze_due)
    #   approved   -> pending                          (undo only)
    #   dismissed  -> pending                          (undo only, and it must
    #                                                   revoke the suppression)
    ALLOWED_TRANSITIONS = {
        STATUS_PENDING: (STATUS_APPROVED, STATUS_SNOOZED, STATUS_DISMISSED),
        STATUS_SNOOZED: (STATUS_APPROVED, STATUS_SNOOZED, STATUS_DISMISSED, STATUS_PENDING),
        STATUS_APPROVED: (STATUS_PENDING,),
        STATUS_DISMISSED: (STATUS_PENDING,),
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
        has to write ``edited_copy or suggested_copy`` (CONTRACT section 9.11).
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
    STATUS_RESOLVED = "resolved"
    STATUS_PENDING = "pending_engineering"

    # OneToOne: an action is resolved by exactly one decision; the DB-level
    # unique constraint blocks duplicate/racing submissions for the same action.
    outreach_action = models.OneToOneField(
        OutreachAction, on_delete=models.CASCADE, related_name="review_decision"
    )
    kind = models.CharField(max_length=32)  # select_existing | propose_new
    status = models.CharField(max_length=32)  # resolved | pending_engineering
    selected_action_type = models.CharField(max_length=64, blank=True)
    proposed_name = models.CharField(max_length=255, blank=True)
    proposed_what = models.TextField(blank=True)
    proposed_when = models.TextField(blank=True)
    reviewer = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


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
