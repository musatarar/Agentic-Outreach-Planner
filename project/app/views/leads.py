"""Lead listing and per-client composition."""

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from project.app.models import Lead
from project.app.serializers import LeadSerializer, OutreachActionSerializer


class LeadListView(APIView):
    """GET /api/leads/ — the signed-in user's book of leads."""

    def get(self, request, *args, **kwargs):
        leads = Lead.objects.for_tenant(request.user).order_by("id")
        serializer = LeadSerializer(leads, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class LeadComposeView(APIView):
    """POST /api/leads/{lead_id}/compose/ — compose outreach for ONE client.

    The same planner ``/api/outreach/run/`` calls, scoped to one lead. 200 with
    the new action, 404 for an unknown lead, 409 when the planner declines
    (already queued or permanently dismissed) — no provider call in either refusal.

    Another tenant's lead is 404, not 403: the caller learns nothing about a
    book that is not theirs.
    """

    def post(self, request, lead_id, *args, **kwargs):
        # Imported inside the method for the same reason OutreachRunView's is.
        from project.app.services.outreach import plan_outreach

        if not Lead.objects.for_tenant(request.user).filter(pk=lead_id).exists():
            return Response({"error": "unknown_lead"}, status=status.HTTP_404_NOT_FOUND)

        planned = plan_outreach(lead_ids=[lead_id], tenant=request.user)
        if not planned:
            return Response({"error": "no_new_recommendation"}, status=status.HTTP_409_CONFLICT)

        # At most one action per lead per run, and this run is one lead.
        serializer = OutreachActionSerializer(planned[0])
        return Response(serializer.data, status=status.HTTP_200_OK)
