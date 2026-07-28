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
from project.app.views_queue import (
    QueueDetailView,
    QueueDoneView,
    QueueListView,
)

# Included at the `api/` prefix by project/urls.py:
#   POST api/outreach/run/  GET api/outreach/  GET api/leads/
#   GET api/llm/catalog/  GET|PUT api/llm/config/  POST api/llm/config/test/
#   GET api/queue/  GET api/queue/done/  GET api/queue/{id}/
urlpatterns = [
    # --- triage queue (MUS-39) ---
    # `queue/done/` MUST precede `queue/<int:pk>/`. With <int:pk> it happens to
    # be safe, but ordering it defensively removes a class of "why does /done
    # 404" debugging for free.
    path("queue/", QueueListView.as_view(), name="queue-list"),
    path("queue/done/", QueueDoneView.as_view(), name="queue-done"),
    path("queue/<int:pk>/", QueueDetailView.as_view(), name="queue-detail"),
    # --- existing ---
    path("outreach/run/", OutreachRunView.as_view(), name="outreach-run"),
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
