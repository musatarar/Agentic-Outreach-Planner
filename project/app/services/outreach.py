"""Outreach planning logic for Eventual's Agentic Outreach Planner.

Pure business logic: `determine_priority` and `determine_action` work via
duck typing on any object exposing the Lead attributes from CONTRACT.md
(plus `lead.events.all()` or a plain list of event-like objects), so they
can be unit-tested without Django or a database. Django models are only
imported inside `plan_outreach()`.
"""

import datetime
import re

from project.app.services import actions
from project.app.services.llm import get_llm_client

MAX_COPY_TOKENS = 500

# Phrases (lowercase) suggesting the lead asked to be contacted later /
# was waiting on something — i.e. a "hold".
HOLD_PHRASES = [
    "waiting on",
    "waiting for",
    "budget approval",
    "budget",
    "follow up in",
    "get back",
    "circle back",
    "touch base in",
    "next quarter",
]

# Phrases (lowercase) suggesting the lead has gone quiet on us.
STALL_PHRASES = [
    "haven't heard back",
    "havent heard back",
    "haven't heard",
    "no response",
    "no reply",
    "went quiet",
    "gone quiet",
]

DORMANT_DAYS = 21          # no login for this long => dormant
QUIET_CONTACT_DAYS = 14    # gone-quiet only counts if last contact >= this old
STALE_CONTACT_DAYS = 21    # contact older than this is overdue
TRIAL_AT_RISK_DAYS = 30    # signed up this long with zero deals => at risk
POWER_USER_DEALS = 5       # deals closed to count as a power user
POWER_USER_SUBMISSIONS = 10  # quote submissions to count as a power user


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _events_list(lead):
    """Return lead events as a list, accepting a manager (.all()) or a list."""
    events = getattr(lead, "events", None)
    if events is None:
        return []
    if hasattr(events, "all"):
        return list(events.all())
    return list(events)


def _as_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    return value


def _days_since(value, today):
    value = _as_date(value)
    if value is None:
        return None
    return (today - value).days


def _notes_blob(lead):
    """Combined lowercase text of hubspot notes + event notes/outcomes."""
    parts = [getattr(lead, "hubspot_notes", "") or ""]
    for event in _events_list(lead):
        meta = getattr(event, "meta", None) or {}
        for key in ("notes", "subject", "outcome"):
            if meta.get(key):
                parts.append(str(meta[key]))
    return " ".join(parts).lower()


def _contains_any(text, phrases):
    return any(p in text for p in phrases)


def _matched_phrase(text, phrases):
    for p in phrases:
        if p in text:
            return p
    return None


def _sentence_containing(text, phrase):
    """Return the sentence of `text` containing `phrase` (case-insensitive)."""
    if not text:
        return ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if phrase in sentence.lower():
            return sentence.strip()
    return ""


def _milestone_from_notes(lead):
    """Pull a numeric deal milestone out of the hubspot notes (e.g. '20 closed
    deals', 'close 5 deals'). Returns int or None."""
    notes = getattr(lead, "hubspot_notes", "") or ""
    match = re.search(r"(\d+)\s+(?:closed\s+)?deals?", notes, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def _had_no_reply_email(lead):
    for event in _events_list(lead):
        if getattr(event, "type", "") == "email_sent":
            meta = getattr(event, "meta", None) or {}
            if meta.get("outcome") == "no_reply":
                return True
    return False


def _gone_quiet(lead, today):
    """True when we reached out, enough time passed, and the lead went quiet."""
    days_contact = _days_since(getattr(lead, "last_contacted_date", None), today)
    if days_contact is None or days_contact < QUIET_CONTACT_DAYS:
        return False
    blob = _notes_blob(lead)
    return _contains_any(blob, STALL_PHRASES) or _had_no_reply_email(lead)


# --------------------------------------------------------------------------
# priority
# --------------------------------------------------------------------------

def determine_priority(lead, today=None):
    """Score a lead and map to priority 1 (highest) .. 3 (lowest).

    Signals: estimated book size, stage (demo completed but never onboarded),
    gone-quiet/no-reply patterns, contact staleness, trial-at-risk, and hot
    revenue engagement.
    """
    today = today or datetime.date.today()
    score = 0

    # Book size: bigger books are worth more attention.
    book = getattr(lead, "estimated_book_size_usd", 0) or 0
    if book >= 5_000_000:
        score += 2
    elif book >= 2_000_000:
        score += 1

    # Demo completed but never signed up: high-value conversion opportunity.
    if getattr(lead, "stage", "") == "demo_completed" and not getattr(lead, "signed_up_date", None):
        score += 2

    # We reached out, time passed, and they went quiet (stall notes / no-reply).
    if _gone_quiet(lead, today):
        score += 2

    # Contact is overdue regardless of why.
    days_contact = _days_since(getattr(lead, "last_contacted_date", None), today)
    if days_contact is None or days_contact > STALE_CONTACT_DAYS:
        score += 1

    # Trial at risk: signed up a while ago, zero deals closed.
    days_signed = _days_since(getattr(lead, "signed_up_date", None), today)
    if days_signed is not None and days_signed > TRIAL_AT_RISK_DAYS and (getattr(lead, "deals_closed", 0) or 0) == 0:
        score += 1

    # Hot revenue engagement: heavy submitters/closers deserve attention too.
    if (getattr(lead, "deals_closed", 0) or 0) >= POWER_USER_DEALS or \
            (getattr(lead, "quotes_submitted", 0) or 0) >= POWER_USER_SUBMISSIONS:
        score += 1

    if score >= 5:
        return 1
    if score >= 2:
        return 2
    return 3


# --------------------------------------------------------------------------
# action classification
# --------------------------------------------------------------------------

def determine_action(lead, today=None):
    """Classify the right outreach action for a lead.

    Returns (action_type, reason). The reason references concrete signals
    (counts, dates, note content) so an AE can trust the recommendation.
    """
    today = today or datetime.date.today()
    name = getattr(lead, "contact_name", "this lead")
    notes = getattr(lead, "hubspot_notes", "") or ""
    blob = _notes_blob(lead)
    deals = getattr(lead, "deals_closed", 0) or 0
    created = getattr(lead, "quotes_created", 0) or 0
    submitted = getattr(lead, "quotes_submitted", 0) or 0
    book = getattr(lead, "estimated_book_size_usd", 0) or 0
    last_login = _as_date(getattr(lead, "last_login_date", None))
    days_login = _days_since(last_login, today)
    days_contact = _days_since(getattr(lead, "last_contacted_date", None), today)
    milestone = _milestone_from_notes(lead)

    # 1. Demo completed but never signed up -> complete onboarding.
    if getattr(lead, "stage", "") == "demo_completed" and not getattr(lead, "signed_up_date", None):
        reason = (
            f"{name} completed a demo but never signed up, and the agency's "
            f"estimated book is ${book:,.0f}."
        )
        stall = _matched_phrase(blob, STALL_PHRASES)
        promise = _sentence_containing(notes, "follow up") or _sentence_containing(notes, "get back")
        if promise:
            reason += f" Notes say: \"{promise}\""
        if days_contact is not None:
            reason += f" Last contact was {days_contact} days ago"
            reason += " with no reply since." if (stall or _had_no_reply_email(lead)) else "."
        return actions.COMPLETE_ONBOARDING, reason

    # 2. Power user near a reward/volume-pricing milestone.
    if deals >= POWER_USER_DEALS and submitted >= POWER_USER_SUBMISSIONS:
        reason = (
            f"{name} is a power user: {created} quotes created, {submitted} "
            f"submitted, {deals} deals closed, last login {last_login}."
        )
        if milestone:
            remaining = max(milestone - deals, 0)
            reason += (
                f" HubSpot notes flag a volume-pricing conversation at the "
                f"{milestone}-deal milestone — only {remaining} deals away."
            )
        snippet = _sentence_containing(notes, "volume pricing")
        if snippet:
            reason += f" Notes: \"{snippet}\""
        return actions.POWER_USER_REWARD, reason

    # 3. On hold ("contact me later" / waiting on budget) and the hold passed.
    hold_phrase = _matched_phrase(blob, HOLD_PHRASES)
    if hold_phrase and _gone_quiet(lead, today):
        reason = f"{name} put us on hold and the hold reason has now passed."
        snippet = _sentence_containing(notes, hold_phrase)
        if not snippet:
            for event in _events_list(lead):
                meta = getattr(event, "meta", None) or {}
                snippet = _sentence_containing(str(meta.get("notes", "")), hold_phrase)
                if snippet:
                    break
        if snippet:
            reason += f" Notes: \"{snippet}\""
        if days_contact is not None:
            reason += f" Last contacted {days_contact} days ago"
            reason += " and a follow-up email got no reply." if _had_no_reply_email(lead) else "."
        if days_login is not None:
            reason += f" Last portal login was {days_login} days ago ({last_login})."
        return actions.FOLLOW_UP_AFTER_HOLD, reason

    # 4. Onboarded but stopped using the portal entirely.
    if getattr(lead, "signed_up_date", None) and (days_login is None or days_login > DORMANT_DAYS):
        signed = _as_date(getattr(lead, "signed_up_date", None))
        if days_login is None:
            reason = f"{name} signed up on {signed} but has never logged in to the portal."
        else:
            reason = (
                f"{name} signed up on {signed} but hasn't logged in for "
                f"{days_login} days (last login {last_login}) — the trial has gone dormant."
            )
        return actions.REENGAGE_DORMANT, reason

    # 5. Active but underusing -> nudge.
    if days_login is not None and days_login <= DORMANT_DAYS:
        if created > 0 and submitted == 0:
            reason = (
                f"{name} logs in regularly (last login {last_login}) and has "
                f"created {created} quotes but has never submitted one — needs "
                f"help getting a first quote over the line."
            )
            return actions.NUDGE_USAGE, reason
        if milestone and 0 < deals < milestone:
            remaining = milestone - deals
            reason = (
                f"{name} is using the portal steadily ({created} quotes created, "
                f"{deals} deals closed, last login {last_login}) but is {remaining} "
                f"deals short of the {milestone}-deal commitment target in the "
                f"notes — a well-timed push could convert the trial."
            )
            return actions.NUDGE_USAGE, reason
        if 0 < deals < POWER_USER_DEALS:
            reason = (
                f"{name} is active (last login {last_login}) with {deals} deals "
                f"closed but momentum is modest — encourage more volume."
            )
            return actions.NUDGE_USAGE, reason

    # 6. Nothing matched -> escalate to a human.
    reason = (
        f"No outreach pattern matched for {name}: stage={getattr(lead, 'stage', '?')}, "
        f"quotes_created={created}, quotes_submitted={submitted}, deals_closed={deals}, "
        f"last_login={last_login}, last_contacted={_as_date(getattr(lead, 'last_contacted_date', None))}. "
        f"BD should review the HubSpot notes and decide the next step manually."
    )
    return actions.UNKNOWN, reason


# --------------------------------------------------------------------------
# copy generation (provider-agnostic; see project.app.services.llm)
# --------------------------------------------------------------------------

def _format_events_for_prompt(lead, limit=6):
    events = _events_list(lead)
    events = sorted(events, key=lambda e: getattr(e, "timestamp"), reverse=True)
    lines = []
    for event in events[:limit]:
        ts = getattr(event, "timestamp")
        ts_str = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)
        meta = getattr(event, "meta", None) or {}
        line = f"- {ts_str} {getattr(event, 'type', 'event')}"
        if meta.get("notes"):
            line += f": {meta['notes']}"
        elif meta.get("subject"):
            line += f": \"{meta['subject']}\" (outcome: {meta.get('outcome', 'unknown')})"
        elif meta.get("client"):
            line += f" — client {meta['client']}, premium ${meta.get('premium', '?')}"
        lines.append(line)
    return "\n".join(lines) if lines else "(no recorded events)"


def _build_copy_prompt(lead, action_type, reason):
    meta = actions.ACTION_META.get(action_type, {})
    return f"""You are an account executive at Eventual. Eventual sells Premium Lock — insurance premium protection for homeowners — through independent insurance agencies. Write a short, personalized outreach email to the agency contact below.

Lead:
- Contact: {getattr(lead, 'contact_name', '')} ({getattr(lead, 'contact_email', '')})
- Agency: {getattr(lead, 'agency_name', '')} ({getattr(lead, 'state', '')}, {getattr(lead, 'num_producers', '?')} producers, {getattr(lead, 'years_in_business', '?')} years in business)
- Stage: {getattr(lead, 'stage', '')}
- Estimated book size: ${getattr(lead, 'estimated_book_size_usd', 0) or 0:,.0f}
- Signed up: {getattr(lead, 'signed_up_date', None)} | Last login: {getattr(lead, 'last_login_date', None)} | Last contacted: {getattr(lead, 'last_contacted_date', None)}
- Usage: {getattr(lead, 'quotes_created', 0)} quotes created, {getattr(lead, 'quotes_submitted', 0)} submitted, {getattr(lead, 'deals_closed', 0)} deals closed

HubSpot notes:
{getattr(lead, 'hubspot_notes', '') or '(none)'}

Recent activity and call/email/demo notes (most recent first):
{_format_events_for_prompt(lead)}

Planned action: {action_type} ({meta.get('label', action_type)}, urgency: {meta.get('urgency', 'medium')})
Why now: {reason}

Write the email now. Requirements:
- Include a Subject line, then the body (about 120 words).
- Warm, specific, and personal — reference the concrete details above (their numbers, their words, their clients) rather than generic praise.
- Voice of an Eventual AE: helpful peer, not salesy.
- Exactly one clear call to action that matches the planned action.
- Output only the email (subject + body), no commentary."""


def generate_copy(lead, action_type, reason):
    """Generate a personalized outreach email via the configured LLM provider.

    The provider (Claude, ChatGPT, DeepSeek, Groq, ...) is selected in
    ``config.toml``; see :mod:`project.app.services.llm`. Returns the text.
    """
    prompt = _build_copy_prompt(lead, action_type, reason)
    return get_llm_client().complete(prompt, max_tokens=MAX_COPY_TOKENS)


# --------------------------------------------------------------------------
# planner
# --------------------------------------------------------------------------

def plan_outreach():
    """Plan outreach for every lead: decide priority + action, generate copy,
    persist OutreachAction rows, and return them sorted by priority."""
    # Imported here so this module stays importable without Django configured.
    from project.app.models import Lead, OutreachAction

    planned = []
    for lead in Lead.objects.all():
        priority = determine_priority(lead)
        action_type, reason = determine_action(lead)
        needs_human = action_type == actions.UNKNOWN
        suggested_copy = ""
        further_action = ""

        if needs_human:
            further_action = (
                f"BD review needed for {lead.contact_name} ({lead.agency_name}): "
                f"no automated outreach pattern matched. Review HubSpot notes and "
                f"recent activity, then decide whether to contact, hold, or disqualify."
            )
        else:
            try:
                suggested_copy = generate_copy(lead, action_type, reason)
            except Exception as exc:  # don't let one API failure sink the run
                needs_human = True
                further_action = (
                    f"Copy generation failed ({exc}). AE should draft the "
                    f"{action_type} email manually using the reason above."
                )

        action = OutreachAction.objects.create(
            lead=lead,
            priority=priority,
            action_type=action_type,
            reason=reason,
            suggested_copy=suggested_copy,
            needs_human=needs_human,
            further_action=further_action,
        )
        planned.append(action)

    planned.sort(key=lambda a: a.priority)
    return planned
