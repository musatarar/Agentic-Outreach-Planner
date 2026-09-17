"""The user a command works for: the one it was given, or the demo owner."""

from django.conf import settings
from django.contrib.auth import get_user_model

# Used when no owner is named and LOGIN_ALLOWED_EMAILS is empty.
DEFAULT_OWNER_EMAIL = "demo@lockedin.example"


def owner_email(explicit=None):
    """The email to work with: the one given, else the first address allowed to
    sign in, else the demo owner."""
    if explicit:
        return explicit.strip().lower()
    if settings.LOGIN_ALLOWED_EMAILS:
        return sorted(settings.LOGIN_ALLOWED_EMAILS)[0]
    return DEFAULT_OWNER_EMAIL


def get_or_create_owner(email):
    """Fetch or create the owner, matching the magic-link sign-in convention:
    username == email, unusable password."""
    user_model = get_user_model()
    user = user_model.objects.filter(username=email).first()
    if user is not None:
        return user
    user = user_model(username=email, email=email)
    user.set_unusable_password()
    user.save()
    return user


def resolve_owner(explicit=None):
    """The user behind :func:`owner_email`, created on first use."""
    return get_or_create_owner(owner_email(explicit))
