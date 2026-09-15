"""Rules-catalog API: CRUD over the signed-in user's actions and rules.

Every queryset is scoped to ``request.user`` and ``owner`` is bound
server-side — a row id belonging to someone else reads as 404, and an owner
in the payload is ignored. Writes run ``full_clean()`` so the model's
pairing/ownership/uniqueness validation backs every endpoint.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import RestrictedError
from django.urls import path
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from project.app.exceptions import ContractError
from project.app.rules.models import ActionType, OutreachRule


class ActionTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ActionType
        fields = [
            "id",
            "key",
            "label",
            "description",
            "urgency",
            "enabled",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class OutreachRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = OutreachRule
        fields = [
            "id",
            "action",
            "name",
            "kind",
            "conditions",
            "inference_prompt",
            "enabled",
            "order",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_fields(self):
        """``action`` resolves only among the requester's own catalog, so a
        foreign action id reads as nonexistent."""
        fields = super().get_fields()
        request = self.context.get("request")
        if request is not None:
            fields["action"].queryset = ActionType.objects.filter(owner=request.user)
        return fields


def _validated_save(instance, validated):
    """Apply the validated fields and save through ``full_clean()``."""
    for field, value in validated.items():
        setattr(instance, field, value)
    try:
        instance.full_clean()
    except DjangoValidationError as exc:
        raise serializers.ValidationError(exc.message_dict)
    instance.save()
    return instance


class _OwnedCatalogView(APIView):
    """Owner scoping shared by the four endpoints below."""

    throttle_scope = "rules_catalog"
    model = None
    serializer_class = None

    def _queryset(self, request):
        return self.model.objects.filter(owner=request.user)

    def _get_owned(self, request, pk):
        instance = self._queryset(request).filter(pk=pk).first()
        if instance is None:
            raise NotFound(f"No {self.model._meta.verbose_name} with this id.")
        return instance


class ActionTypeListCreateView(_OwnedCatalogView):
    """GET/POST /api/rules/actions/ — the signed-in user's action catalog."""

    model = ActionType
    serializer_class = ActionTypeSerializer

    def get(self, request, *args, **kwargs):
        serializer = self.serializer_class(self._queryset(request), many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request, *args, **kwargs):
        serializer = self.serializer_class(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        instance = _validated_save(self.model(owner=request.user), serializer.validated_data)
        return Response(self.serializer_class(instance).data, status=status.HTTP_201_CREATED)


class ActionTypeDetailView(_OwnedCatalogView):
    """GET/PATCH/DELETE /api/rules/actions/{id}/ — one owned action."""

    model = ActionType
    serializer_class = ActionTypeSerializer

    def get(self, request, pk, *args, **kwargs):
        instance = self._get_owned(request, pk)
        return Response(self.serializer_class(instance).data, status=status.HTTP_200_OK)

    def patch(self, request, pk, *args, **kwargs):
        instance = self._get_owned(request, pk)
        serializer = self.serializer_class(
            instance, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        instance = _validated_save(instance, serializer.validated_data)
        return Response(self.serializer_class(instance).data, status=status.HTTP_200_OK)

    def delete(self, request, pk, *args, **kwargs):
        instance = self._get_owned(request, pk)
        try:
            instance.delete()
        except RestrictedError:
            raise ContractError(
                "action_in_use",
                "Rules still select this action; delete or repoint them first.",
                status_code=status.HTTP_409_CONFLICT,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


class OutreachRuleListCreateView(_OwnedCatalogView):
    """GET/POST /api/rules/ — the signed-in user's rules in evaluation order."""

    model = OutreachRule
    serializer_class = OutreachRuleSerializer

    def get(self, request, *args, **kwargs):
        serializer = self.serializer_class(self._queryset(request), many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request, *args, **kwargs):
        serializer = self.serializer_class(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        instance = _validated_save(self.model(owner=request.user), serializer.validated_data)
        return Response(self.serializer_class(instance).data, status=status.HTTP_201_CREATED)


class OutreachRuleDetailView(_OwnedCatalogView):
    """GET/PATCH/DELETE /api/rules/{id}/ — one owned rule."""

    model = OutreachRule
    serializer_class = OutreachRuleSerializer

    def get(self, request, pk, *args, **kwargs):
        instance = self._get_owned(request, pk)
        return Response(self.serializer_class(instance).data, status=status.HTTP_200_OK)

    def patch(self, request, pk, *args, **kwargs):
        instance = self._get_owned(request, pk)
        serializer = self.serializer_class(
            instance, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        instance = _validated_save(instance, serializer.validated_data)
        return Response(self.serializer_class(instance).data, status=status.HTTP_200_OK)

    def delete(self, request, pk, *args, **kwargs):
        self._get_owned(request, pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# Appended to the `api/` urlpatterns as flat patterns (not include()d): the
# auth suite audits every pattern's permission classes and expects callbacks.
urlpatterns = [
    path("rules/actions/", ActionTypeListCreateView.as_view(), name="rules-action-list"),
    path("rules/actions/<int:pk>/", ActionTypeDetailView.as_view(), name="rules-action-detail"),
    path("rules/", OutreachRuleListCreateView.as_view(), name="rules-list"),
    path("rules/<int:pk>/", OutreachRuleDetailView.as_view(), name="rules-detail"),
]
