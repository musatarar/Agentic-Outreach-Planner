"""Lead listing, and the shape that says what a lead is."""

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from project.app.models import Lead, Shape
from project.app.serializers import LeadSerializer, ShapeSerializer
from project.app.services import shape as shape_service


class LeadListView(APIView):
    """GET /api/leads/ — all leads."""

    def get(self, request, *args, **kwargs):
        leads = Lead.objects.all().order_by("id")
        serializer = LeadSerializer(leads, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class ShapeView(APIView):
    """GET/PUT /api/shape/ — the signed-in user's own declaration.

    One shape per user, so there is no id to address: the session names it.
    A user who has declared nothing reads an empty one rather than a 404,
    because that is what they are about to fill in.

    PUT replaces the declaration whole — both lists are required — and is
    refused when it would leave one of the owner's stored rules unevaluable.
    An event column is declared and may be named in a rule, but no condition on
    the ``events`` source evaluates yet: the engine refuses those until events
    have an "any event where..." semantic.
    """

    throttle_scope = "shape"

    def get(self, request, *args, **kwargs):
        shape = Shape.objects.filter(owner=request.user).first() or Shape(owner=request.user)
        return Response(ShapeSerializer(shape).data, status=status.HTTP_200_OK)

    def put(self, request, *args, **kwargs):
        serializer = ShapeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            shape = shape_service.declare(request.user, serializer.validated_data)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict)
        return Response(ShapeSerializer(shape).data, status=status.HTTP_200_OK)
