"""Workspace resolution and membership writes.

One tenant per user in this iteration: a caller's workspace is the single
:class:`~project.app.models.TenantMembership` row on their user. Everything
that needs to know "whose data is this?" asks here, so there is one answer.

``user_for_email`` lives here rather than on the sign-in view because the
membership commands create users too, and both paths must create them
identically.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import transaction

from project.app.models import Tenant, TenantMembership


def tenant_for_user(user) -> Tenant | None:
    """The workspace this user belongs to, or ``None``.

    One query. ``None`` is a fail-closed answer everywhere it is consumed: the
    API returns 403 ``no_tenant`` rather than falling back to "all leads".
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    membership = TenantMembership.objects.select_related("tenant").filter(user=user).first()
    return membership.tenant if membership is not None else None


def create_tenant(slug: str, name: str | None = None) -> Tenant:
    """Create the workspace, or return the existing one; idempotent on slug.

    A ``name`` is applied when given, so re-running the command renames rather
    than silently ignoring the new value.
    """
    tenant, created = Tenant.objects.get_or_create(slug=slug, defaults={"name": name or slug})
    if not created and name and tenant.name != name:
        tenant.name = name
        tenant.save(update_fields=["name"])
    return tenant


def user_for_email(email: str):
    """Fetch or create the Django user for an address.

    Created on first sign-in so the allowlist stays the single source of truth;
    the password is unusable because there is no password login path. Moved
    here from ``AuthConsumeView._user_for`` unchanged, so the membership
    commands and sign-in create users the same way.
    """
    user_model = get_user_model()
    user = user_model.objects.filter(username=email).first()
    if user is not None:
        return user
    user = user_model(username=email, email=email)
    user.set_unusable_password()
    user.save()
    return user


def add_member(tenant: Tenant, email: str) -> TenantMembership:
    """Enrol ``email`` in ``tenant``, creating the user if needed.

    Idempotent for a user already in this tenant. Raises ``ValueError`` when
    the user already belongs to a *different* tenant: moving someone between
    workspaces changes what they can see, so it is never a side effect.
    """
    with transaction.atomic():
        user = user_for_email(email)
        existing = TenantMembership.objects.select_related("tenant").filter(user=user).first()
        if existing is not None:
            if existing.tenant_id != tenant.id:
                raise ValueError(
                    f"{email} already belongs to workspace "
                    f'"{existing.tenant.slug}"; refusing to move them to "{tenant.slug}".'
                )
            return existing
        return TenantMembership.objects.create(user=user, tenant=tenant)
