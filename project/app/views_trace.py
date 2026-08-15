"""GET /api/outreach/<pk>/trace/ — one action's agent step log (MUS-29).

The reports page renders this as "How this draft was reached". Single-shot
actions (no agent run) 404 with ``{"error": "no_agent_trace"}`` so the frontend
can hide the toggle instead of special-casing an empty trace.

Serialized inline rather than via a DRF serializer: the shape is pinned by
docs/contracts/agent-loop.md and ``serializers.py`` is owned by the
approval_gate component. Constant query count: one action, one run, one
step prefetch.
"""

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from project.app.models import AgentLeadRun, OutreachAction
from project.app.serializers_queue import iso


class OutreachTraceView(APIView):
    """Serve the persisted ``AgentStep`` trace for one ``OutreachAction``."""

    def get(self, request, pk):
        action = get_object_or_404(OutreachAction, pk=pk)
        run = None
        if action.trace_run_id:
            # The (trace_run_id, lead) pair is the join identity promised by
            # AgentLeadRun's unique constraint; single-shot actions share a
            # trace_run_id but have no run row.
            run = (
                AgentLeadRun.objects.filter(
                    trace_run_id=action.trace_run_id, lead_id=action.lead_id
                )
                .prefetch_related("steps")
                .first()
            )
        if run is None:
            return Response({"error": "no_agent_trace"}, status=status.HTTP_404_NOT_FOUND)
        return Response(
            {
                "action_id": action.pk,
                "lead_id": action.lead_id,
                "trace_run_id": run.trace_run_id,
                "status": run.status,
                "steps_used": run.steps_used,
                "tool_calls_used": run.tool_calls_used,
                "steps": [
                    {
                        "seq": step.seq,
                        "kind": step.kind,
                        "payload": step.payload,
                        "created_at": iso(step.created_at),
                    }
                    for step in run.steps.all()
                ],
            }
        )
