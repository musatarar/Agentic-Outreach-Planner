"""Generated copy on the wire: the review item.

``ReviewItemSerializer`` is complete on purpose: advancing a row in the inbox
must perform zero extra network requests, so the list endpoint and every
mutation return whole items in one shape.
"""

from rest_framework import serializers

from project.app.models import Lead, OutreachGeneratedCopy
from project.app.rules.models import resolved_priority
from project.app.services import queue_copy
from project.app.services.actions import ACTION_META

# The only event data the frontend gets -- the inbox needs no second request.
RECENT_EVENT_LIMIT = 5

# Rendered server-side so the surfaces that show activity cannot drift.
EVENT_SUMMARIES = {
    "login": "Portal login",
    "quote_created": "Quote created",
    "quote_submitted": "Quote submitted",
    "deal_closed": "Deal closed",
    "call_logged": "Call logged",
    "email_sent": "Email sent",
    "demo_completed": "Demo completed",
    "onboarding_call": "Onboarding call",
}

_DATETIME = serializers.DateTimeField()


def priority_of(obj):
    """The inbox priority of one row, by the same rule the inbox sorts on."""
    return resolved_priority(obj.priority, obj.action.urgency if obj.action_id else None)


def _event_summary(event):
    return EVENT_SUMMARIES.get(event.type) or event.type.replace("_", " ").capitalize()


class ReviewLeadSerializer(serializers.ModelSerializer):
    """The lead as the review inbox needs it, with its recent activity."""

    recent_events = serializers.SerializerMethodField()

    class Meta:
        model = Lead
        fields = [
            "id",
            "agency_name",
            "contact_name",
            "contact_email",
            "state",
            "stage",
            "num_producers",
            "estimated_book_size_usd",
            "quotes_created",
            "quotes_submitted",
            "deals_closed",
            "signed_up_date",
            "last_login_date",
            "last_contacted_date",
            "recent_events",
        ]

    def get_recent_events(self, obj):
        # Sliced in Python off the prefetched queryset; slicing inside the
        # Prefetch would re-query per lead and blow the constant query count.
        events = list(obj.events.all())[:RECENT_EVENT_LIMIT]
        return [
            {
                "type": event.type,
                "timestamp": _DATETIME.to_representation(event.timestamp),
                "summary": _event_summary(event),
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
    priority = serializers.SerializerMethodField()
    effective_copy = serializers.SerializerMethodField()
    effective_subject = serializers.SerializerMethodField()
    effective_body = serializers.SerializerMethodField()
    is_edited = serializers.SerializerMethodField()
    verification = serializers.SerializerMethodField()
    can_approve = serializers.SerializerMethodField()

    class Meta:
        model = OutreachGeneratedCopy
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
            "subject",
            "body",
            "edited_copy",
            "edited_subject",
            "edited_body",
            "effective_copy",
            "effective_subject",
            "effective_body",
            "is_edited",
            "verification",
            "can_approve",
        ]

    def get_action_label(self, obj):
        # The owner's own label when the draft came from their catalog; the
        # planner's constants otherwise, which have no entry for a user key.
        if obj.action_id is not None:
            return obj.action.label
        meta = ACTION_META.get(obj.action_type) or {}
        return meta.get("label", obj.action_type)

    def get_priority(self, obj):
        return priority_of(obj)

    def get_effective_copy(self, obj):
        return obj.effective_copy

    def get_effective_subject(self, obj):
        return obj.effective_subject

    def get_effective_body(self, obj):
        return obj.effective_body

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
