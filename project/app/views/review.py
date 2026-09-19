"""The review surface: one inbox list and five lifecycle moves.

``pending -> approved | dismissed``, and either decision reopens to ``pending``.
Reopening a dismissal also revokes its suppression row, so the planner offers
the recommendation again on the next run.
"""

from typing import NamedTuple

from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from project.app.models import DismissedOutreachKey, Event, OutreachGeneratedCopy
from project.app.rules.models import resolved_priority
from project.app.serializers import ReviewItemSerializer
from project.app.services import dedupe, outreach, queue_copy


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


# The two halves a reviewer edits; sent together or not at all.
PAIR_FIELDS = ("subject", "body")


class CandidateCopy(NamedTuple):
    """One proposed draft in both representations, which always agree.

    ``copy`` is the string the verifier indexes and the row stores; ``subject``
    and ``body`` are its parts.
    """

    copy: str
    subject: str
    body: str


def candidate_copy(payload):
    """The draft a request proposes, or the 400 its body earns.

    Two input forms, one stored result. ``{"copy": "<string>"}`` sends the whole
    draft and the parts are split out of it; ``{"subject": ..., "body": ...}``
    sends the parts and the draft is composed from them, through the producers'
    own composer -- so a dry run and the row it becomes cannot disagree about
    the string. Returns ``(candidate, None)`` or ``(None, response)``.
    """
    sent_pair = [field for field in PAIR_FIELDS if field in payload]
    if "copy" in payload and sent_pair:
        return None, error(
            "validation_error",
            "Send either `copy` or the `subject` and `body` pair, not both.",
            status.HTTP_400_BAD_REQUEST,
        )

    if sent_pair:
        if len(sent_pair) != len(PAIR_FIELDS):
            return None, error(
                "validation_error",
                "`subject` and `body` are sent together.",
                status.HTTP_400_BAD_REQUEST,
            )
        subject, body = payload["subject"], payload["body"]
        if not isinstance(subject, str) or not isinstance(body, str):
            return None, error(
                "validation_error",
                "`subject` and `body` must be strings.",
                status.HTTP_400_BAD_REQUEST,
            )
        # Normalized BEFORE composing, so every offset indexes what is stored,
        # and flattened the way the composer will flatten it.
        subject = outreach.one_line(subject)
        body = queue_copy.normalize_copy(body).strip()
        if not subject or not body:
            return None, error(
                "empty_copy",
                "The subject and the body are both required.",
                status.HTTP_400_BAD_REQUEST,
            )
        return CandidateCopy(outreach.compose_email(subject, body), subject, body), None

    raw = payload.get("copy")
    if not isinstance(raw, str):
        return None, error(
            "validation_error",
            "Send `copy`, or the `subject` and `body` pair.",
            status.HTTP_400_BAD_REQUEST,
        )
    copy = queue_copy.normalize_copy(raw)
    if not copy.strip():
        return None, error(
            "empty_copy",
            "The copy cannot be empty.",
            status.HTTP_400_BAD_REQUEST,
        )
    return CandidateCopy(copy, *outreach.split_email(copy)), None


def review_queryset():
    """The base queryset for every review read.

    ``action`` is joined so an engine draft's own label and urgency cost no
    query per row.
    """
    return OutreachGeneratedCopy.objects.select_related("lead", "action").prefetch_related(
        Prefetch("lead__events", queryset=Event.objects.order_by("-timestamp", "-id"))
    )


def latest_action_ids():
    """Ids of the most recent action per lead, in review order.

    One values-only query: the table is walked to pick the survivors, but
    nothing is serialized until the page is sliced out of this list. The sort
    is Python, not SQL, so an engine draft's null ``priority`` would raise
    rather than mis-sort: it is resolved to its action's urgency here, exactly
    as the serializer resolves the one it reports.
    """
    seen = set()
    ordered = []
    for pk, lead_id, priority, urgency in OutreachGeneratedCopy.objects.order_by(
        "lead_id", "-created_at", "-id"
    ).values_list("id", "lead_id", "priority", "action__urgency"):
        if lead_id in seen:
            continue
        seen.add(lead_id)
        ordered.append((resolved_priority(priority, urgency), lead_id, pk))
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

    def get_action(self, pk):
        return review_queryset().filter(pk=pk).first()


class ReviewListView(ReviewBaseView):
    """GET /api/outreach/ — the inbox: latest action per lead, paginated."""

    # Picked up by the global ScopedRateThrottle: the list is the one endpoint
    # a page load always hits.
    throttle_scope = "outreach_list"
    throttle_detail = "Too many outreach list requests."

    def get(self, request, *args, **kwargs):
        paginator = ReviewPagination()
        page_ids = paginator.paginate_queryset(latest_action_ids(), request, view=self)
        by_id = {action.id: action for action in review_queryset().filter(pk__in=page_ids)}
        items = [by_id[pk] for pk in page_ids if pk in by_id]
        return paginator.get_paginated_response(ReviewItemSerializer(items, many=True).data)


class ReviewMutationView(ReviewBaseView):
    """POST ``/api/outreach/{id}/<verb>/`` — one item, one lifecycle move.

    Subclasses implement ``mutate``.
    """

    def post(self, request, pk, *args, **kwargs):
        action = self.get_action(pk)
        if action is None:
            return not_found()
        return self.mutate(request, action)

    def mutate(self, request, action):  # pragma: no cover - abstract
        raise NotImplementedError


class ReviewEditView(ReviewMutationView):
    """POST /api/outreach/{id}/edit/ — persist a reviewer's edit of the copy.

    ``suggested_copy`` is never touched: the edit lands in ``edited_copy`` with
    its two halves beside it, and ``{"copy": null}`` reverts by clearing all
    three. The edit is sent as a whole string or as its ``subject``/``body``
    pair; see :func:`candidate_copy`.
    """

    def mutate(self, request, action):
        if action.status not in OutreachGeneratedCopy.EDITABLE_STATUSES:
            return invalid_transition(action, "edit")

        payload = body_of(request)
        # The one revert: `copy: null` explicitly, not merely a body with no copy.
        if payload.get("copy", False) is None:
            new_copy = action.suggested_copy
            action.edited_copy = action.edited_subject = action.edited_body = ""
        else:
            candidate, refusal = candidate_copy(payload)
            if refusal is not None:
                return refusal
            new_copy = candidate.copy
            action.edited_copy = candidate.copy
            action.edited_subject = candidate.subject
            action.edited_body = candidate.body

        report = queue_copy.build_verification(action.lead, new_copy, action.action_type)
        action.verification = report
        action.save(update_fields=["edited_copy", "edited_subject", "edited_body", "verification"])

        return Response(self.serialize(action), status=status.HTTP_200_OK)


class ReviewVerifyView(ReviewMutationView):
    """POST /api/outreach/{id}/verify/ — a DRY RUN over candidate copy."""

    # Picked up by the global ScopedRateThrottle in settings.REST_FRAMEWORK, so
    # key repeat in the inline editor cannot hammer the verifier.
    throttle_scope = "copy_verify"
    # Read by the contract exception handler to lead the 429 sentence.
    throttle_detail = "Too many verification requests."

    def mutate(self, request, action):
        candidate, refusal = candidate_copy(body_of(request))
        if refusal is not None:
            return refusal
        report = queue_copy.build_verification(action.lead, candidate.copy, action.action_type)
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
        if not action.can_transition_to(OutreachGeneratedCopy.STATUS_APPROVED):
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

        action.status = OutreachGeneratedCopy.STATUS_APPROVED
        action.status_changed_at = timezone.now()
        action.verification = report
        action.save(update_fields=["status", "status_changed_at", "verification"])

        return Response(self.serialize(action), status=status.HTTP_200_OK)


class ReviewDismissView(ReviewMutationView):
    """POST /api/outreach/{id}/dismiss/ — gone, and it does not come back."""

    def mutate(self, request, action):
        if not action.can_transition_to(OutreachGeneratedCopy.STATUS_DISMISSED):
            return invalid_transition(action, "dismiss")

        reason = body_of(request).get("reason") or ""
        if reason not in OutreachGeneratedCopy.DISMISS_REASONS and reason != "":
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
            action.status = OutreachGeneratedCopy.STATUS_DISMISSED
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
        if not action.can_transition_to(OutreachGeneratedCopy.STATUS_PENDING):
            return invalid_transition(action, "reopen")

        was_dismissed = action.status == OutreachGeneratedCopy.STATUS_DISMISSED
        now = timezone.now()
        with transaction.atomic():
            if was_dismissed:
                # Conditional UPDATE, not a read-then-check: two reviewers
                # reopening at once must not double-revoke.
                DismissedOutreachKey.objects.filter(
                    dedupe_key=action.dedupe_key, revoked_at__isnull=True
                ).update(revoked_at=now)
            action.status = OutreachGeneratedCopy.STATUS_PENDING
            action.status_changed_at = now
            action.save(update_fields=["status", "status_changed_at"])

        return Response(self.serialize(action), status=status.HTTP_200_OK)
