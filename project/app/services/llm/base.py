"""Provider-agnostic LLM client interface.

Every provider adapter (Claude, ChatGPT, DeepSeek, Groq, ...) implements
``generate`` so the rest of the app can produce text without knowing which
provider is configured. The active provider is chosen in ``config.toml`` and
resolved by :func:`project.app.services.llm.get_llm_client`.

Adapters used to return a bare ``str``, which threw away everything the provider
says *alongside* the text — how many tokens it charged us for, which model it
actually served, why it stopped generating, how long it took. The copy eval was
reduced to estimating tokens as ``len(text) // 4``, and there was nothing for a
telemetry span to record. :class:`LLMResult` is that discarded context, kept.

``complete()`` remains the text-only convenience wrapper, unchanged in name,
signature and return type. It is the seam the whole test suite mocks, and it is
still the right call for the majority of callers that only want the string.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

# Normalized finish reasons. Providers each have their own vocabulary
# ("end_turn" vs "stop", "max_tokens" vs "length") — fine for a human reading
# one provider's logs, useless for a metric aggregated across two.
FINISH_STOP = "stop"
FINISH_LENGTH = "length"
FINISH_CONTENT_FILTER = "content_filter"
FINISH_TOOL_CALLS = "tool_calls"
# Reserved for callers that build an LLMResult describing a call that failed.
# No adapter produces it from a successful response.
FINISH_ERROR = "error"

# Provider vocabulary -> ours. Anthropic ``stop_reason`` values first, then the
# OpenAI-compatible ``finish_reason`` values.
_FINISH_REASONS = {
    "end_turn": FINISH_STOP,
    "stop_sequence": FINISH_STOP,
    "max_tokens": FINISH_LENGTH,
    "tool_use": FINISH_TOOL_CALLS,
    "refusal": FINISH_CONTENT_FILTER,
    "stop": FINISH_STOP,
    "length": FINISH_LENGTH,
    "content_filter": FINISH_CONTENT_FILTER,
    "tool_calls": FINISH_TOOL_CALLS,
    "function_call": FINISH_TOOL_CALLS,
}


def normalize_finish_reason(raw: object) -> str | None:
    """Map a provider's stop/finish reason onto our small shared vocabulary.

    An unrecognized value maps to ``None``, not to :data:`FINISH_ERROR`: a
    provider adding a new legitimate reason (Anthropic added ``pause_turn``)
    must not start reporting healthy generations as errors on a dashboard. The
    provider's own string is always preserved in
    :attr:`LLMResult.raw_finish_reason`, so nothing is lost by declining to
    guess.
    """
    if not isinstance(raw, str):
        return None
    return _FINISH_REASONS.get(raw)


def coerce_token_count(value: object) -> int | None:
    """Return ``value`` as a token count, or ``None`` if it isn't one.

    Deliberately ``None`` rather than ``0`` for a missing or unusable count.
    ``0`` is a *measurement* ("this call consumed no tokens") and would poison a
    token-usage histogram; ``None`` is the absence of one. Providers are
    inconsistent about whether ``usage`` appears at all, so the distinction is
    load-bearing rather than pedantic.

    ``bool`` is excluded explicitly — it is an ``int`` subclass in Python, and a
    stray ``True`` silently becoming ``1`` token is the kind of thing nobody
    ever finds.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def coerce_text(value: object) -> str | None:
    """Return ``value`` if it is a non-empty string, else ``None``.

    Guards the fields read straight off a provider payload (``model``,
    ``stop_reason``) so a missing key, a JSON ``null``, or a test double can
    never put a non-string into a typed field.
    """
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True, slots=True)
class LLMResult:
    """One completed provider call: the text plus everything it arrived with.

    Frozen because it records something that already happened — nothing
    downstream has any business editing a provider's reported token count.

    ``model`` is what we asked for; ``response_model`` is what the provider says
    it served. They differ more often than you would hope: aliases resolve to
    dated snapshots, and providers substitute a fallback under load. Keeping
    both is what lets a copy-quality regression be traced to a model swap
    instead of blamed on the prompt.

    ``finish_reason`` is normalized across providers (see
    :func:`normalize_finish_reason`); ``raw_finish_reason`` is the provider's
    own string, untouched, because the normalization is lossy by design.

    ``latency_s`` times the provider call **only**. Retry backoff is excluded on
    purpose: folding it in would make a throttled free tier look like a slow
    model, which is the opposite of the conclusion the number should support.
    """

    text: str
    provider: str
    model: str
    response_model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = None
    raw_finish_reason: str | None = None
    latency_s: float = 0.0


class LLMClient(ABC):
    """Base class for a single LLM provider.

    ``model`` and ``default_max_tokens`` come from the provider's section of
    ``config.toml`` (see :mod:`project.app.services.llm.config`).
    """

    # Our config.toml name for the provider ("groq", "claude", ...). Subclasses
    # must set it; it rides on both LLMResult.provider and LLMError.provider, so
    # a success and a failure are joinable back to the same [llm.<name>] block.
    provider_name: str

    def __init__(self, model, default_max_tokens=500):
        self.model = model
        self.default_max_tokens = default_max_tokens

    @abstractmethod
    def generate(self, prompt, max_tokens=None) -> LLMResult:
        """Call the provider once and return the full :class:`LLMResult`.

        ``max_tokens`` falls back to ``default_max_tokens`` when not supplied.
        Failures are raised as one of the typed errors in
        :mod:`project.app.services.llm.errors`.
        """
        raise NotImplementedError

    async def agenerate(self, prompt, max_tokens=None) -> LLMResult:
        """Async counterpart of :meth:`generate`.

        Declared on the interface from the start so the concurrent planner has a
        stable signature to be written against, but left unimplemented here
        rather than defaulting to ``asyncio.to_thread(self.generate, ...)``: a
        thread-pool impersonation of async would silently cap concurrency at the
        pool size while looking like it worked. Adapters override this with a
        genuinely async client (``AsyncAnthropic``, ``httpx.AsyncClient``).
        """
        raise NotImplementedError(
            f"{type(self).__name__} has no native async implementation; call generate() instead."
        )

    def complete(self, prompt, max_tokens=None) -> str:
        """Return just the model's text completion for a single user ``prompt``.

        The original interface, preserved exactly — sync, same name, plain
        ``str``. Most callers want the string and nothing else, and this is the
        seam the test suite mocks.
        """
        return self.generate(prompt, max_tokens=max_tokens).text

    async def acomplete(self, prompt, max_tokens=None) -> str:
        """Async counterpart of :meth:`complete`."""
        result = await self.agenerate(prompt, max_tokens=max_tokens)
        return result.text
