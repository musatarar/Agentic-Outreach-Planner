"""Lead listing and per-client composition."""

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from project.app.models import Lead
from project.app.permissions import HasTenant
from project.app.serializers import LeadSerializer, OutreachActionSerializer


class LeadListView(APIView):
    """GET /api/leads/ — the caller's workspace's leads."""

    permission_classes = [IsAuthenticated, HasTenant]

    def get(self, request, *args, **kwargs):
        leads = Lead.objects.filter(tenant=request.tenant).order_by("id")
        serializer = LeadSerializer(leads, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class LeadComposeView(APIView):
    """POST /api/leads/{lead_id}/compose/ — compose outreach for ONE client.

    The same planner ``/api/outreach/run/`` calls, scoped to one lead. 200 with
    the new action, 404 for an unknown lead, 409 when the planner declines
    (already queued or permanently dismissed) — no provider call in either refusal.

    Another workspace's lead is the same 404 as a lead that does not exist: the
    response must not tell the caller which ids are taken elsewhere.
    """

    permission_classes = [IsAuthenticated, HasTenant]

    def post(self, request, lead_id, *args, **kwargs):
        # Imported inside the method for the same reason OutreachRunView's is.
        from project.app.services.outreach import plan_outreach

        if not Lead.objects.filter(pk=lead_id, tenant=request.tenant).exists():
            return Response({"error": "unknown_lead"}, status=status.HTTP_404_NOT_FOUND)

        planned = plan_outreach(request.tenant, lead_ids=[lead_id])
        if not planned:
            return Response({"error": "no_new_recommendation"}, status=status.HTTP_409_CONFLICT)

        # At most one action per lead per run, and this run is one lead.
        serializer = OutreachActionSerializer(planned[0])
        return Response(serializer.data, status=status.HTTP_200_OK)
