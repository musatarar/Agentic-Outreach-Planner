"""Domain-split API views; the SPA page shells stay in ``views.frontend``."""

from .auth import AuthConsumeView, AuthLogoutView, AuthMeView, AuthRequestLinkView
from .leads import LeadComposeView, LeadListView
from .llm import LLMCatalogView, LLMConfigTestView, LLMConfigView
from .outreach import (
    OutreachListView,
    OutreachReportView,
    OutreachRunView,
    ReviewDecisionListCreateView,
    ReviewQueueView,
)
from .queue import (
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
from .trace import OutreachTraceView

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
    "OutreachListView",
    "OutreachReportView",
    "OutreachRunView",
    "OutreachTraceView",
    "QueueApproveView",
    "QueueDetailView",
    "QueueDismissView",
    "QueueDoneView",
    "QueueEditView",
    "QueueListView",
    "QueueSnoozeView",
    "QueueUndoView",
    "QueueVerifyView",
    "ReviewDecisionListCreateView",
    "ReviewQueueView",
]
