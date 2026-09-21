"""Outreach actions on the wire: the review item.

``ReviewItemSerializer`` is complete on purpose: advancing a row in the inbox
must perform zero extra network requests, so the list endpoint and every
mutation return whole items in one shape.
"""

from rest_framework import serializers

from project.app.models import Lead, OutreachAction
from project.app.services import queue_copy
from project.app.services.actions import ACTION_META

# The only event data the frontend gets -- the inbox needs no second request.
RECENT_EVENT_LIMIT = 5

_DATETIME = serializers.DateTimeField()


class ReviewLeadSerializer(serializers.ModelSerializer):
    """The lead as the review inbox needs it, with its recent activity.

    The blob goes over the wire whole; what its keys mean is the reader's
    shape to say, not this serializer's.
    """

    recent_events = serializers.SerializerMethodField()

    class Meta:
        model = Lead
        fields = ["id", "data", "recent_events"]

    def get_recent_events(self, obj):
        # Sliced in Python off the prefetched queryset; slicing inside the
        # Prefetch would re-query per lead and blow the constant query count.
        events = list(obj.events.all())[:RECENT_EVENT_LIMIT]
        return [
            {
                "timestamp": _DATETIME.to_representation(event.timestamp),
                "data": event.data,
            }
            for event in events
        ]


class ReviewItemSerializer(serializers.ModelSerializer):
    """One recommendation as the reviewer sees it, complete.

    Returned identically by ``GET /api/outreach/`` and every
    ``/api/outreach/{id}/<verb>/`` mutation, so the frontend has exactly one
    shape to render.
    """

    lead = ReviewLeadSerializer(read_only=True)
    action_label = serializers.SerializerMethodField()
    effective_copy = serializers.SerializerMethodField()
    is_edited = serializers.SerializerMethodField()
    verification = serializers.SerializerMethodField()
    can_approve = serializers.SerializerMethodField()

    class Meta:
        model = OutreachAction
        fields = [
            "id",
            "status",
            "status_changed_at",
            "priority",
            "action_type",
            "action_label",
            "reason",
            "needs_human",
            "further_action",
            "created_at",
            "dedupe_key",
            "lead",
            "suggested_copy",
            "edited_copy",
            "effective_copy",
            "is_edited",
            "verification",
            "can_approve",
        ]

    def get_action_label(self, obj):
        meta = ACTION_META.get(obj.action_type) or {}
        return meta.get("label", obj.action_type)

    def get_effective_copy(self, obj):
        return obj.effective_copy

    def get_is_edited(self, obj):
        return bool(obj.edited_copy)

    def _report(self, obj):
        """The verification report describing ``effective_copy``.

        ``verification.copy == effective_copy`` is an invariant; this only
        recomputes for rows written before the verifier existed.
        """
        cached = getattr(obj, "_review_report", None)
        if cached is not None:
            return cached
        report = obj.verification or {}
        if report.get("copy") != obj.effective_copy:
            report = queue_copy.build_verification(obj.lead, obj.effective_copy, obj.action_type)
        obj._review_report = report
        return report

    def get_verification(self, obj):
        return self._report(obj)

    def get_can_approve(self, obj):
        # Derived from the same report as `verification`, so the two can never
        # disagree.
        return queue_copy.can_approve(self._report(obj))
