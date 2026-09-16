"""Domain-split API views; the SPA page shells stay in ``views.frontend``."""

from .auth import AuthConsumeView, AuthLogoutView, AuthMeView, AuthRequestLinkView
from .leads import LeadComposeView, LeadListView
from .outreach import OutreachRunView
from .review import (
    ReviewApproveView,
    ReviewDismissView,
    ReviewEditView,
    ReviewListView,
    ReviewReopenView,
    ReviewVerifyView,
)

__all__ = [
    "AuthConsumeView",
    "AuthLogoutView",
    "AuthMeView",
    "AuthRequestLinkView",
    "LeadComposeView",
    "LeadListView",
    "OutreachRunView",
    "ReviewApproveView",
    "ReviewDismissView",
    "ReviewEditView",
    "ReviewListView",
    "ReviewReopenView",
    "ReviewVerifyView",
]
