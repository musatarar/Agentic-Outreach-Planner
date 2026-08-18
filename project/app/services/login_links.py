"""Issue and consume magic-link login tokens (MUS-37).

Only ``sha256(token)`` is stored — a database read must not hand out a working
credential (plain SHA-256 is fine: no dictionary attacks 256 random bits).
Redemption is a single conditional ``UPDATE``, so concurrent redemptions race
in the database and exactly one wins. Delivery defaults to console; nothing in
the demo path requires SMTP.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from urllib.parse import urlencode

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from project.app.models import LoginToken

logger = logging.getLogger(__name__)

#: Bytes of entropy handed to ``secrets.token_urlsafe``. 32 bytes = 256 bits.
TOKEN_BYTES = 32

#: Path of the React route that redeems a token (see MUS-38's ConsumePage).
CONSUME_PATH = "/auth/consume"

DELIVERY_CONSOLE = "console"
DELIVERY_EMAIL = "email"

_EMAIL_SUBJECT = "Your sign-in link"
_EMAIL_BODY = (
    "Use the link below to sign in. It expires in {minutes} minutes and works only once.\n"
    "\n"
    "{link}\n"
    "\n"
    "If you did not request this, you can ignore this email -- no one can sign in without\n"
    "the link itself.\n"
)


class ConsumeOutcome(str, Enum):
    """Result of attempting to redeem a raw token.

    ``EXPIRED`` vs ``INVALID`` is deliberate: the sign-in page needs an
    "ask for a new link" state, and it leaks nothing the token holder
    doesn't already know.
    """

    OK = "ok"
    EXPIRED = "expired"
    INVALID = "invalid"


@dataclass(frozen=True)
class IssuedLink:
    """A freshly minted link. ``raw_token`` exists only in memory, never in the DB."""

    login_token: LoginToken
    raw_token: str
    link: str
    expires_in: int


def hash_token(raw_token: str) -> str:
    """Return the stored representation of ``raw_token``."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def generate_raw_token() -> str:
    """Return a fresh URL-safe token with :data:`TOKEN_BYTES` bytes of entropy."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def normalize_email(email: str) -> str:
    """Casefold and strip an address so allowlisting and rate limiting agree."""
    return email.strip().lower()


def is_allowed(email: str) -> bool:
    """True when ``email`` may be sent a link at all (no signup flow; the
    allowlist decides). The caller must NOT vary its response on this -- see
    ``views.auth.AuthRequestLinkView``."""
    return normalize_email(email) in settings.LOGIN_ALLOWED_EMAILS


def build_link(raw_token: str) -> str:
    """Absolute URL a recipient clicks to redeem ``raw_token``."""
    base = str(settings.LOGIN_LINK_BASE_URL).rstrip("/")
    return f"{base}{CONSUME_PATH}?{urlencode({'token': raw_token})}"


def burn_discard_token() -> str:
    """Generate and hash a token that is thrown away.

    Called on the non-allowlisted branch of ``request-link`` so response
    timing cannot be used to enumerate the allowlist.
    """
    discard = generate_raw_token()
    return hash_token(discard)


def issue_login_link(
    email: str,
    *,
    ip: str | None = None,
    user_agent: str = "",
) -> IssuedLink:
    """Mint, persist (hashed) and return a single-use login link for ``email``."""
    raw_token = generate_raw_token()
    ttl = int(settings.LOGIN_TOKEN_TTL_SECONDS)
    login_token = LoginToken.objects.create(
        email=normalize_email(email),
        token_hash=hash_token(raw_token),
        expires_at=timezone.now() + timedelta(seconds=ttl),
        requested_ip=ip,
        requested_user_agent=(user_agent or "")[:255],
    )
    return IssuedLink(
        login_token=login_token,
        raw_token=raw_token,
        link=build_link(raw_token),
        expires_in=ttl,
    )


def deliver_login_link(issued: IssuedLink) -> None:
    """Hand ``issued`` to the configured delivery channel.

    Delivery failures are logged, never raised: a different HTTP response
    would re-open the enumeration oracle.
    """
    if settings.LOGIN_LINK_DELIVERY == DELIVERY_EMAIL:
        minutes = max(1, int(settings.LOGIN_TOKEN_TTL_SECONDS) // 60)
        try:
            send_mail(
                subject=_EMAIL_SUBJECT,
                message=_EMAIL_BODY.format(minutes=minutes, link=issued.link),
                from_email=None,
                recipient_list=[issued.login_token.email],
                fail_silently=False,
            )
        except Exception:
            logger.exception("Failed to email a login link to %s", issued.login_token.email)
        return

    # console (default): the link goes to the server log and nowhere else.
    logger.info(
        "Magic sign-in link for %s (expires in %ss): %s",
        issued.login_token.email,
        issued.expires_in,
        issued.link,
    )


def consume_login_token(raw_token: str) -> tuple[ConsumeOutcome, LoginToken | None]:
    """Redeem ``raw_token`` exactly once.

    A single conditional ``UPDATE``: the database, not application logic,
    decides the winner when the same link is opened twice at once.
    """
    digest = hash_token(raw_token)
    now = timezone.now()
    updated = LoginToken.objects.filter(
        token_hash=digest, consumed_at__isnull=True, expires_at__gt=now
    ).update(consumed_at=now)
    if updated != 1:
        # Only an unconsumed row merely past its TTL earns the friendlier
        # "expired" message.
        expired = LoginToken.objects.filter(
            token_hash=digest, consumed_at__isnull=True, expires_at__lte=now
        ).exists()
        return (ConsumeOutcome.EXPIRED if expired else ConsumeOutcome.INVALID), None

    login_token = LoginToken.objects.filter(token_hash=digest).first()
    if login_token is None:  # pragma: no cover - the row was just updated
        return ConsumeOutcome.INVALID, None
    return ConsumeOutcome.OK, login_token
