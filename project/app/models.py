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
