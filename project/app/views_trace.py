"""GET /api/outreach/<pk>/trace/ — one action's agent step log (MUS-29).

The reports page renders this as "How this draft was reached". Single-shot
actions (no agent run) 404 with ``{"error": "no_agent_trace"}`` so the frontend
can hide the toggle instead of special-casing an empty trace.

Skeleton stub; the ``reports_trace`` component PR implements it.
"""

from rest_framework.views import APIView


class OutreachTraceView(APIView):
    """Serve the persisted ``AgentStep`` trace for one ``OutreachAction``."""

    def get(self, request, pk):
        raise NotImplementedError("reports_trace component owns OutreachTraceView")
