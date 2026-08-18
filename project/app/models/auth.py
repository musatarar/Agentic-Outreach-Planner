"""Magic-link auth (MUS-37)."""

from django.db import models


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
