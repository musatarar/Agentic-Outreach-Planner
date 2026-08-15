"""Run Composer endpoints (MUS-47).

Every route is wired in ``urls.py`` from the skeleton onward, and a view its component
has not landed yet raises ``NotImplementedError`` rather than 404ing. That is deliberate:
a route that does not exist answers an endpoint test with the same 404 a typo'd path
gives, so the test would pass or fail for reasons unrelated to what it specifies. Wiring
up front means a red endpoint test fails on its own assertion, and the component PR that
fills the view in is the only diff that has to change.

The scope-owned views are live (MUS-47 component 2); the rest still carry their stubs.

Error envelope: ``views_queue.error()``, i.e. ``{"code": ..., "detail": ...}``. Reused
rather than reimplemented, because ``frontend/src/api/client.ts`` reads ``code`` as the
machine slug it branches on. See docs/contracts/run-composer.md.
"""

import datetime

from django.db import IntegrityError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from project.app.models import Lead, SavedScope
from project.app.serializers_compose import SavedScopeSerializer
from project.app.services.compose import scope as scope_service

# Reused, not reimplemented -- one error shape across the whole API.
from project.app.views_queue import error


def scope_error(exc):
    """A 400 that names the filter that caused it.

    The standard envelope plus one key. ``key`` is what lets the frontend highlight the
    chip the operator has to fix rather than making them re-read the whole scope.
    """
    return Response(
        {"code": exc.code, "detail": str(exc), "key": exc.key},
        status=status.HTTP_400_BAD_REQUEST,
    )


def scope_body(request):
    """The ``scope`` object out of a request body, defaulting to "everyone"."""
    body = request.data if isinstance(request.data, dict) else {}
    raw = body.get("scope", {})
    return raw if isinstance(raw, dict) else {}


class _ComposeStub(APIView):
    """Base for the not-yet-implemented compose views.

    Names the owning component in the message so a 500 in a test run points at the PR that
    still owes the work rather than at a mystery.
    """

    component = "compose"

    def _unimplemented(self):
        raise NotImplementedError(
            f"{type(self).__name__} is a MUS-47 skeleton stub; "
            f"the {self.component} component owns it"
        )

    def get(self, request, *args, **kwargs):
        self._unimplemented()

    def post(self, request, *args, **kwargs):
        self._unimplemented()

    def delete(self, request, *args, **kwargs):
        self._unimplemented()


# --- runs (lifecycle component) ---------------------------------------------


class RunListCreateView(_ComposeStub):
    """POST /api/runs/ -- create a draft run from a scope. 409 when one is active."""

    component = "lifecycle"


class RunActiveView(_ComposeStub):
    """GET /api/runs/active/ -- the one active run, whatever stage it is at."""

    component = "lifecycle"


class RunPreviewCountView(APIView):
    """POST /api/runs/preview-count/ -- how many leads a scope matches.

    The number under the Create Run button, and the only compose endpoint that spends
    nothing, writes nothing, and knows nothing about the run lifecycle. That last part is
    deliberate: `POST /api/runs/` 409s while a run is active, but re-scoping is exactly
    what an operator does *with* a run open, so this must keep answering.

    `total` is what makes `count` legible -- "4" on its own does not say of how many.
    """

    def post(self, request, *args, **kwargs):
        try:
            validated = scope_service.validate_scope(scope_body(request))
        except scope_service.ScopeError as exc:
            return scope_error(exc)

        matched = scope_service.apply_scope(
            Lead.objects.all(), validated, today=datetime.date.today()
        )
        return Response(
            {"count": matched.count(), "total": Lead.objects.count()},
            status=status.HTTP_200_OK,
        )


class RunDetailView(_ComposeStub):
    """GET /api/runs/{id}/ -- the run plus its RunLead rows."""

    component = "lifecycle"


class RunClassifyView(_ComposeStub):
    """POST /api/runs/{id}/classify/ -- the free deterministic pass."""

    component = "lifecycle"


class RunCloseView(_ComposeStub):
    """POST /api/runs/{id}/close/ -- finish the run and free the active slot."""

    component = "lifecycle"


class RunDiscardView(_ComposeStub):
    """POST /api/runs/{id}/discard/ -- abandon the run and free the active slot."""

    component = "lifecycle"


class RunEstimateView(_ComposeStub):
    """GET /api/runs/{id}/estimate/?stage=&provider=&model= -- the price, before the spend."""

    component = "estimate"


class RunReadView(_ComposeStub):
    """POST /api/runs/{id}/read/ -- the optional advisory pass."""

    component = "read"


class SuggestionAcceptView(_ComposeStub):
    """POST /api/runs/{id}/suggestions/{lead_id}/accept/ -- a logged human decision."""

    component = "decisions"


class SuggestionRejectView(_ComposeStub):
    """POST /api/runs/{id}/suggestions/{lead_id}/reject/ -- equally logged, equally real."""

    component = "decisions"


class RunSelectView(_ComposeStub):
    """POST /api/runs/{id}/select/ -- bulk-toggle which leads get copy."""

    component = "generate"


class RunGenerateView(_ComposeStub):
    """POST /api/runs/{id}/generate/ -- the expensive stage."""

    component = "generate"


# --- saved scopes (scope component) -----------------------------------------


class ScopeFieldsView(APIView):
    """GET /api/scopes/fields/ -- the filterable-field catalog the chip builder renders.

    An envelope rather than a bare list, so a key can be added later without breaking
    the frontend's parse.
    """

    def get(self, request, *args, **kwargs):
        return Response({"fields": scope_service.scope_field_catalog()}, status=status.HTTP_200_OK)


class ScopeListCreateView(APIView):
    """GET/POST /api/scopes/ -- list and save named scopes.

    A saved scope's filters go through `validate_scope` on the way IN, not on the way
    out. The row is replayed into `apply_scope` on some future run, and validating only
    then leaves an unusable scope sitting in the list looking legitimate in the
    meantime -- and stores a raw body that pushes coercion onto every later reader.
    """

    def get(self, request, *args, **kwargs):
        scopes = SavedScope.objects.all()  # Meta.ordering = ["name"]
        return Response(SavedScopeSerializer(scopes, many=True).data, status=status.HTTP_200_OK)

    def post(self, request, *args, **kwargs):
        body = request.data if isinstance(request.data, dict) else {}
        name = (body.get("name") or "").strip()
        if not name:
            return error(
                "invalid_scope", "A saved scope needs a name.", status.HTTP_400_BAD_REQUEST
            )

        raw = body.get("filters", {})
        try:
            filters = scope_service.validate_scope(raw if isinstance(raw, dict) else {})
        except scope_service.ScopeError as exc:
            return scope_error(exc)

        try:
            saved = SavedScope.objects.create(
                name=name, filters=filters, created_by=getattr(request.user, "email", "") or ""
            )
        except IntegrityError:
            # `name` is unique: two tabs, or a re-submitted save.
            return error(
                "scope_exists",
                f"A saved scope named {name!r} already exists.",
                status.HTTP_409_CONFLICT,
            )

        return Response(SavedScopeSerializer(saved).data, status=status.HTTP_201_CREATED)


class ScopeDetailView(APIView):
    """DELETE /api/scopes/{id}/ -- forget a saved scope."""

    def delete(self, request, pk, *args, **kwargs):
        deleted, _ = SavedScope.objects.filter(pk=pk).delete()
        if not deleted:
            # Two tabs, one scope, one delete each: the second gets an answer it can
            # branch on rather than a silent success that implies it did something.
            return error("not_found", "No saved scope with that id.", status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)
