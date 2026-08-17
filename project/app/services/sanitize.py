"""Isolation + neutralization of untrusted third-party CRM free-text (MUS-23).

Defense against OWASP LLM01 (indirect prompt injection): length cap,
neutralization of instruction-shaped patterns, and delimiter stripping, then
:func:`wrap_untrusted` fences the text in a labeled block ("spotlighting").
Heuristic defense-in-depth — a phrasing that slips past the regexes is still
confined to the data block and still grounded by verify.py on the way out
(see SECURITY.md). Pure stdlib, no Django import.
"""

import re

# Per-field character budget for untrusted text: generous for a real note, small
# enough that maxed-out fields cannot bury the trusted instructions.
MAX_NOTE_CHARS = 1200

# Delimiters for the untrusted data block. '<'/'>' are stripped from the
# untrusted text itself, so a note cannot forge these and break out.
UNTRUSTED_OPEN = "<<UNTRUSTED_CRM_DATA>>"
UNTRUSTED_CLOSE = "<<END_UNTRUSTED_CRM_DATA>>"

_REDACTION = "[redacted: instruction-like text removed from untrusted CRM note]"
_TRUNCATION = " …[truncated]"

# Instruction-shaped patterns we neutralize. Each is anchored on an
# injection-specific trigger and consumes at most the rest of its clause.
# Over-redaction inside untrusted data is acceptable; under-redaction is the risk.
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "ignore / disregard / override the previous instructions / prompt / rules"
    re.compile(
        r"\b(?:ignore|disregard|forget|discard|override|skip|bypass)\b"
        r"[^.!?\n]{0,40}?\b(?:instruction|instructions|prompt|prompts|context|"
        r"directions?|rules?|messages?|guidelines?|commands?|orders?)\b",
        re.IGNORECASE,
    ),
    # "disregard the above / everything above / all prior ..."
    re.compile(
        r"\b(?:ignore|disregard|forget|discard|override)\b\s+(?:all\s+|everything\s+)?"
        r"(?:the\s+|any\s+)?(?:above|below|preceding|previous|prior|earlier|"
        r"foregoing|aforementioned)\b",
        re.IGNORECASE,
    ),
    # role reassignment
    re.compile(r"\byou\s+are\s+now\b[^.!?\n]{0,80}", re.IGNORECASE),
    re.compile(
        r"\byou(?:'re|\s+are)\s+(?:a|an|the)\s+(?:new\s+)?(?:ai|assistant|model|bot|"
        r"chatbot|language\s+model|system|persona)\b[^.!?\n]{0,80}",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bfrom\s+now\s+on,?\s+(?:you|please|ignore|respond|reply|act|do|say|write|"
        r"always|never)\b[^.!?\n]{0,80}",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:act|behave)\s+as\s+(?:a|an|the|if|though)\b[^.!?\n]{0,60}", re.IGNORECASE),
    re.compile(r"\bpretend\s+(?:to\s+be|to|that|you)\b[^.!?\n]{0,60}", re.IGNORECASE),
    re.compile(r"\brole[-\s]?play(?:\s+as)?\b[^.!?\n]{0,60}", re.IGNORECASE),
    # "your new instructions / task / system prompt are ..."
    re.compile(
        r"\b(?:your\s+)?new\s+(?:instruction|instructions|task|tasks|role|job|"
        r"objective|goal|directive|directives|system\s+prompt|persona|rules?)\b"
        r"[^.!?\n]{0,60}",
        re.IGNORECASE,
    ),
    re.compile(r"\bsystem\s+prompt\b[^.!?\n]{0,60}", re.IGNORECASE),
    # fake conversation turns at the start of a line ("System:", "Assistant:")
    re.compile(r"(?im)^[^\S\n]*(?:system|assistant|user|ai|human|developer|model)\s*:"),
    # chat template / special tokens (run before '<'/'>' are stripped)
    re.compile(r"<\|[^|>\n]{0,40}\|>"),
    re.compile(r"\[/?INST\]", re.IGNORECASE),
    re.compile(r"\[/?SYS\]", re.IGNORECASE),
)


def sanitize_untrusted(text: str) -> str:
    """Return ``text`` capped, neutralized, and delimiter-safe for prompt use.

    Clean business prose passes through unchanged (bar whitespace trimming).
    """
    if not text:
        return ""
    text = str(text)

    # 1. Length cap first, so the regex work below is bounded.
    if len(text) > MAX_NOTE_CHARS:
        text = text[:MAX_NOTE_CHARS] + _TRUNCATION

    # 2. Neutralize before stripping '<'/'>' so special-token patterns still match.
    for pattern in _INJECTION_PATTERNS:
        text = pattern.sub(_REDACTION, text)

    # 3. Delimiter defense: strip characters that could forge our block markers.
    text = text.replace("`", "'").replace("<", "").replace(">", "")

    return text.strip()


def wrap_untrusted(text: str) -> str:
    """Fence sanitized untrusted text in an explicit labeled block.

    The caller precedes this block with a standing everything-inside-is-data
    instruction ("spotlighting").
    """
    return f"{UNTRUSTED_OPEN}\n{text}\n{UNTRUSTED_CLOSE}"


def sanitize_and_wrap(text: str) -> str:
    """Convenience: :func:`sanitize_untrusted` then :func:`wrap_untrusted`."""
    return wrap_untrusted(sanitize_untrusted(text))
