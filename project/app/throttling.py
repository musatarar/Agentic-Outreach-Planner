"""Rate limits for the sign-in endpoints (MUS-37).

Two independent caps: per IP (DRF's ``ScopedRateThrottle``, declared on the
view) and per recipient email (this module). Both run before the view body,
so a 429 cannot be used to enumerate the allowlist.
"""

from __future__ import annotations

import hashlib
from typing import Any

from django.conf import settings
from rest_framework.request import Request
from rest_framework.throttling import SimpleRateThrottle

EMAIL_THROTTLE_SCOPE = "auth_request_email"


class LoginEmailRateThrottle(SimpleRateThrottle):
    """Cap login-link requests per *recipient address*, across all clients."""

    scope = EMAIL_THROTTLE_SCOPE

    def get_rate(self) -> str:
        return str(settings.LOGIN_RATE_LIMIT_EMAIL)

    def get_cache_key(self, request: Request, view: Any) -> str | None:
        """Bucket by a hash of the normalised address.

        Hashed so the throttle cache never accumulates raw addresses. ``None``
        (no email in the body) means "do not throttle" -- the view 400s it anyway.
        """
        data = request.data if isinstance(request.data, dict) else {}
        email = str(data.get("email") or "").strip().lower()
        if not email:
            return None
        digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
        return self.cache_format % {"scope": self.scope, "ident": digest}
