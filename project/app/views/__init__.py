"""Domain-split API views; the SPA page shells stay in ``views.frontend``."""

from .auth import AuthConsumeView, AuthLogoutView, AuthMeView, AuthRequestLinkView
from .leads import LeadComposeView, LeadListView
from .llm import LLMCatalogView, LLMConfigTestView, LLMConfigView
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
    "LLMCatalogView",
    "LLMConfigTestView",
    "LLMConfigView",
    "OutreachRunView",
    "ReviewApproveView",
    "ReviewDismissView",
    "ReviewEditView",
    "ReviewListView",
    "ReviewReopenView",
    "ReviewVerifyView",
]
