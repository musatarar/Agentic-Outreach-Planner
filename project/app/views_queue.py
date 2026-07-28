"""Triage queue API (MUS-39).

Plain ``APIView`` throughout, matching the rest of this API: no routers, no
ViewSets, no pagination. `GET /api/queue/` returns the whole queue in one call
because it is tens of items, not thousands, and because the inbox owes a
sub-120ms row advance with no spinner -- which is only achievable if advancing
a row performs zero network requests (CONTRACT MUS-35 section 9.14).

Errors use the contract's uniform envelope, ``{"code", "detail"}``, emitted
directly by these views rather than left to MUS-37's exception handler: the
codes are pinned per endpoint (section 5.3), and returning them here keeps the
shape identical whether or not that handler is installed.
"""

import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Count, Prefetch, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from project.app.models import Event, OutreachAction
from project.app.serializers_queue import QueueItemSerializer, iso

DONE_STATUSES = (
    OutreachAction.STATUS_APPROVED,
    OutreachAction.STATUS_SNOOZED,
    OutreachAction.STATUS_DISMISSED,
)


def error(code, detail, status_code):
    """The one error shape in this API (section 5)."""
    return Response({"code": code, "detail": detail}, status=status_code)


def not_found():
    return error("not_found", "No queue item with that id.", status.HTTP_404_NOT_FOUND)


def queue_queryset():
    """The base queryset for every queue read.

    ``select_related("lead")`` plus ONE prefetch of the lead's events, ordered
    in SQL and sliced in Python by the serializer. Slicing inside the Prefetch
    queryset makes Django re-query per object, which is exactly how the
    existing N+1 in ``_events_list`` would land in the hot path.
    """
    return OutreachAction.objects.select_related("lead").prefetch_related(
        Prefetch("lead__events", queryset=Event.objects.order_by("-timestamp", "-id"))
    )


def triage_day():
    """(now, today, start, end) for the server's idea of "today".

    The server decides the day boundary, in ``settings.TRIAGE_TIMEZONE``, and
    returns it. A reviewer in UTC-7 clearing the queue at 6pm local would
    otherwise see ``0 / 0 today`` if the frontend computed it (section 9.5).
    """
    tz = ZoneInfo(settings.TRIAGE_TIMEZONE)
    now = timezone.now()
    today = now.astimezone(tz).date()
    start = datetime.datetime.combine(today, datetime.time.min, tzinfo=tz)
    return now, today, start, start + datetime.timedelta(days=1)


def _today(field_status, start, end):
    return Q(status=field_status, status_changed_at__gte=start, status_changed_at__lt=end)


def day_counts(start, end):
    """One aggregate query for the whole `03 / 14 today` header."""
    agg = OutreachAction.objects.aggregate(
        remaining=Count("id", filter=Q(status=OutreachAction.STATUS_PENDING)),
        approved_today=Count("id", filter=_today(OutreachAction.STATUS_APPROVED, start, end)),
        snoozed_today=Count("id", filter=_today(OutreachAction.STATUS_SNOOZED, start, end)),
        dismissed_today=Count("id", filter=_today(OutreachAction.STATUS_DISMISSED, start, end)),
    )
    done_today = agg["approved_today"] + agg["snoozed_today"] + agg["dismissed_today"]
    return {
        "total_today": done_today + agg["remaining"],
        "done_today": done_today,
        "remaining": agg["remaining"],
        "approved_today": agg["approved_today"],
        "snoozed_today": agg["snoozed_today"],
        "dismissed_today": agg["dismissed_today"],
    }


class QueueBaseView(APIView):
    """Authenticated by default.

    Set explicitly rather than inherited from the global default MUS-37 adds,
    so the queue is never accidentally public if that setting is reordered.
    """

    permission_classes = [IsAuthenticated]

    def serialize(self, action, now=None):
        return QueueItemSerializer(action, context={"now": now or timezone.now()}).data

    def get_action(self, pk):
        return queue_queryset().filter(pk=pk).first()


class QueueListView(QueueBaseView):
    """GET /api/queue/ -- every pending item, complete, in one call."""

    def get(self, request, *args, **kwargs):
        now, today, start, end = triage_day()
        items = queue_queryset().filter(status=OutreachAction.STATUS_PENDING)
        items = items.order_by("priority", "lead_id")
        return Response(
            {
                "date": today.isoformat(),
                "timezone": settings.TRIAGE_TIMEZONE,
                "counts": day_counts(start, end),
                "items": QueueItemSerializer(items, many=True, context={"now": now}).data,
            },
            status=status.HTTP_200_OK,
        )


class QueueDetailView(QueueBaseView):
    """GET /api/queue/{id}/ -- refresh-after-mutation and deep links."""

    def get(self, request, pk, *args, **kwargs):
        action = self.get_action(pk)
        if action is None:
            return not_found()
        return Response(self.serialize(action), status=status.HTTP_200_OK)


class QueueDoneView(QueueBaseView):
    """GET /api/queue/done/ -- everything decided today, newest first."""

    def get(self, request, *args, **kwargs):
        now, today, start, end = triage_day()
        items = list(
            queue_queryset()
            .filter(status__in=DONE_STATUSES, status_changed_at__gte=start)
            .filter(status_changed_at__lt=end)
            .order_by("-status_changed_at", "-id")
        )
        counts = day_counts(start, end)
        stamps = [item.status_changed_at for item in items]
        total = len(items)
        return Response(
            {
                "date": today.isoformat(),
                "timezone": settings.TRIAGE_TIMEZONE,
                "summary": {
                    "approved": counts["approved_today"],
                    "snoozed": counts["snoozed_today"],
                    "dismissed": counts["dismissed_today"],
                    "total": total,
                    # The single flag that selects the celebration state. The
                    # frontend must not infer it from array lengths.
                    "queue_cleared": counts["remaining"] == 0 and total > 0,
                    "pipeline_value_usd": sum(
                        item.lead.estimated_book_size_usd
                        for item in items
                        if item.status == OutreachAction.STATUS_APPROVED
                    ),
                    "elapsed_seconds": (
                        int((max(stamps) - min(stamps)).total_seconds()) if total > 1 else None
                    ),
                    "first_action_at": iso(min(stamps)) if stamps else None,
                    "last_action_at": iso(max(stamps)) if stamps else None,
                },
                "items": QueueItemSerializer(items, many=True, context={"now": now}).data,
            },
            status=status.HTTP_200_OK,
        )
