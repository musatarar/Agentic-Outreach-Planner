"""The actions engine: enqueue a lead, claim its job, run it to a decision.

One job runs as four steps -- read the lead and the events it was queued for,
the deterministic pass (pure Python, no tokens), the inference pass for what is
left (stubbed, see :mod:`project.app.actions.inference`), then the weight tally
the rules entity already owns (``rules.services.select_action``). The chosen
action and the tally's workings land on the job; generating copy for it is the
planner's job, not this one.

Every status write is a conditional UPDATE from the status it expects, so two
crons running the same batch cannot both process a job.
"""

import datetime
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Exists, F, OuterRef
from django.utils import timezone

from project.app.actions import evaluate, inference
from project.app.actions.models import ActionJob
from project.app.models.lead import Event, Lead
from project.app.rules import services as rules_services
from project.app.rules.models import OutreachRule
from project.app.services import outreach

logger = logging.getLogger(__name__)

# How many jobs one cron tick drains; the command's --limit overrides it.
DEFAULT_BATCH_SIZE = 50


class _JobLead:
    """The lead as one job sees it: the row's own fields, but only its events.

    Duck-typed for the planner's rule helpers, which read ``lead.events`` and
    accept a plain list.
    """

    def __init__(self, lead, events):
        self._lead = lead
        self.events = list(events)

    def __getattr__(self, name):
        return getattr(self._lead, name)


# --------------------------------------------------------------------------
# the lead's catalog
# --------------------------------------------------------------------------


def rules_for_lead(lead):
    """Every enabled rule in the catalog of the user whose book this lead is in.

    An unowned lead has no rules, so its job resolves to no action.
    """
    if lead.owner_id is None:
        return OutreachRule.objects.none()
    return rules_services.enabled_rules_for(lead.owner_id)


# --------------------------------------------------------------------------
# the queue
# --------------------------------------------------------------------------


def enqueue_lead(lead, events=None):
    """Queue one lead with the events to judge it on, or return its open job.

    One open job per lead is a partial unique constraint, so this inserts and
    handles the refusal rather than reading first. ``events`` defaults to the
    lead's whole history; the job keeps that set whatever arrives later.
    """
    events = list(lead.events.all()) if events is None else list(events)
    try:
        with transaction.atomic():
            job = ActionJob.objects.create(lead=lead)
            job.events.set(events)
    except IntegrityError:
        # Whoever won the race owns the open job; None means it finished since.
        return ActionJob.objects.filter(lead=lead, status__in=ActionJob.OPEN_STATUSES).first()
    return job


def settled_lead_ids(today):
    """Leads a run already decided today with nothing new to say about them.

    A job judges the events it snapshotted at ``created_at``, so an event newer
    than that was never looked at and unsettles the lead -- ``finished_at``
    would wrongly count one that arrived mid-run. Events only accumulate, so if
    any of today's jobs has nothing newer than it, the latest one has not
    either.
    """
    newer_event = Event.objects.filter(
        lead_id=OuterRef("lead_id"), timestamp__gt=OuterRef("created_at")
    )
    return set(
        ActionJob.objects.filter(status__in=ActionJob.DECIDED_STATUSES, finished_at__date=today)
        .annotate(has_newer_event=Exists(newer_event))
        .filter(has_newer_event=False)
        .values_list("lead_id", flat=True)
    )


def enqueue_pending_leads(lead_ids=None, *, today=None):
    """Queue every lead with no open job that today has not already settled --
    the engine's ingest step."""
    today = today or datetime.date.today()
    skip = set(
        ActionJob.objects.filter(status__in=ActionJob.OPEN_STATUSES).values_list(
            "lead_id", flat=True
        )
    )
    skip |= settled_lead_ids(today)
    leads = Lead.objects.prefetch_related("events")
    if lead_ids is not None:
        leads = leads.filter(id__in=list(lead_ids))
    queued = [enqueue_lead(lead) for lead in leads if lead.id not in skip]
    return [job for job in queued if job is not None]


def claim(job):
    """Take the job off the queue, or ``False`` when another cron got there first."""
    claimed = ActionJob.objects.filter(pk=job.pk, status=ActionJob.STATUS_QUEUED).update(
        status=ActionJob.STATUS_PROCESSING,
        started_at=timezone.now(),
        attempts=F("attempts") + 1,
        error="",
    )
    if not claimed:
        return False
    # Mirrored locally rather than refetched: a refresh would drop the
    # prefetched events this job is about to be judged on.
    job.status = ActionJob.STATUS_PROCESSING
    job.attempts += 1
    return True


def _transition(job, expected, new_status, **fields):
    """Move the job with a conditional UPDATE from ``expected``.

    ``False`` means someone else moved it first, which is an outcome, not an
    error: the caller stops touching the job.
    """
    if new_status not in ActionJob.ALLOWED_TRANSITIONS.get(expected, ()):
        raise ValueError(f"{expected} -> {new_status} is not a legal transition.")
    updated = ActionJob.objects.filter(pk=job.pk, status=expected).update(
        status=new_status, **fields
    )
    if not updated:
        return False
    for field, value in fields.items():
        setattr(job, field, value)
    job.status = new_status
    return True


# --------------------------------------------------------------------------
# running one job
# --------------------------------------------------------------------------


def run_job(job, *, today=None):
    """Run one claimed job to a terminal state; returns the job.

    A failure is recorded on the job rather than raised, so one malformed lead
    costs one row instead of the cron's whole batch.
    """
    today = today or datetime.date.today()
    try:
        return _resolve(job, today)
    except Exception as exc:
        logger.exception("action job %s failed", job.pk)
        _fail(job, exc)
        return job


def _resolve(job, today):
    lead = _JobLead(job.lead, job.events.all())
    rules = list(rules_for_lead(job.lead))
    unevaluable = []

    matched = [
        rule
        for rule in rules
        if rule.kind == OutreachRule.KIND_DETERMINISTIC and _holds(rule, lead, today, unevaluable)
    ]

    score = rules_services.select_action(matched)
    if score is not None:
        _finish(
            job,
            ActionJob.STATUS_PROCESSING,
            ActionJob.STATUS_DETERMINISTIC_ACTION_CHOSEN,
            score,
            _decision(job, rules, matched, unevaluable),
        )
        return job

    # Nothing deterministic argued hard enough: the remainder goes to the model.
    candidates = [
        rule
        for rule in rules
        if rule.kind == OutreachRule.KIND_INFERENCE
        and (not rule.conditions or _holds(rule, lead, today, unevaluable))
    ]
    if not _transition(job, ActionJob.STATUS_PROCESSING, ActionJob.STATUS_INFERRING):
        return job

    section = (
        inference.not_asked(candidates, inference.DRY_RUN)
        if settings.ACTIONS_LLM_DRY_RUN
        else inference.infer(candidates, lead, today)
    )
    decision = _decision(job, rules, matched, unevaluable, section)
    # A verdict naming anything outside the candidate set is not a match.
    holding = set(section.get("matched_rule_ids") or ())
    inferred = [rule for rule in candidates if rule.pk in holding]

    # One tally over both passes: a weak deterministic rule and a weak
    # inference rule agreeing is a case neither makes alone.
    score = rules_services.select_action(matched + inferred)
    if score is not None:
        _finish(
            job,
            ActionJob.STATUS_INFERRING,
            ActionJob.STATUS_INFERRED_ACTION_CHOSEN,
            score,
            decision,
        )
    else:
        _finish(job, ActionJob.STATUS_INFERRING, ActionJob.STATUS_NO_ACTION, None, decision)
    return job


def _decision(job, rules, matched, unevaluable, section=None):
    """The job's workings: what each pass read, matched and could not judge.

    ``unevaluable_rule_ids`` is the union of both passes' -- the inference
    section's copy is lifted out rather than nested a second time, and so is
    its own rule count, which the top-level figure already covers.
    """
    unevaluable = set(unevaluable)
    decision = {
        "owner_id": job.lead.owner_id,
        "rules_evaluated": len(rules),
        "deterministic": {
            "matched_rule_ids": [rule.pk for rule in matched],
            "matched_rules": [rule.name for rule in matched],
        },
    }
    if section is not None:
        section = dict(section)
        section.pop("rules_evaluated", None)
        unevaluable |= set(section.pop("unevaluable_rule_ids", None) or ())
        decision["inference"] = section
    decision["unevaluable_rule_ids"] = sorted(unevaluable)
    return decision


def _holds(rule, lead, today, unevaluable):
    """Whether the rule's conditions hold. A payload this engine cannot
    evaluate never fires and is recorded on the job instead of firing or
    passing silently."""
    try:
        return evaluate.matches(rule.conditions, lead, today)
    except evaluate.ConditionError:
        logger.warning("rule %s carries conditions this engine cannot evaluate", rule.pk)
        unevaluable.append(rule.pk)
        return False


def _finish(job, expected, new_status, score, decision):
    if score is not None:
        decision["selected"] = {
            "action_key": score.action.key,
            "weight": score.weight,
            "reasons": score.reasons,
        }
    return _transition(
        job,
        expected,
        new_status,
        selected_action=score.action if score is not None else None,
        decision=decision,
        finished_at=timezone.now(),
    )


def _fail(job, exc):
    if ActionJob.STATUS_FAILED not in ActionJob.ALLOWED_TRANSITIONS.get(job.status, ()):
        return False
    return _transition(
        job,
        job.status,
        ActionJob.STATUS_FAILED,
        error=outreach._redact_and_bound(exc),
        finished_at=timezone.now(),
    )


# --------------------------------------------------------------------------
# the cron's tick
# --------------------------------------------------------------------------


def run_queue(limit=DEFAULT_BATCH_SIZE, *, today=None):
    """Claim and run up to ``limit`` queued jobs, oldest first."""
    queued = (
        ActionJob.objects.filter(status=ActionJob.STATUS_QUEUED)
        .select_related("lead")
        .prefetch_related("events")
        .order_by("created_at", "id")[:limit]
    )
    return [run_job(job, today=today) for job in queued if claim(job)]
