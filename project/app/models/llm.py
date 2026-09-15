"""The LLM catalog and the singleton provider/model/key selection."""

from django.db import models
from django.db.models import CheckConstraint, Q


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
