"""The single error envelope every non-2xx response in this API uses (MUS-37).

Every failure is ``{"code": "machine_slug", "detail": "sentence"}``. Views
raise :class:`ContractError` when they know the code; everything else is
mapped from the HTTP status here, so a code can never be missing.
"""

from __future__ import annotations

import math
from typing import Any

from rest_framework import status
from rest_framework.exceptions import APIException, PermissionDenied, Throttled
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

#: Fallback ``code`` by HTTP status, for exceptions raised outside our own views.
STATUS_FALLBACK_CODES = {
    status.HTTP_400_BAD_REQUEST: "validation_error",
    status.HTTP_401_UNAUTHORIZED: "not_authenticated",
    status.HTTP_403_FORBIDDEN: "permission_denied",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
    status.HTTP_406_NOT_ACCEPTABLE: "not_acceptable",
    status.HTTP_409_CONFLICT: "invalid_transition",
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: "unsupported_media_type",
    status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
}

DEFAULT_CODE = "error"
DEFAULT_DETAIL = "Something went wrong."
DEFAULT_THROTTLE_DETAIL = "Too many requests."


class ContractError(APIException):
    """An error whose ``code`` is pinned by the contract rather than inferred."""

    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(
        self,
        code: str,
        detail: str,
        status_code: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.contract_code = code
        self.extra: dict[str, Any] = dict(extra or {})
        if status_code is not None:
            self.status_code = status_code
        super().__init__(detail)


def contract_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """Wrap DRF's handler and normalise its four output shapes into one."""
    response = drf_exception_handler(exc, context)
    if response is None:
        # Not a DRF-handled exception -- let Django's 500 path deal with it.
        return None

    code = getattr(exc, "contract_code", None) or _fallback_code(exc, response.status_code)
    detail = _detail_sentence(exc, response.data, context)

    body: dict[str, Any] = {"code": code, "detail": detail}
    body.update(getattr(exc, "extra", None) or {})

    if isinstance(exc, Throttled) and exc.wait is not None:
        # Mirrors the `Retry-After` header: browser fetch() cannot always read headers.
        body["retry_after"] = int(math.ceil(exc.wait))

    response.data = body
    return response


def _fallback_code(exc: Exception, status_code: int) -> str:
    if isinstance(exc, PermissionDenied) and "CSRF" in str(exc.detail):
        # Django's CSRF rejection surfaces as PermissionDenied with the reason in the message.
        return "csrf_failed"
    return STATUS_FALLBACK_CODES.get(status_code, DEFAULT_CODE)


def _detail_sentence(exc: Exception, data: Any, context: dict[str, Any]) -> str:
    if isinstance(exc, Throttled):
        return _throttle_sentence(exc, context)
    return _flatten_detail(data) or DEFAULT_DETAIL


def _throttle_sentence(exc: Throttled, context: dict[str, Any]) -> str:
    """Say which limit was hit and when to come back, in that order."""
    view = context.get("view")
    lead = getattr(view, "throttle_detail", None) or DEFAULT_THROTTLE_DETAIL
    if exc.wait is None:
        return lead
    seconds = int(math.ceil(exc.wait))
    if seconds >= 60:
        minutes = int(math.ceil(seconds / 60))
        unit = "minute" if minutes == 1 else "minutes"
        return f"{lead} Try again in {minutes} {unit}."
    unit = "second" if seconds == 1 else "seconds"
    return f"{lead} Try again in {seconds} {unit}."


def _flatten_detail(data: Any) -> str:
    """Reduce any of DRF's error payload shapes to one sentence."""
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        if "detail" in data:
            return _flatten_detail(data["detail"])
        parts = []
        for field, value in data.items():
            message = _flatten_detail(value)
            if message:
                parts.append(message if field == "non_field_errors" else f"{field}: {message}")
        return " ".join(parts)
    if isinstance(data, (list, tuple)):
        for item in data:
            message = _flatten_detail(item)
            if message:
                return message
    return ""
