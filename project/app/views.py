from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from project.app.models import Lead, OutreachAction
from project.app.serializers import (
    LeadSerializer,
    OutreachActionSerializer,
)


class OutreachRunView(APIView):
    """POST /api/outreach/run/ — run the planner and return created actions."""

    def post(self, request, *args, **kwargs):
        # Imported inside the method so the view module loads even before the
        # service module exists (it's built by another agent in parallel).
        from project.app.services.outreach import plan_outreach

        actions = plan_outreach()
        actions = sorted(actions, key=lambda a: (a.priority, a.lead_id))
        serializer = OutreachActionSerializer(actions, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class OutreachListView(APIView):
    """GET /api/outreach/ — most recent action per lead, ordered by priority."""

    def get(self, request, *args, **kwargs):
        # Most recent OutreachAction per lead: order so the newest action for a
        # lead comes first, then keep the first occurrence per lead.
        latest = (
            OutreachAction.objects.select_related("lead")
            .order_by("lead_id", "-created_at", "-id")
        )
        seen = set()
        actions = []
        for action in latest:
            if action.lead_id in seen:
                continue
            seen.add(action.lead_id)
            actions.append(action)

        actions.sort(key=lambda a: (a.priority, a.lead_id))
        serializer = OutreachActionSerializer(actions, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class LeadListView(APIView):
    """GET /api/leads/ — all leads."""

    def get(self, request, *args, **kwargs):
        leads = Lead.objects.all().order_by("id")
        serializer = LeadSerializer(leads, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)
