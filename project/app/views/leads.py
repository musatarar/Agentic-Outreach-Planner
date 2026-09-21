"""Lead listing."""

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from project.app.models import Lead
from project.app.serializers import LeadSerializer


class LeadListView(APIView):
    """GET /api/leads/ — all leads."""

    def get(self, request, *args, **kwargs):
        leads = Lead.objects.all().order_by("id")
        serializer = LeadSerializer(leads, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)
