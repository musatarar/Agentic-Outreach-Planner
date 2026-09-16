"""Rules-catalog API: CRUD over the signed-in user's actions and rules.

HTTP only — reads, writes and their rules live in :mod:`services`. Every
lookup is owner-scoped there, ``owner`` is bound from the session (an owner in
the payload is ignored), and a row id belonging to someone else reads as 404.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.urls import path
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from project.app.exceptions import ContractError
from project.app.rules import services
from project.app.rules.models import ActionType, OutreachRule
from project.app.views.review import ReviewPagination


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
            "weight",
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
            fields["action"].queryset = services.actions_for(request.user)
        return fields


class _CatalogView(APIView):
    """The read/write plumbing the four endpoints share."""

    throttle_scope = "rules_catalog"
    serializer_class = None

    def _payload(self, request, instance=None):
        """Validated fields from the request body, serializer first then the
        model — a DRF-shaped error either way."""
        serializer = self.serializer_class(
            instance, data=request.data, partial=instance is not None, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data

    def _write(self, write, *args):
        try:
            return write(*args)
        except DjangoValidationError as exc:
            problems = dict(exc.message_dict)
            # `full_clean` files model-wide problems (a unique constraint among
            # them) under `__all__`; DRF's envelope names that key differently.
            if "__all__" in problems:
                problems["non_field_errors"] = problems.pop("__all__")
            raise serializers.ValidationError(problems)

    def _paginated(self, request, queryset):
        """List responses are paginated: a catalog is per-user and small today,
        but nothing caps how many rules a user writes."""
        paginator = ReviewPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(self.serializer_class(page, many=True).data)

    def _render(self, instance, http_status=status.HTTP_200_OK):
        return Response(self.serializer_class(instance).data, status=http_status)

    def _found(self, instance, what):
        if instance is None:
            raise NotFound(f"No {what} with this id.")
        return instance


class ActionTypeListCreateView(_CatalogView):
    """GET/POST /api/rules/actions/ — the signed-in user's action catalog."""

    serializer_class = ActionTypeSerializer

    def get(self, request, *args, **kwargs):
        return self._paginated(request, services.actions_for(request.user))

    def post(self, request, *args, **kwargs):
        fields = self._payload(request)
        action = self._write(services.create_action, request.user, fields)
        return self._render(action, status.HTTP_201_CREATED)


class ActionTypeDetailView(_CatalogView):
    """GET/PATCH/DELETE /api/rules/actions/{id}/ — one owned action."""

    serializer_class = ActionTypeSerializer

    def _action(self, request, pk):
        return self._found(services.action_for(request.user, pk), "action type")

    def get(self, request, pk, *args, **kwargs):
        return self._render(self._action(request, pk))

    def patch(self, request, pk, *args, **kwargs):
        action = self._action(request, pk)
        fields = self._payload(request, action)
        return self._render(self._write(services.update_action, action, fields))

    def delete(self, request, pk, *args, **kwargs):
        try:
            services.delete_action(self._action(request, pk))
        except services.ActionInUse:
            raise ContractError(
                "action_in_use",
                "Rules still select this action; delete or repoint them first.",
                status_code=status.HTTP_409_CONFLICT,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


class OutreachRuleListCreateView(_CatalogView):
    """GET/POST /api/rules/ — the signed-in user's rules, heaviest first."""

    serializer_class = OutreachRuleSerializer

    def get(self, request, *args, **kwargs):
        return self._paginated(request, services.rules_for(request.user))

    def post(self, request, *args, **kwargs):
        fields = self._payload(request)
        rule = self._write(services.create_rule, request.user, fields)
        return self._render(rule, status.HTTP_201_CREATED)


class OutreachRuleDetailView(_CatalogView):
    """GET/PATCH/DELETE /api/rules/{id}/ — one owned rule."""

    serializer_class = OutreachRuleSerializer

    def _rule(self, request, pk):
        return self._found(services.rule_for(request.user, pk), "rule")

    def get(self, request, pk, *args, **kwargs):
        return self._render(self._rule(request, pk))

    def patch(self, request, pk, *args, **kwargs):
        rule = self._rule(request, pk)
        fields = self._payload(request, rule)
        return self._render(self._write(services.update_rule, rule, fields))

    def delete(self, request, pk, *args, **kwargs):
        services.delete_rule(self._rule(request, pk))
        return Response(status=status.HTTP_204_NO_CONTENT)


# Appended to the `api/` urlpatterns as flat patterns (not include()d): the
# auth suite audits every pattern's permission classes and expects callbacks.
urlpatterns = [
    path("rules/actions/", ActionTypeListCreateView.as_view(), name="rules-action-list"),
    path("rules/actions/<int:pk>/", ActionTypeDetailView.as_view(), name="rules-action-detail"),
    path("rules/", OutreachRuleListCreateView.as_view(), name="rules-list"),
    path("rules/<int:pk>/", OutreachRuleDetailView.as_view(), name="rules-detail"),
]
