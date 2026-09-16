"""Workspaces: who owns a book of leads, and which user belongs to which one.

A ``Tenant`` is a workspace. Tenancy is data, not configuration: no
environment variable selects one. Membership is one tenant per user in this
iteration (:class:`TenantMembership` is a OneToOne on the user), so a caller's
tenant is unambiguous without a header or a session value.
"""

from django.conf import settings
from django.db import models


class Tenant(models.Model):
    """A workspace that owns leads and their events."""

    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["slug"]

    def __str__(self):
        return f"{self.slug} - {self.name}"


class TenantMembership(models.Model):
    """The single row that resolves a user to their workspace.

    OneToOne on purpose: multi-workspace users need a selector in the UI and a
    per-request choice on the API, which is a follow-up. Until then the
    resolution must never be ambiguous.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tenant_membership",
    )
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="memberships")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user_id} in {self.tenant_id}"
