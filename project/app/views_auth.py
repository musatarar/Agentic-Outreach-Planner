"""Magic-link sign-in endpoints (MUS-37).

Four endpoints, pinned by contract MUS-35 section 5.1:

* ``POST /api/auth/request-link/`` -- mint and deliver a link. Always 200.
* ``POST /api/auth/consume/``      -- redeem it, establish the session.
* ``POST /api/auth/logout/``       -- flush the session.
* ``GET  /api/auth/me/``           -- who am I (401 when nobody).

Why magic links at all: one operator, no roles, no invites. That means no
password hashing, no reset flow, no credential rotation and no second factor
to bolt on later -- strictly less code *and* strictly less risk than
passwords for this product.

Two properties in here are worth more than the rest of the module:

**request-link is not an account-enumeration oracle.** The response is the
same shape, the same status and (outside DEBUG+console) byte-identical
whether or not the address is allowlisted, and the non-allowlisted branch
burns an equivalent token generation + hash so wall-clock timing matches too.
Rate limiting is applied by DRF *before* this view body runs, so a 429 cannot
be used to enumerate either.

**``dev_link`` leaking in production is a full auth bypass.** It is populated
only when ``DEBUG`` is True *and* delivery is ``console`` *and* the address is
allowlisted. All three, every time.

New module rather than additions to ``views.py`` -- see contract section 8.1;
that separation is the whole conflict-avoidance strategy for this wave.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from project.app.exceptions import ContractError
from project.app.serializers_auth import ConsumeTokenSerializer, RequestLinkSerializer
from project.app.services import login_links
from project.app.services.login_links import ConsumeOutcome
from project.app.throttling import LoginEmailRateThrottle

INVALID_EMAIL_DETAIL = "Enter a valid email address."
INVALID_TOKEN_DETAIL = "This sign-in link is not valid."
EXPIRED_TOKEN_DETAIL = "This sign-in link has expired. Request a new one."
NOT_AUTHENTICATED_DETAIL = "Authentication credentials were not provided."


def _client_ip(request: Request) -> str | None:
    """Best-effort client IP for the audit columns.

    ``REMOTE_ADDR`` only, deliberately: ``X-Forwarded-For`` is attacker-controlled
    unless a trusted proxy is known to rewrite it, and ``requested_ip`` is
    audit data that must never influence an authorization decision.
    """
    return request.META.get("REMOTE_ADDR") or None


def _iso_z(value: datetime) -> str:
    """Render a datetime the way the contract's example bodies do."""
    return value.isoformat().replace("+00:00", "Z")


class AuthRequestLinkView(APIView):
    """``POST /api/auth/request-link/`` -- ask for a sign-in link.

    Always 200 for a syntactically valid address. See the module docstring
    for why that is not negotiable.
    """

    permission_classes = [AllowAny]
    # Two independent caps: per source IP (scoped, DRF's own) and per
    # recipient address. Neither alone is enough -- see throttling.py. Both
    # run in DRF's initial(), i.e. before the allowlist check below, so a 429
    # cannot be used to enumerate the allowlist either.
    throttle_classes = [ScopedRateThrottle, LoginEmailRateThrottle]
    throttle_scope = "auth_request_ip"
    throttle_detail = "Too many login links requested."

    def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        serializer = RequestLinkSerializer(data=request.data)
        if not serializer.is_valid():
            raise ContractError("invalid_email", INVALID_EMAIL_DETAIL)

        email = login_links.normalize_email(serializer.validated_data["email"])
        dev_link: str | None = None

        if login_links.is_allowed(email):
            issued = login_links.issue_login_link(
                email,
                ip=_client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )
            login_links.deliver_login_link(issued)
            if settings.DEBUG and settings.LOGIN_LINK_DELIVERY == login_links.DELIVERY_CONSOLE:
                dev_link = issued.link
        else:
            # No row, no delivery -- but the same CSPRNG draw and the same
            # hash, so the two branches cost the same wall-clock time.
            login_links.burn_discard_token()

        return Response(
            {
                "status": "sent",
                "expires_in": int(settings.LOGIN_TOKEN_TTL_SECONDS),
                "resend_after": int(settings.LOGIN_RESEND_COOLDOWN_SECONDS),
                "dev_link": dev_link,
            },
            status=status.HTTP_200_OK,
        )


class AuthConsumeView(APIView):
    """``POST /api/auth/consume/`` -- redeem a link and establish the session.

    ``expired`` and ``invalid`` are distinguished on purpose: the sign-in page
    needs a third state that offers "send me a new one" instead of "that link
    is wrong". It is safe because holding a token already implies a link was
    once issued; it says nothing about which addresses exist.
    """

    permission_classes = [AllowAny]
    throttle_scope = "auth_consume_ip"
    throttle_detail = "Too many sign-in attempts."

    def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        serializer = ConsumeTokenSerializer(data=request.data)
        if not serializer.is_valid():
            raise ContractError("invalid_token", INVALID_TOKEN_DETAIL)

        outcome, login_token = login_links.consume_login_token(serializer.validated_data["token"])
        if outcome is ConsumeOutcome.EXPIRED:
            raise ContractError("expired_token", EXPIRED_TOKEN_DETAIL)
        if outcome is ConsumeOutcome.INVALID or login_token is None:
            raise ContractError("invalid_token", INVALID_TOKEN_DETAIL)

        email = login_token.email
        if not login_links.is_allowed(email):
            # The allowlist can shrink between issue and redeem. Revoking
            # access must not require hunting down outstanding links.
            raise ContractError("invalid_token", INVALID_TOKEN_DETAIL)

        user = self._user_for(email)
        # login() rotates the CSRF token (see contract section 9.12) -- the client
        # must re-read the `csrftoken` cookie after this response.
        django_login(request, user)

        return Response(
            {
                "authenticated": True,
                "email": email,
                "session_expires_at": _iso_z(request.session.get_expiry_date()),
            },
            status=status.HTTP_200_OK,
        )

    @staticmethod
    def _user_for(email: str):
        """Fetch or create the Django user for an allowlisted address.

        Created on first successful sign-in rather than up front, so the
        allowlist stays the single source of truth about who may sign in and
        there is no user table to keep in step with it. The password is
        unusable: there is no password login path to attack.
        """
        user_model = get_user_model()
        user = user_model.objects.filter(username=email).first()
        if user is not None:
            return user
        user = user_model(username=email, email=email)
        user.set_unusable_password()
        user.save()
        return user


class AuthLogoutView(APIView):
    """``POST /api/auth/logout/`` -- flush the session.

    POST rather than GET so CSRF applies: a GET logout is trivially triggered
    by any third-party page embedding an image tag.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        django_logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class AuthMeView(APIView):
    """``GET /api/auth/me/`` -- the route guard's one question.

    ``AllowAny`` at the DRF level and raising the 401 by hand, so the frontend
    can call it on every page load without the global 401 handler firing
    recursively on its own probe.
    """

    permission_classes = [AllowAny]

    def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        if not request.user.is_authenticated:
            raise ContractError(
                "not_authenticated",
                NOT_AUTHENTICATED_DETAIL,
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        return Response(
            {"authenticated": True, "email": request.user.email or request.user.get_username()},
            status=status.HTTP_200_OK,
        )
