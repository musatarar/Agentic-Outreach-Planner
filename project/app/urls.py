from django.urls import path

from project.app.views import (
    LeadListView,
    LLMCatalogView,
    LLMConfigTestView,
    LLMConfigView,
    OutreachListView,
    OutreachReportView,
    OutreachRunView,
    ReviewDecisionListCreateView,
    ReviewQueueView,
)
from project.app.views_auth import (
    AuthConsumeView,
    AuthLogoutView,
    AuthMeView,
    AuthRequestLinkView,
)
from project.app.views_compose import (
    RunActiveView,
    RunClassifyView,
    RunCloseView,
    RunDetailView,
    RunDiscardView,
    RunEstimateView,
    RunGenerateView,
    RunListCreateView,
    RunPreviewCountView,
    RunReadView,
    RunSelectView,
    ScopeDetailView,
    ScopeFieldsView,
    ScopeListCreateView,
    SuggestionAcceptView,
    SuggestionRejectView,
)
from project.app.views_queue import (
    QueueApproveView,
    QueueDetailView,
    QueueDismissView,
    QueueDoneView,
    QueueEditView,
    QueueListView,
    QueueSnoozeView,
    QueueUndoView,
    QueueVerifyView,
)
from project.app.views_trace import OutreachTraceView

# Included at the `api/` prefix by project/urls.py:
#   POST api/auth/request-link/  POST api/auth/consume/
#   POST api/auth/logout/  GET api/auth/me/
#   POST api/outreach/run/  GET api/outreach/  GET api/leads/
#   GET api/llm/catalog/  GET|PUT api/llm/config/  POST api/llm/config/test/
#   GET api/queue/  GET api/queue/done/  GET api/queue/{id}/
urlpatterns = [
    # --- auth (MUS-37) ---
    path("auth/request-link/", AuthRequestLinkView.as_view(), name="auth-request-link"),
    path("auth/consume/", AuthConsumeView.as_view(), name="auth-consume"),
    path("auth/logout/", AuthLogoutView.as_view(), name="auth-logout"),
    path("auth/me/", AuthMeView.as_view(), name="auth-me"),
    # --- triage queue (MUS-39) ---
    # `queue/done/` MUST precede `queue/<int:pk>/`. With <int:pk> it happens to
    # be safe, but ordering it defensively removes a class of "why does /done
    # 404" debugging for free.
    path("queue/", QueueListView.as_view(), name="queue-list"),
    path("queue/done/", QueueDoneView.as_view(), name="queue-done"),
    path("queue/<int:pk>/", QueueDetailView.as_view(), name="queue-detail"),
    path("queue/<int:pk>/edit/", QueueEditView.as_view(), name="queue-edit"),
    path("queue/<int:pk>/verify/", QueueVerifyView.as_view(), name="queue-verify"),
    path("queue/<int:pk>/approve/", QueueApproveView.as_view(), name="queue-approve"),
    path("queue/<int:pk>/snooze/", QueueSnoozeView.as_view(), name="queue-snooze"),
    path("queue/<int:pk>/dismiss/", QueueDismissView.as_view(), name="queue-dismiss"),
    path("queue/<int:pk>/undo/", QueueUndoView.as_view(), name="queue-undo"),
    # --- run composer (MUS-47) ---
    # The two literal segments MUST precede `runs/<int:pk>/`, and `scopes/fields/` must
    # precede `scopes/<int:pk>/` -- same defensive ordering as `queue/done/` above.
    path("runs/active/", RunActiveView.as_view(), name="run-active"),
    path("runs/preview-count/", RunPreviewCountView.as_view(), name="run-preview-count"),
    path("runs/", RunListCreateView.as_view(), name="run-list-create"),
    path("runs/<int:pk>/", RunDetailView.as_view(), name="run-detail"),
    path("runs/<int:pk>/classify/", RunClassifyView.as_view(), name="run-classify"),
    path("runs/<int:pk>/close/", RunCloseView.as_view(), name="run-close"),
    path("runs/<int:pk>/discard/", RunDiscardView.as_view(), name="run-discard"),
    path("runs/<int:pk>/estimate/", RunEstimateView.as_view(), name="run-estimate"),
    path("runs/<int:pk>/read/", RunReadView.as_view(), name="run-read"),
    path("runs/<int:pk>/select/", RunSelectView.as_view(), name="run-select"),
    path("runs/<int:pk>/generate/", RunGenerateView.as_view(), name="run-generate"),
    path(
        "runs/<int:pk>/suggestions/<str:lead_id>/accept/",
        SuggestionAcceptView.as_view(),
        name="run-suggestion-accept",
    ),
    path(
        "runs/<int:pk>/suggestions/<str:lead_id>/reject/",
        SuggestionRejectView.as_view(),
        name="run-suggestion-reject",
    ),
    path("scopes/fields/", ScopeFieldsView.as_view(), name="scope-fields"),
    path("scopes/", ScopeListCreateView.as_view(), name="scope-list-create"),
    path("scopes/<int:pk>/", ScopeDetailView.as_view(), name="scope-detail"),
    # --- existing ---
    path("outreach/run/", OutreachRunView.as_view(), name="outreach-run"),
    path("outreach/<int:pk>/trace/", OutreachTraceView.as_view(), name="outreach-trace"),
    path("outreach/", OutreachListView.as_view(), name="outreach-list"),
    path("leads/", LeadListView.as_view(), name="lead-list"),
    path("reports/", OutreachReportView.as_view(), name="outreach-reports"),
    path("review-queue/", ReviewQueueView.as_view(), name="review-queue"),
    path(
        "review-decisions/",
        ReviewDecisionListCreateView.as_view(),
        name="review-decisions",
    ),
    path("llm/catalog/", LLMCatalogView.as_view(), name="llm-catalog"),
    path("llm/config/", LLMConfigView.as_view(), name="llm-config"),
    path("llm/config/test/", LLMConfigTestView.as_view(), name="llm-config-test"),
]
