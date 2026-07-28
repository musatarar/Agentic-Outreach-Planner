"""Issue and consume magic-link login tokens (MUS-37).

Two operations, and the security of the whole sign-in flow lives in them:

* :func:`issue_login_link` mints ``secrets.token_urlsafe(32)`` (256 bits of
  CSPRNG output), persists only ``sha256(token).hexdigest()``, and returns the
  raw token to the caller exactly once. Storing a hash rather than the token
  is the same reasoning as password storage: a database read must not hand an
  attacker a working credential. A plain SHA-256 rather than a slow KDF is
  correct here -- there is no dictionary to attack against 256 random bits,
  and the verify path has to stay cheap enough that it can be *rate limited*
  rather than turned into a DoS amplifier.

* :func:`consume_login_token` redeems a token with a single conditional
  ``UPDATE`` (contract MUS-35 section 5.1), never a read-then-write. Two
  concurrent redemptions of the same link therefore race in the database and
  exactly one wins; the loser gets ``INVALID`` and no session.

Delivery is deliberately stubbed: ``LOGIN_LINK_DELIVERY=console`` (the
default, and the demo path) writes the link to the server log, while
``email`` hands it to Django's configured email backend. Nothing in the demo
path requires SMTP.
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

    ``EXPIRED`` is distinguished from ``INVALID`` on purpose: the sign-in page
    needs a third state that says "ask for a new link" rather than "that link
    is wrong". It leaks only that *some* link for *some* address once existed,
    which whoever is holding the token already knows.
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
    """True when ``email`` may be sent a link at all.

    There is no signup flow, so an allowlist is what decides who can ask for
    one. The caller must NOT vary its response on this -- see
    ``views_auth.AuthRequestLinkView`` and contract section 9.18.
    """
    return normalize_email(email) in settings.LOGIN_ALLOWED_EMAILS


def build_link(raw_token: str) -> str:
    """Absolute URL a recipient clicks to redeem ``raw_token``."""
    base = str(settings.LOGIN_LINK_BASE_URL).rstrip("/")
    return f"{base}{CONSUME_PATH}?{urlencode({'token': raw_token})}"


def burn_discard_token() -> str:
    """Generate and hash a token that is thrown away.

    Called on the non-allowlisted branch of ``request-link`` so that the
    wall-clock cost of "we sent you a link" matches the cost of "we did
    nothing", and response timing cannot be used to enumerate the allowlist.
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

    ``console`` logs it (the demo path -- no SMTP anywhere). ``email`` uses
    Django's email backend. Delivery failures are logged, never raised: a
    bounced link must not turn into a different HTTP response, because that
    would re-open the enumeration oracle the identical-response rule closes.
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

    The redemption is a single conditional ``UPDATE`` -- ``filter(unconsumed,
    unexpired).update(consumed_at=now)`` -- so the database, not application
    logic, decides the winner when the same link is opened twice at once.
    ``updated`` is the row count: 1 means this caller redeemed it, 0 means
    someone (or something) else did, or it never existed, or it had expired.
    """
    digest = hash_token(raw_token)
    now = timezone.now()
    updated = LoginToken.objects.filter(
        token_hash=digest, consumed_at__isnull=True, expires_at__gt=now
    ).update(consumed_at=now)
    if updated != 1:
        # Distinguish "expired" from "unknown / already used" for the /signin
        # expired state. An unconsumed row that is merely past its TTL is the
        # only case that earns the friendlier message.
        expired = LoginToken.objects.filter(
            token_hash=digest, consumed_at__isnull=True, expires_at__lte=now
        ).exists()
        return (ConsumeOutcome.EXPIRED if expired else ConsumeOutcome.INVALID), None

    login_token = LoginToken.objects.filter(token_hash=digest).first()
    if login_token is None:  # pragma: no cover - the row was just updated
        return ConsumeOutcome.INVALID, None
    return ConsumeOutcome.OK, login_token
