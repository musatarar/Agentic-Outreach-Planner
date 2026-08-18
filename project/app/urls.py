from django.urls import path

from project.app.views import (
    LeadAssessView,
    LeadComposeView,
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

# Included at the `api/` prefix by project/urls.py.
urlpatterns = [
    # --- auth (MUS-37) ---
    path("auth/request-link/", AuthRequestLinkView.as_view(), name="auth-request-link"),
    path("auth/consume/", AuthConsumeView.as_view(), name="auth-consume"),
    path("auth/logout/", AuthLogoutView.as_view(), name="auth-logout"),
    path("auth/me/", AuthMeView.as_view(), name="auth-me"),
    # --- triage queue (MUS-39) ---
    # `queue/done/` must precede `queue/<int:pk>/`.
    path("queue/", QueueListView.as_view(), name="queue-list"),
    path("queue/done/", QueueDoneView.as_view(), name="queue-done"),
    path("queue/<int:pk>/", QueueDetailView.as_view(), name="queue-detail"),
    path("queue/<int:pk>/edit/", QueueEditView.as_view(), name="queue-edit"),
    path("queue/<int:pk>/verify/", QueueVerifyView.as_view(), name="queue-verify"),
    path("queue/<int:pk>/approve/", QueueApproveView.as_view(), name="queue-approve"),
    path("queue/<int:pk>/snooze/", QueueSnoozeView.as_view(), name="queue-snooze"),
    path("queue/<int:pk>/dismiss/", QueueDismissView.as_view(), name="queue-dismiss"),
    path("queue/<int:pk>/undo/", QueueUndoView.as_view(), name="queue-undo"),
    # --- existing ---
    path("outreach/run/", OutreachRunView.as_view(), name="outreach-run"),
    path("outreach/<int:pk>/trace/", OutreachTraceView.as_view(), name="outreach-trace"),
    path("outreach/", OutreachListView.as_view(), name="outreach-list"),
    path("leads/", LeadListView.as_view(), name="lead-list"),
    # --- per-client composition (MUS-68) and assessment (MUS-70) ---
    path("leads/<str:lead_id>/assess/", LeadAssessView.as_view(), name="lead-assess"),
    path("leads/<str:lead_id>/compose/", LeadComposeView.as_view(), name="lead-compose"),
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
