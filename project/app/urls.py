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

# Included at the `api/` prefix by project/urls.py:
#   POST api/outreach/run/  GET api/outreach/  GET api/leads/
#   GET api/llm/catalog/  GET|PUT api/llm/config/  POST api/llm/config/test/
urlpatterns = [
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
