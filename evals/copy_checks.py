"""Deterministic, LLM-free checks on a generated outreach email (MUS-21).

Pure Python (only ``re``) so both the Django test suite and the Inspect scorer
can use them. Intentionally coarse: gross violations only; finer quality
judgements are the LLM judge's job.
"""

import re

# "about 120 words" -> a wide band on purpose; nitpicking length is the judge's job.
WORD_MIN = 60
WORD_MAX = 200

# Tolerates markdown decoration around the label ("**Subject:**", "## Subject:",
# "**Subject**:"): models emit it, and a decorated subject is still a subject.
# Trailing emphasis is stripped from the captured text ("**Subject: X**" -> "X").
_SUBJECT_RE = re.compile(r"^[\s#*_]*subject\s*[*_]*\s*:\s*[*_]*\s*(.*?)[\s*_]*$", re.IGNORECASE)

# Preamble the model was told not to emit; a clean email opens with the Subject line.
_PREAMBLE_OPENERS = (
    "here is",
    "here's",
    "here are",
    "sure,",
    "sure!",
    "sure.",
    "certainly",
    "of course",
    "absolutely",
    "below is",
    "below you",
    "i've written",
    "i have written",
    "i've drafted",
    "i have drafted",
    "i've put together",
    "as requested",
    "the following",
    "please find",
    "```",
)

# CTA-shaped phrases; a sentence ending in "?" also counts. Soft pitch phrases
# ("happy to", "I'd love to") are deliberately excluded -- intent, not an ask.
_CTA_RE = re.compile(
    r"\b("
    r"let me know|let's|reply|respond|reach out|get in touch|touch base|"
    r"schedule|book a|set up a|hop on|grab (?:15|a few|some|30|20|time)|"
    r"can we|could we|would you|are you (?:free|open|available|around)|"
    r"worth a (?:quick )?(?:call|chat|look|conversation)|take a look|"
    r"check out|sign up|get started|give me a (?:call|shout|ring)|"
    r"call me|jump on a|find (?:15 minutes|a )?time|when works|"
    r"does (?:that|next week|this week) work"
    r")\b",
    re.IGNORECASE,
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def check_subject_line(email):
    """Return ``(passed, subject_text)`` — is there a ``Subject:`` line?"""
    for line in email.splitlines():
        m = _SUBJECT_RE.match(line)
        if m:
            return True, m.group(1).strip()
    return False, ""


def _body_after_subject(email):
    """The email text with the Subject line removed (whole email if none)."""
    lines = email.splitlines()
    for i, line in enumerate(lines):
        if _SUBJECT_RE.match(line):
            return "\n".join(lines[i + 1 :]).strip()
    return email.strip()


def count_body_words(email):
    """Word count of the body (Subject line excluded)."""
    body = _body_after_subject(email)
    return len(body.split())


def check_word_count(email, low=WORD_MIN, high=WORD_MAX):
    """Return ``(passed, word_count)`` for the body length band."""
    count = count_body_words(email)
    return (low <= count <= high), count


def check_no_preamble(email):
    """Return ``(passed, offending_opener)`` — did the output start clean?"""
    head = email.strip().lower()
    for opener in _PREAMBLE_OPENERS:
        if head.startswith(opener):
            return False, opener
    return True, ""


def count_cta_sentences(email):
    """Count call-to-action-shaped sentences in the body (heuristic)."""
    body = _body_after_subject(email)
    count = 0
    for sentence in _SENTENCE_SPLIT_RE.split(body):
        s = sentence.strip()
        if not s:
            continue
        if s.endswith("?") or _CTA_RE.search(s):
            count += 1
    return count


def check_single_cta(email):
    """Return ``(passed, cta_count)`` — exactly one CTA-shaped sentence?"""
    count = count_cta_sentences(email)
    return count == 1, count


# Names of the four boolean checks, in report order.
CHECK_NAMES = ("subject", "word_count", "no_preamble", "single_cta")


def run_all(email):
    """Run every deterministic check on ``email``.

    Returns a flat dict: one bool per name in :data:`CHECK_NAMES`, plus
    ``word_count`` (int) and ``cta_count`` (int) detail for reporting.
    """
    subject_ok, _subject = check_subject_line(email)
    wc_ok, word_count = check_word_count(email)
    preamble_ok, _opener = check_no_preamble(email)
    cta_ok, cta_count = check_single_cta(email)
    return {
        "subject": subject_ok,
        "word_count": wc_ok,
        "no_preamble": preamble_ok,
        "single_cta": cta_ok,
        "detail_word_count": word_count,
        "detail_cta_count": cta_count,
    }
