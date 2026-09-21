from django.urls import path

from project.app.actions import routes as actions_routes
from project.app.rules import routes as rules_routes
from project.app.views.auth import (
    AuthConsumeView,
    AuthLogoutView,
    AuthMeView,
    AuthRequestLinkView,
)
from project.app.views.leads import LeadListView
from project.app.views.review import (
    ReviewApproveView,
    ReviewDismissView,
    ReviewEditView,
    ReviewListView,
    ReviewReopenView,
    ReviewVerifyView,
)

# Included at the `api/` prefix by project/urls.py.
urlpatterns = [
    # --- auth ---
    path("auth/request-link/", AuthRequestLinkView.as_view(), name="auth-request-link"),
    path("auth/consume/", AuthConsumeView.as_view(), name="auth-consume"),
    path("auth/logout/", AuthLogoutView.as_view(), name="auth-logout"),
    path("auth/me/", AuthMeView.as_view(), name="auth-me"),
    # --- leads ---
    path("leads/", LeadListView.as_view(), name="lead-list"),
    # --- the review inbox ---
    path("outreach/", ReviewListView.as_view(), name="outreach-list"),
    path("outreach/<int:pk>/edit/", ReviewEditView.as_view(), name="outreach-edit"),
    path("outreach/<int:pk>/verify/", ReviewVerifyView.as_view(), name="outreach-verify"),
    path("outreach/<int:pk>/approve/", ReviewApproveView.as_view(), name="outreach-approve"),
    path("outreach/<int:pk>/dismiss/", ReviewDismissView.as_view(), name="outreach-dismiss"),
    path("outreach/<int:pk>/reopen/", ReviewReopenView.as_view(), name="outreach-reopen"),
    # --- user-defined rules catalog ---
    *rules_routes.urlpatterns,
    # --- what the actions engine chose, and copy for one on demand ---
    *actions_routes.urlpatterns,
]
