"""The review surface: one inbox list and five lifecycle moves.

``pending -> approved | dismissed``, and either decision reopens to ``pending``.
Reopening a dismissal also revokes its suppression row, so the planner offers
the recommendation again on the next run.
"""

from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from project.app.models import DismissedOutreachKey, Event, OutreachAction
from project.app.serializers import ReviewItemSerializer
from project.app.services import dedupe, queue_copy


def error(code, detail, status_code):
    """The one error shape in this API."""
    return Response({"code": code, "detail": detail}, status=status_code)


def not_found():
    return error("not_found", "No outreach action with that id.", status.HTTP_404_NOT_FOUND)


def invalid_transition(action, verb):
    """The 409 an illegal lifecycle move gets. Never a silent no-op."""
    return error(
        "invalid_transition",
        f'Cannot {verb} an action with status "{action.status}".',
        status.HTTP_409_CONFLICT,
    )


def reviewer_of(request):
    """Best-effort attribution for the audit trail; never authorization."""
    return getattr(request.user, "email", "") or ""


def body_of(request):
    return request.data if isinstance(request.data, dict) else {}


def review_queryset(user):
    """The base queryset for every review read, scoped to ``user``'s leads.

    Every read goes through here, so an action about another tenant's lead is
    invisible to the list AND to the five lifecycle moves -- a 404, not a 403.
    """
    return (
        OutreachAction.objects.for_tenant(user)
        .select_related("lead")
        .prefetch_related(
            Prefetch("lead__events", queryset=Event.objects.order_by("-timestamp", "-id"))
        )
    )


def latest_action_ids(user):
    """Ids of the most recent action per lead, in review order.

    One values-only query: the table is walked to pick the survivors, but
    nothing is serialized until the page is sliced out of this list.
    """
    seen = set()
    ordered = []
    for pk, lead_id, priority in (
        OutreachAction.objects.for_tenant(user)
        .order_by("lead_id", "-created_at", "-id")
        .values_list("id", "lead_id", "priority")
    ):
        if lead_id in seen:
            continue
        seen.add(lead_id)
        ordered.append((priority, lead_id, pk))
    ordered.sort()
    return [pk for _priority, _lead_id, pk in ordered]


class ReviewPagination(PageNumberPagination):
    """``?page=`` / ``?page_size=``; the default page size is settings.PAGE_SIZE."""

    page_size_query_param = "page_size"
    max_page_size = 100


class ReviewBaseView(APIView):
    """Authenticated by default (settings.REST_FRAMEWORK)."""

    def serialize(self, action):
        return ReviewItemSerializer(action).data

    def get_action(self, request, pk):
        return review_queryset(request.user).filter(pk=pk).first()


class ReviewListView(ReviewBaseView):
    """GET /api/outreach/ — the inbox: latest action per lead, paginated."""

    # Picked up by the global ScopedRateThrottle: the list is the one endpoint
    # a page load always hits.
    throttle_scope = "outreach_list"
    throttle_detail = "Too many outreach list requests."

    def get(self, request, *args, **kwargs):
        paginator = ReviewPagination()
        page_ids = paginator.paginate_queryset(latest_action_ids(request.user), request, view=self)
        by_id = {
            action.id: action for action in review_queryset(request.user).filter(pk__in=page_ids)
        }
        items = [by_id[pk] for pk in page_ids if pk in by_id]
        return paginator.get_paginated_response(ReviewItemSerializer(items, many=True).data)


class ReviewMutationView(ReviewBaseView):
    """POST ``/api/outreach/{id}/<verb>/`` — one item, one lifecycle move.

    Subclasses implement ``mutate``.
    """

    def post(self, request, pk, *args, **kwargs):
        action = self.get_action(request, pk)
        if action is None:
            return not_found()
        return self.mutate(request, action)

    def mutate(self, request, action):  # pragma: no cover - abstract
        raise NotImplementedError


class ReviewEditView(ReviewMutationView):
    """POST /api/outreach/{id}/edit/ — persist a reviewer's edit of the copy.

    ``suggested_copy`` is never touched: the edit lands in ``edited_copy`` and
    ``{"copy": null}`` reverts by clearing it.
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
            # Normalized BEFORE storage and before any offset is computed
            new_copy = queue_copy.normalize_copy(raw)
            if not new_copy.strip():
                return error(
                    "empty_copy",
                    "The copy cannot be empty. Send null to revert to the original draft.",
                    status.HTTP_400_BAD_REQUEST,
                )
            edited_copy = new_copy

        report = queue_copy.build_verification(action.lead, new_copy, action.action_type)

        action.edited_copy = edited_copy
        action.verification = report
        action.save(update_fields=["edited_copy", "verification"])

        return Response(self.serialize(action), status=status.HTTP_200_OK)


class ReviewVerifyView(ReviewMutationView):
    """POST /api/outreach/{id}/verify/ — a DRY RUN over candidate copy."""

    # Picked up by the global ScopedRateThrottle in settings.REST_FRAMEWORK, so
    # key repeat in the inline editor cannot hammer the verifier.
    throttle_scope = "copy_verify"
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


class ReviewApproveView(ReviewMutationView):
    """POST /api/outreach/{id}/approve/ — the copy is good to send.

    The copy in play is already persisted via /edit/, so the body is empty.
    Blocked, server-side, when the copy makes a claim the lead record does not
    support: the frontend disables the affordance from `can_approve` and the
    server independently returns 409. Nothing is sent: approving marks the
    draft fit to leave via the clipboard.
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

        action.status = OutreachAction.STATUS_APPROVED
        action.status_changed_at = timezone.now()
        action.verification = report
        action.save(update_fields=["status", "status_changed_at", "verification"])

        return Response(self.serialize(action), status=status.HTTP_200_OK)


class ReviewDismissView(ReviewMutationView):
    """POST /api/outreach/{id}/dismiss/ — gone, and it does not come back."""

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
        # The row and its suppression land together: a dismissal the planner
        # does not see would resurrect the recommendation on the next run.
        with transaction.atomic():
            action.status = OutreachAction.STATUS_DISMISSED
            action.status_changed_at = now
            action.dedupe_key = key
            action.save(update_fields=["status", "status_changed_at", "dedupe_key"])
            DismissedOutreachKey.objects.update_or_create(
                dedupe_key=key,
                defaults={
                    "lead": action.lead,
                    "action_type": action.action_type,
                    # The dismissal reason lives on the ledger row, which
                    # outlives the action that created it.
                    "reason": reason,
                    "dismissed_by": reviewer_of(request),
                    "source_action": action,
                    # A re-dismissal after a reopen must suppress again.
                    "revoked_at": None,
                },
            )

        return Response(self.serialize(action), status=status.HTTP_200_OK)


class ReviewReopenView(ReviewMutationView):
    """POST /api/outreach/{id}/reopen/ — put a decided item back in the inbox."""

    def mutate(self, request, action):
        if not action.can_transition_to(OutreachAction.STATUS_PENDING):
            return invalid_transition(action, "reopen")

        was_dismissed = action.status == OutreachAction.STATUS_DISMISSED
        now = timezone.now()
        with transaction.atomic():
            if was_dismissed:
                # Conditional UPDATE, not a read-then-check: two reviewers
                # reopening at once must not double-revoke.
                DismissedOutreachKey.objects.filter(
                    dedupe_key=action.dedupe_key, revoked_at__isnull=True
                ).update(revoked_at=now)
            action.status = OutreachAction.STATUS_PENDING
            action.status_changed_at = now
            action.save(update_fields=["status", "status_changed_at"])

        return Response(self.serialize(action), status=status.HTTP_200_OK)
