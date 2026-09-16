"""Request-level permissions.

:class:`HasTenant` is the one thing standing between a signed-in user and
another workspace's leads: it resolves the caller's tenant onto the request so
every view downstream can filter by it, and fails closed when there is none.
"""

from __future__ import annotations

from typing import Any

from rest_framework import status
from rest_framework.permissions import BasePermission

from project.app.exceptions import ContractError
from project.app.services.tenancy import tenant_for_user

NO_TENANT_DETAIL = "Your account is not assigned to a workspace."


class HasTenant(BasePermission):
    """Resolve the caller's tenant onto ``request.tenant``, or 403 ``no_tenant``.

    Ordered *after* ``IsAuthenticated`` on every view that uses it, so an
    anonymous caller still gets the 401 the frontend's route guard expects.
    A signed-in user with no membership sees nothing: there is no
    "unassigned" book of leads to fall back to.
    """

    def has_permission(self, request: Any, view: Any) -> bool:
        tenant = tenant_for_user(getattr(request, "user", None))
        if tenant is None:
            raise ContractError(
                "no_tenant",
                NO_TENANT_DETAIL,
                status_code=status.HTTP_403_FORBIDDEN,
            )
        request.tenant = tenant
        return True
