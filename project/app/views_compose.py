"""Run Composer endpoints (MUS-47).

Skeleton state: every view below is wired into ``urls.py`` and raises
``NotImplementedError``. That is deliberate. A route that does not exist yet answers an
endpoint test with a Django 404, and a 404 proves nothing about the endpoint the test is
specifying — it is the same answer a typo'd path gives. Wiring the routes up front means a
red endpoint test fails on the thing it is actually asserting, and the component PR that
fills the view in is the only diff that has to change.

Error envelope: ``views_queue.error()``, i.e. ``{"code": ..., "detail": ...}``. Reused
rather than reimplemented, because ``frontend/src/api/client.ts`` reads ``code`` as the
machine slug it branches on. See docs/contracts/run-composer.md.
"""

from rest_framework.views import APIView

# Reused, not reimplemented -- one error shape across the whole API.
from project.app.views_queue import error  # noqa: F401  (component PRs use it)


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


class RunPreviewCountView(_ComposeStub):
    """POST /api/runs/preview-count/ -- how many leads a scope matches. No run created."""

    component = "scope"


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


class ScopeFieldsView(_ComposeStub):
    """GET /api/scopes/fields/ -- the filterable-field catalog the chip builder renders."""

    component = "scope"


class ScopeListCreateView(_ComposeStub):
    """GET/POST /api/scopes/ -- list and save named scopes."""

    component = "scope"


class ScopeDetailView(_ComposeStub):
    """DELETE /api/scopes/{id}/ -- forget a saved scope."""

    component = "scope"
