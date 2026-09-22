"""Domain-split API views; the SPA page shells stay in ``views.frontend``."""

from .auth import AuthConsumeView, AuthLogoutView, AuthMeView, AuthRequestLinkView
from .leads import LeadListView, ShapeView
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
    "LeadListView",
    "ReviewApproveView",
    "ReviewDismissView",
    "ReviewEditView",
    "ReviewListView",
    "ReviewReopenView",
    "ReviewVerifyView",
    "ShapeView",
]
