"""Serializers for the triage queue API (MUS-39).

A separate module from ``serializers.py`` on purpose: MUS-37 and MUS-39 build
in parallel, and new view/serializer modules are the whole conflict-avoidance
strategy for this feature branch (CONTRACT MUS-35 section 8.1).

Everything a reviewer needs to judge one recommendation is in ``QueueItem``:
the lead, the structured rule trace, the verification spans and the copy.
That is deliberate and load-bearing -- advancing a row in the inbox must
perform zero network requests (section 9.14), which is only possible if the
list endpoint already returned complete items.
"""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers

from project.app.models import Lead, OutreachAction
from project.app.services import queue_copy
from project.app.services.actions import ACTION_META

# Only the newest few events, rendered server-side. This is the ONLY event data
# the frontend gets, and it exists so the inbox needs no second request.
RECENT_EVENT_LIMIT = 5

# Human summaries for the event types the ingest pipeline produces. Rendered
# here rather than in the frontend so the three surfaces that show activity
# cannot drift.
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


def iso(value):
    """Render a datetime the way every other timestamp in this API is rendered."""
    return _DATETIME.to_representation(value)


def _event_summary(event):
    return EVENT_SUMMARIES.get(event.type) or event.type.replace("_", " ").capitalize()


class QueueLeadSerializer(serializers.ModelSerializer):
    """The lead as the triage inbox needs it, with its recent activity."""

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
        # Sliced in PYTHON, off a prefetched, already-ordered queryset. Slicing
        # inside the Prefetch queryset instead would make Django re-query once
        # per lead and blow the constant-query-count requirement (section 5.2).
        events = list(obj.events.all())[:RECENT_EVENT_LIMIT]
        return [
            {
                "type": event.type,
                "timestamp": _DATETIME.to_representation(event.timestamp),
                "summary": _event_summary(event),
            }
            for event in events
        ]


class QueueItemSerializer(serializers.ModelSerializer):
    """One triage recommendation, complete.

    Returned identically by ``GET /api/queue/``, ``GET /api/queue/{id}/`` and
    every mutation, so the frontend has exactly one shape to render.
    """

    lead = QueueLeadSerializer(read_only=True)
    action_label = serializers.SerializerMethodField()
    effective_copy = serializers.SerializerMethodField()
    is_edited = serializers.SerializerMethodField()
    verification = serializers.SerializerMethodField()
    can_approve = serializers.SerializerMethodField()
    snooze = serializers.SerializerMethodField()
    undo = serializers.SerializerMethodField()

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
            "rule_trace",
            "verification",
            "can_approve",
            "snooze",
            "dismiss_reason",
            "undo",
        ]

    # -- derived copy fields ------------------------------------------------

    def get_action_label(self, obj):
        meta = ACTION_META.get(obj.action_type) or {}
        return meta.get("label", obj.action_type)

    def get_effective_copy(self, obj):
        return obj.effective_copy

    def get_is_edited(self, obj):
        return bool(obj.edited_copy)

    def _report(self, obj):
        """The verification report describing ``effective_copy``.

        ``verification.copy == effective_copy`` is an invariant (section 9.2):
        spans computed against one string and rendered over another underline
        the wrong words. The stored report is authoritative and is rewritten on
        every copy change; this only recomputes for a row written before the
        verifier existed, so the invariant holds unconditionally.
        """
        cached = getattr(obj, "_queue_report", None)
        if cached is not None:
            return cached
        report = obj.verification or {}
        if report.get("copy") != obj.effective_copy:
            report = queue_copy.build_verification(obj.lead, obj.effective_copy, obj.action_type)
        obj._queue_report = report
        return report

    def get_verification(self, obj):
        return self._report(obj)

    def get_can_approve(self, obj):
        # A mirror of verification.can_approve, derived from the same value so
        # the two can never disagree. The server, not the client, decides.
        return queue_copy.can_approve(self._report(obj))

    # -- lifecycle fields ---------------------------------------------------

    def get_snooze(self, obj):
        return {
            "until": _DATETIME.to_representation(obj.snooze_until),
            "trigger": obj.snooze_trigger,
            "activity_after": _DATETIME.to_representation(obj.snooze_activity_after),
        }

    def get_undo(self, obj):
        """Server-computed undo window (section 9.6).

        ``expires_at`` is an absolute server timestamp. Clock skew between a
        browser and the server is real and unbounded, so the frontend renders a
        countdown from it but never gates the request on it.

        A SNOOZED row is ``{"available": true, "expires_at": null}`` -- null
        meaning "no deadline", not "expired". Un-snoozing is a new decision
        rather than the correction of a mistake, so it is never time-boxed, and
        this field has to say so or the UI hides a control the server would
        have honoured. See ``views_queue.UNDO_WINDOWED_STATUSES``.
        """
        if obj.status == OutreachAction.STATUS_PENDING or obj.status_changed_at is None:
            return {"available": False, "expires_at": None}
        if obj.status == OutreachAction.STATUS_SNOOZED:
            return {"available": True, "expires_at": None}
        expires_at = obj.status_changed_at + timedelta(seconds=settings.TRIAGE_UNDO_WINDOW_SECONDS)
        now = self.context.get("now") or timezone.now()
        return {
            "available": now < expires_at,
            "expires_at": _DATETIME.to_representation(expires_at),
        }
