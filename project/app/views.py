from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from project.app.models import Lead, OutreachAction, ReviewDecision
from project.app.serializers import (
    LeadSerializer,
    OutreachActionSerializer,
    ReviewDecisionSerializer,
)
from project.app.services.actions import ACTION_META, SELECTABLE_ACTION_TYPES


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
        latest = OutreachAction.objects.select_related("lead").order_by(
            "lead_id", "-created_at", "-id"
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


class OutreachReportView(APIView):
    """GET /api/reports/ — full outreach action history, newest first."""

    def get(self, request, *args, **kwargs):
        actions = OutreachAction.objects.select_related("lead").order_by(
            "-created_at", "-id"
        )
        serializer = OutreachActionSerializer(actions, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class ReviewQueueView(APIView):
    """GET /api/review-queue/ — needs_human actions awaiting a decision."""

    def get(self, request, *args, **kwargs):
        decided_ids = set(ReviewDecision.objects.values_list("outreach_action_id", flat=True))

        latest = OutreachAction.objects.select_related("lead").order_by(
            "lead_id", "-created_at", "-id"
        )
        seen = set()
        items = []
        for action in latest:
            if action.lead_id in seen:
                continue
            seen.add(action.lead_id)
            if not action.needs_human:
                continue
            if action.id in decided_ids:
                continue
            items.append(action)

        items.sort(key=lambda a: (a.priority, a.lead_id))

        action_options = [
            {
                "value": k,
                "label": ACTION_META[k]["label"],
                "urgency": ACTION_META[k]["urgency"],
            }
            for k in SELECTABLE_ACTION_TYPES
        ]

        return Response(
            {
                "items": OutreachActionSerializer(items, many=True).data,
                "action_options": action_options,
            },
            status=status.HTTP_200_OK,
        )


class ReviewDecisionListCreateView(APIView):
    """GET/POST /api/review-decisions/."""

    def get(self, request, *args, **kwargs):
        qs = ReviewDecision.objects.all().order_by("-created_at", "-id")
        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        serializer = ReviewDecisionSerializer(qs, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request, *args, **kwargs):
        serializer = ReviewDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            # Savepoint so the IntegrityError doesn't poison the surrounding
            # transaction (ATOMIC_REQUESTS / TestCase) and we can return cleanly.
            with transaction.atomic():
                serializer.save()
        except IntegrityError:
            # OneToOne unique constraint: a decision already exists for this
            # action (double-click / concurrent reviewers). It's already resolved.
            return Response(
                {"outreach_action": "A decision already exists for this action."},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class LeadListView(APIView):
    """GET /api/leads/ — all leads."""

    def get(self, request, *args, **kwargs):
        leads = Lead.objects.all().order_by("id")
        serializer = LeadSerializer(leads, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)
