"""Proposed-actions API: what the engine chose, and copy for one on demand.

HTTP only — the reads, the two refusals and the draft itself live in
:mod:`services`. Every lookup is owner-scoped there, so a job belonging to
someone else's lead reads as 404.
"""

from django.urls import path
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from project.app.actions import services
from project.app.actions.models import ActionJob
from project.app.exceptions import ContractError
from project.app.rules.models import ActionType
from project.app.serializers import LeadSummarySerializer, ReviewItemSerializer
from project.app.views.review import ReviewPagination


class ProposedActionTypeSerializer(serializers.ModelSerializer):
    """The catalog action a proposal chose, resolved for the card."""

    class Meta:
        model = ActionType
        fields = ["key", "label", "urgency"]


class ProposedActionSerializer(serializers.ModelSerializer):
    """One decided job as a proposal card.

    The raw ``decision`` payload never crosses the wire: it carries rule ids and
    the inference pass's verdicts, and the card needs the tally's conclusion.
    """

    lead = LeadSummarySerializer(read_only=True)
    action = ProposedActionTypeSerializer(source="selected_action", read_only=True)
    reasons = serializers.SerializerMethodField()
    weight = serializers.SerializerMethodField()
    decided_at = serializers.DateTimeField(source="finished_at", read_only=True)
    draft_id = serializers.SerializerMethodField()

    class Meta:
        model = ActionJob
        fields = ["id", "lead", "action", "reasons", "weight", "decided_at", "draft_id"]

    def get_reasons(self, obj):
        return services.selected_of(obj).get("reasons") or []

    def get_weight(self, obj):
        return services.selected_of(obj).get("weight")

    def get_draft_id(self, obj):
        # Resolved for the whole page at once; a per-row lookup would be N+1.
        return (self.context.get("draft_ids") or {}).get(obj.pk)


class ProposedActionListView(APIView):
    """GET /api/actions/ — the actions the engine chose for the signed-in
    user's leads, newest decision first."""

    throttle_scope = "actions_list"
    throttle_detail = "Too many proposed action list requests."

    def get(self, request, *args, **kwargs):
        paginator = ReviewPagination()
        page = paginator.paginate_queryset(services.proposals_for(request.user), request, view=self)
        context = {"draft_ids": services.open_draft_ids(page)}
        return paginator.get_paginated_response(
            ProposedActionSerializer(page, many=True, context=context).data
        )


class ProposedActionGenerateView(APIView):
    """POST /api/actions/{id}/generate/ — draft the copy for ONE proposal.

    201 with the inbox row, 404 for a proposal that is not the requester's or
    never chose an action, 409 when it is already drafted or was dismissed —
    and no provider call in either refusal.
    """

    def post(self, request, pk, *args, **kwargs):
        job = services.proposal_for(request.user, pk)
        if job is None:
            raise NotFound("No proposed action with this id.")
        try:
            action = services.compose(job)
        except services.NothingToCompose as exc:
            raise ContractError(
                "no_new_recommendation", str(exc), status_code=status.HTTP_409_CONFLICT
            )
        return Response(ReviewItemSerializer(action).data, status=status.HTTP_201_CREATED)


# Appended to the `api/` urlpatterns as flat patterns, as the rules ones are:
# the auth suite audits every pattern's permission classes and expects callbacks.
urlpatterns = [
    path("actions/", ProposedActionListView.as_view(), name="actions-list"),
    path(
        "actions/<int:pk>/generate/", ProposedActionGenerateView.as_view(), name="actions-generate"
    ),
]
