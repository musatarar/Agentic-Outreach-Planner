"""Triage queue API (MUS-39).

Plain ``APIView`` throughout, matching the rest of this API: no routers, no
ViewSets, no pagination. `GET /api/queue/` returns the whole queue in one call
because it is tens of items, not thousands, and because the inbox owes a
sub-120ms row advance with no spinner -- which is only achievable if advancing
a row performs zero network requests.

Errors use the contract's uniform envelope, ``{"code", "detail"}``, emitted
directly by these views rather than left to MUS-37's exception handler: the
codes are pinned per endpoint (section 5.3), and returning them here keeps the
shape identical whether or not that handler is installed.
"""

import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from project.app.models import DismissedOutreachKey, Event, OutreachAction, OutreachEdit
from project.app.serializers_queue import QueueItemSerializer, iso
from project.app.services import dedupe, queue_copy

DONE_STATUSES = (
    OutreachAction.STATUS_APPROVED,
    OutreachAction.STATUS_SNOOZED,
    OutreachAction.STATUS_DISMISSED,
)

SNOOZE_OFFSET_DAYS = {
    OutreachAction.TRIGGER_TOMORROW: 1,
    OutreachAction.TRIGGER_IN_3_DAYS: 3,
}

# The hour, in TRIAGE_TIMEZONE, a date-based snooze returns at. Not midnight:
# a snoozed lead should reappear at the top of a working day, not overnight.
SNOOZE_HOUR = 9


def error(code, detail, status_code):
    """The one error shape in this API (section 5)."""
    return Response({"code": code, "detail": detail}, status=status_code)


def not_found():
    return error("not_found", "No queue item with that id.", status.HTTP_404_NOT_FOUND)


def invalid_transition(action, verb):
    """The 409 an illegal lifecycle move gets. Never a silent no-op."""
    return error(
        "invalid_transition",
        f'Cannot {verb} an action with status "{action.status}".',
        status.HTTP_409_CONFLICT,
    )


def editor_of(request):
    """Best-effort attribution for the audit trail; never authorization."""
    return getattr(request.user, "email", "") or ""


def body_of(request):
    return request.data if isinstance(request.data, dict) else {}


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


def snooze_target(trigger, until_raw, now):
    """Resolve a snooze trigger to ``(snooze_until, snooze_activity_after)``.

    Returns ``(None, None)`` when a ``custom`` trigger is missing or in the
    past -- the only user-supplied value here.

    Every branch produces a non-NULL ``snooze_until``, including
    ``on_activity``, which gets a backstop of
    ``TRIAGE_SNOOZE_ON_ACTIVITY_BACKSTOP_DAYS``. "Come back when they actually
    do something" for a lead that never does anything is indistinguishable from
    a dismiss nobody chose, and a non-NULL
    column means neither the unsnooze sweep nor the frontend ordering needs a
    NULL branch.
    """
    tz = ZoneInfo(settings.TRIAGE_TIMEZONE)

    if trigger == OutreachAction.TRIGGER_CUSTOM:
        parsed = parse_datetime(until_raw) if isinstance(until_raw, str) else None
        if parsed is None:
            return None, None
        if timezone.is_naive(parsed):
            parsed = parsed.replace(tzinfo=tz)
        return (parsed, None) if parsed > now else (None, None)

    if trigger == OutreachAction.TRIGGER_ON_ACTIVITY:
        backstop = datetime.timedelta(days=settings.TRIAGE_SNOOZE_ON_ACTIVITY_BACKSTOP_DAYS)
        # `now` is the watermark: events already on the record must not wake it,
        # only something the lead does AFTER the reviewer said "come back when".
        return now + backstop, now

    local = now.astimezone(tz)
    if trigger == OutreachAction.TRIGGER_NEXT_WEEK:
        offset = 7 - local.weekday()  # next Monday; on a Monday that is +7
    else:
        offset = SNOOZE_OFFSET_DAYS[trigger]
    target = datetime.datetime.combine(
        local.date() + datetime.timedelta(days=offset),
        datetime.time(hour=SNOOZE_HOUR),
        tzinfo=tz,
    )
    return target, None


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


class QueueMutationView(QueueBaseView):
    """POST /api/queue/{id}/<verb>/ -- one item, one lifecycle move.

    Subclasses implement ``mutate``; the 404 and the auth check are handled
    once, here.
    """

    def post(self, request, pk, *args, **kwargs):
        action = self.get_action(pk)
        if action is None:
            return not_found()
        return self.mutate(request, action)

    def mutate(self, request, action):  # pragma: no cover - abstract
        raise NotImplementedError


class QueueEditView(QueueMutationView):
    """POST /api/queue/{id}/edit/ -- persist a reviewer's edit of the copy.

    ``suggested_copy`` is never written. The edit lands in ``edited_copy`` and
    an OutreachEdit row records the before/after pair: that diff is the copy
    eval corpus, and it only exists if the original survives.

    ``{"copy": null}`` is "revert to original" -- it clears ``edited_copy`` and
    still writes an OutreachEdit, because reverting is itself a judgement worth
    keeping.
    """

    def mutate(self, request, action):
        if action.status not in OutreachAction.EDITABLE_STATUSES:
            return invalid_transition(action, "edit")

        body = body_of(request)
        if "copy" not in body:
            return error(
                "validation_error",
                "`copy` is required. Send null to revert to the original draft.",
                status.HTTP_400_BAD_REQUEST,
            )

        raw = body["copy"]
        if raw is None:
            new_copy, edited_copy = action.suggested_copy, ""
        else:
            if not isinstance(raw, str):
                return error(
                    "validation_error",
                    "`copy` must be a string or null.",
                    status.HTTP_400_BAD_REQUEST,
                )
            # Normalized BEFORE storage and before any offset is computed: a
            # <textarea> submits \r\n, and Python counts that as two characters
            # (section 9.1c).
            new_copy = queue_copy.normalize_copy(raw)
            if not new_copy.strip():
                return error(
                    "empty_copy",
                    "The copy cannot be empty. Send null to revert to the original draft.",
                    status.HTTP_400_BAD_REQUEST,
                )
            edited_copy = new_copy

        before = action.effective_copy
        report = queue_copy.build_verification(action.lead, new_copy, action.action_type)

        with transaction.atomic():
            action.edited_copy = edited_copy
            action.verification = report
            action.save(update_fields=["edited_copy", "verification"])
            OutreachEdit.objects.create(
                outreach_action=action,
                editor=editor_of(request),
                before_text=before,
                after_text=new_copy,
                **queue_copy.diff_edit(before, new_copy),
            )

        return Response(self.serialize(action), status=status.HTTP_200_OK)


class QueueVerifyView(QueueMutationView):
    """POST /api/queue/{id}/verify/ -- a DRY RUN over candidate copy.

    Nothing is persisted and no OutreachEdit is written; this backs live
    re-verification while the reviewer types. The response echoes the exact
    copy it verified so a debounced out-of-order reply can be discarded rather
    than rendered over newer text (section 9.2).
    """

    # Picked up by the global ScopedRateThrottle in settings.REST_FRAMEWORK, so
    # key repeat in the inline editor cannot hammer the verifier. The scope name
    # must match a key in DEFAULT_THROTTLE_RATES or DRF raises
    # ImproperlyConfigured -- there is no silently-inert middle state.
    throttle_scope = "queue_verify"
    # Read by the contract exception handler to lead the 429 sentence.
    throttle_detail = "Too many verification requests."

    def mutate(self, request, action):
        raw = body_of(request).get("copy")
        if not isinstance(raw, str) or not queue_copy.normalize_copy(raw).strip():
            return error(
                "empty_copy",
                "The copy cannot be empty.",
                status.HTTP_400_BAD_REQUEST,
            )
        report = queue_copy.build_verification(action.lead, raw, action.action_type)
        return Response(report, status=status.HTTP_200_OK)


class QueueApproveView(QueueMutationView):
    """POST /api/queue/{id}/approve/ -- ship it.

    The copy in play is already persisted via /edit/, so the body is empty.
    Blocked, server-side, when the copy makes a claim the lead record does not
    support: the frontend disables the affordance from `can_approve` and the
    server independently returns 409.
    """

    def mutate(self, request, action):
        if not action.can_transition_to(OutreachAction.STATUS_APPROVED):
            return invalid_transition(action, "approve")

        report = action.verification or {}
        if report.get("copy") != action.effective_copy:
            report = queue_copy.build_verification(
                action.lead, action.effective_copy, action.action_type
            )
        if not queue_copy.can_approve(report):
            return error(
                "unverified_claims",
                f"{report.get('unverified_count', 0)} of {report.get('checked_count', 0)} "
                "claims are unverified. Fix or revert the copy before approving.",
                status.HTTP_409_CONFLICT,
            )

        now = timezone.now()
        with transaction.atomic():
            action.status = OutreachAction.STATUS_APPROVED
            action.status_changed_at = now
            action.verification = report
            action.save(update_fields=["status", "status_changed_at", "verification"])
            # The edit that was in place at the moment of approval is the one
            # the corpus should score: it is what a human actually sent.
            latest = action.edits.order_by("-created_at", "-id").first()
            if latest is not None:
                latest.committed = True
                latest.save(update_fields=["committed"])

        return Response(self.serialize(action, now=now), status=status.HTTP_200_OK)


class QueueSnoozeView(QueueMutationView):
    """POST /api/queue/{id}/snooze/ -- not skip.

    Snooze carries a judgement about *when* the lead should come back, which is
    the thing a reviewer actually wants to express and usually cannot.
    Re-snoozing an already-snoozed item is allowed and refreshes both stamps.
    """

    def mutate(self, request, action):
        if not action.can_transition_to(OutreachAction.STATUS_SNOOZED):
            return invalid_transition(action, "snooze")

        body = body_of(request)
        trigger = body.get("trigger")
        if trigger not in dict(OutreachAction.SNOOZE_TRIGGER_CHOICES):
            return error(
                "invalid_snooze",
                f'Unknown snooze trigger "{trigger}".',
                status.HTTP_400_BAD_REQUEST,
            )

        now = timezone.now()
        until, activity_after = snooze_target(trigger, body.get("until"), now)
        if until is None:
            return error(
                "invalid_snooze",
                '`until` is required and must be in the future for trigger "custom".',
                status.HTTP_400_BAD_REQUEST,
            )

        action.status = OutreachAction.STATUS_SNOOZED
        action.status_changed_at = now
        action.snooze_until = until
        action.snooze_trigger = trigger
        action.snooze_activity_after = activity_after
        action.save(
            update_fields=[
                "status",
                "status_changed_at",
                "snooze_until",
                "snooze_trigger",
                "snooze_activity_after",
            ]
        )
        return Response(self.serialize(action, now=now), status=status.HTTP_200_OK)


class QueueDismissView(QueueMutationView):
    """POST /api/queue/{id}/dismiss/ -- gone, and it does not come back.

    Writes the suppression ledger row in the same transaction as the status
    change, so a later plan_outreach() run cannot resurrect the recommendation
    (and does not spend an LLM call discovering that).
    """

    def mutate(self, request, action):
        if not action.can_transition_to(OutreachAction.STATUS_DISMISSED):
            return invalid_transition(action, "dismiss")

        reason = body_of(request).get("reason") or ""
        if reason not in OutreachAction.DISMISS_REASONS and reason != "":
            return error(
                "invalid_reason",
                f'"{reason}" is not a recognized dismiss reason.',
                status.HTTP_400_BAD_REQUEST,
            )

        now = timezone.now()
        key = action.dedupe_key or dedupe.dedupe_key(action.lead_id, action.action_type)
        with transaction.atomic():
            action.status = OutreachAction.STATUS_DISMISSED
            action.status_changed_at = now
            action.dismiss_reason = reason
            action.dedupe_key = key
            action.save(
                update_fields=["status", "status_changed_at", "dismiss_reason", "dedupe_key"]
            )
            DismissedOutreachKey.objects.update_or_create(
                dedupe_key=key,
                defaults={
                    "lead": action.lead,
                    "action_type": action.action_type,
                    "reason": reason,
                    "dismissed_by": editor_of(request),
                    "source_action": action,
                    # A re-dismissal after an undo must suppress again.
                    "revoked_at": None,
                },
            )

        return Response(self.serialize(action, now=now), status=status.HTTP_200_OK)


#: Statuses whose reversal is time-boxed. The undo window exists to catch a
#: fat-fingered IRREVERSIBLE act -- an approve that put text on someone's
#: clipboard, a dismiss that suppresses a recommendation permanently. Snooze is
#: neither: it is a deferral, and bringing a deferred lead back early is a new
#: decision rather than the correction of a mistake, so it is never time-boxed.
UNDO_WINDOWED_STATUSES = (
    OutreachAction.STATUS_APPROVED,
    OutreachAction.STATUS_DISMISSED,
)


class QueueUndoView(QueueMutationView):
    """POST /api/queue/{id}/undo/ -- reverse the last decision.

    Undoing a DISMISS also revokes the suppression, in the same transaction.
    Without that the row goes back to pending and the UI looks perfectly
    correct, while the ledger keeps suppressing -- so the next plan_outreach()
    run, days later, quietly drops the lead (section 9.7).

    Un-snoozing is deliberately NOT time-boxed. Capping it at the undo window
    would leave `/done` showing an un-snooze control that is dead for every row
    snoozed more than five minutes ago -- which, on a view that lists a whole
    day's decisions, is nearly all of them.
    """

    def mutate(self, request, action):
        if action.status == OutreachAction.STATUS_PENDING:
            return error(
                "invalid_transition",
                "Nothing to undo — this action is already pending.",
                status.HTTP_409_CONFLICT,
            )
        if not action.can_transition_to(OutreachAction.STATUS_PENDING):
            return invalid_transition(action, "undo")

        window = settings.TRIAGE_UNDO_WINDOW_SECONDS
        now = timezone.now()
        if action.status in UNDO_WINDOWED_STATUSES:
            expires_at = action.status_changed_at + datetime.timedelta(seconds=window)
            if now >= expires_at:
                return error(
                    "undo_window_expired",
                    f"The {window // 60}-minute undo window has passed.",
                    status.HTTP_409_CONFLICT,
                )

        was_dismissed = action.status == OutreachAction.STATUS_DISMISSED
        with transaction.atomic():
            if was_dismissed:
                DismissedOutreachKey.objects.filter(
                    dedupe_key=action.dedupe_key, revoked_at__isnull=True
                ).update(revoked_at=now)
            action.status = OutreachAction.STATUS_PENDING
            action.status_changed_at = now
            action.snooze_until = None
            action.snooze_trigger = ""
            action.snooze_activity_after = None
            action.dismiss_reason = ""
            # edited_copy and the OutreachEdit rows are deliberately untouched:
            # undoing a decision is not undoing the writing.
            action.save(
                update_fields=[
                    "status",
                    "status_changed_at",
                    "snooze_until",
                    "snooze_trigger",
                    "snooze_activity_after",
                    "dismiss_reason",
                ]
            )

        return Response(self.serialize(action, now=now), status=status.HTTP_200_OK)
