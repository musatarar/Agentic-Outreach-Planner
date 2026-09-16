"""Planner-facing endpoints: run the planner over the whole book."""

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from project.app.serializers import OutreachActionSerializer


class OutreachRunView(APIView):
    """POST /api/outreach/run/ — plan the signed-in user's book, return the actions."""

    def post(self, request, *args, **kwargs):
        # Imported inside the method so this module loads independently of the
        # service module.
        from project.app.services.outreach import plan_outreach

        actions = plan_outreach(tenant=request.user)
        actions = sorted(actions, key=lambda a: (a.priority, a.lead_id))
        serializer = OutreachActionSerializer(actions, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)
