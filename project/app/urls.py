from django.urls import path

from project.app.views import (
    LeadListView,
    OutreachListView,
    OutreachRunView,
)

# Included at the `api/` prefix by project/urls.py:
#   POST api/outreach/run/  GET api/outreach/  GET api/leads/
urlpatterns = [
    path("outreach/run/", OutreachRunView.as_view(), name="outreach-run"),
    path("outreach/", OutreachListView.as_view(), name="outreach-list"),
    path("leads/", LeadListView.as_view(), name="lead-list"),
]
