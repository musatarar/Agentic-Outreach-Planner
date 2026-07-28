"""Rate limits for the sign-in endpoints (MUS-37).

Without these, ``POST /api/auth/request-link/`` is a free email relay pointed
at whatever address an attacker types: it accepts any syntactically valid
address, and (by design) tells the caller nothing about whether it was
delivered. Two independent caps close that, and they are independent on
purpose because each one alone leaves a hole:

* **per IP** (``auth_request_ip``, default ``20/hour``) -- stops one host
  spraying links at a list of addresses. DRF's stock ``ScopedRateThrottle``
  does this; the scope is declared on the view.
* **per email** (:class:`LoginEmailRateThrottle`, default ``5/hour``) -- stops
  a distributed set of hosts mailbombing *one* address, which the IP cap
  cannot see. That is the one this module exists for.

Both are applied by DRF in ``initial()``, i.e. **before** the view body and
therefore before the allowlist check, so a 429 says the same thing for an
allowlisted address as for an unknown one and cannot be used to enumerate the
allowlist -- see ``views_auth`` and contract section 5.1.
"""

from __future__ import annotations

import hashlib
from typing import Any

from django.conf import settings
from rest_framework.request import Request
from rest_framework.throttling import SimpleRateThrottle

EMAIL_THROTTLE_SCOPE = "auth_request_email"


class LoginEmailRateThrottle(SimpleRateThrottle):
    """Cap login-link requests per *recipient address*, across all clients.

    The rate is read from ``settings.LOGIN_RATE_LIMIT_EMAIL`` rather than from
    ``DEFAULT_THROTTLE_RATES``, because the recipient cap is a property of the
    sign-in flow and should be tunable without touching the DRF config block.
    """

    scope = EMAIL_THROTTLE_SCOPE

    def get_rate(self) -> str:
        return str(settings.LOGIN_RATE_LIMIT_EMAIL)

    def get_cache_key(self, request: Request, view: Any) -> str | None:
        """Bucket by a hash of the normalised address.

        Hashed, not raw: the throttle cache is the one place a list of every
        address anyone has ever typed into the sign-in box would otherwise
        accumulate, and it is a cache -- often shared, rarely audited.

        Normalised first, so ``Bob@Example.com`` and ``bob@example.com`` are
        one bucket rather than two.

        Returning ``None`` (no email in the body) means "do not throttle" --
        the request is going to 400 in the view anyway, and bucketing every
        malformed body together would let one client's junk lock out
        everyone else's.
        """
        data = request.data if isinstance(request.data, dict) else {}
        email = str(data.get("email") or "").strip().lower()
        if not email:
            return None
        digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
        return self.cache_format % {"scope": self.scope, "ident": digest}
