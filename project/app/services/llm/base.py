"""Provider-agnostic LLM client interface.

Every provider adapter (Claude, ChatGPT, DeepSeek, Groq, ...) implements
``generate`` so the rest of the app can produce text without knowing which
provider is configured. The active provider is chosen in ``config.toml`` and
resolved by :func:`project.app.services.llm.get_llm_client`.

Adapters used to return a bare ``str``, which throws away everything the provider
says *alongside* the text — how many tokens it charged us for, which model it
actually served, why it stopped generating, how long it took. :class:`LLMResult`
is that discarded context, kept.

The copy eval reads it already. It still *estimates* tokens as ``len(text) // 4``
rather than reading the provider's counts, deliberately: the committed baselines
were recorded against the estimate, and changing a metric and its baseline
together would make a regression indistinguishable from a units change. The
planner still takes the text-only path; MUS-26 moves it over when it makes copy
generation concurrent.

``complete()`` remains the text-only convenience wrapper, unchanged in name,
signature and return type. It is the seam the whole test suite mocks, and it is
still the right call for the majority of callers that only want the string.
"""

import asyncio
import threading
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Generic, TypeVar

# The async client an adapter caches: httpx.AsyncClient or AsyncAnthropic.
ClientT = TypeVar("ClientT")

# Normalized finish reasons. Providers each have their own vocabulary
# ("end_turn" vs "stop", "max_tokens" vs "length") — fine for a human reading
# one provider's logs, useless for a metric aggregated across two.
FINISH_STOP = "stop"
FINISH_LENGTH = "length"
FINISH_CONTENT_FILTER = "content_filter"
FINISH_TOOL_CALLS = "tool_calls"

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
# Deliberately absent, and both must stay absent: Anthropic's "pause_turn" and
# DeepSeek's "insufficient_system_resource" have no honest equivalent here. See
# normalize_finish_reason for why an unknown reason is None rather than "error".


def normalize_finish_reason(raw: object) -> str | None:
    """Map a provider's stop/finish reason onto our small shared vocabulary.

    An unrecognized value maps to ``None``, not to some catch-all "error"
    bucket: a provider adding a new legitimate reason (Anthropic added
    ``pause_turn``) must not start reporting healthy generations as errors on a
    dashboard. A call that actually failed raises an ``LLMError`` and produces
    no ``LLMResult`` at all, so there is nothing for such a bucket to hold. The
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


def read_field(container: object, name: str) -> object:
    """Read ``name`` off a provider payload, whether it is an object or a dict.

    The two adapters receive the same information in different containers: the
    Anthropic SDK hands us a pydantic model, an OpenAI-compatible body is plain
    JSON. Both shapes also cross over in practice — ``with_raw_response`` and
    ``model_dump()`` turn an SDK response into dicts — and a token count going
    silently missing because the container was the other kind is the worst sort
    of loss for a billing number. One reader, both shapes, no asymmetry.
    """
    if isinstance(container, Mapping):
        return container.get(name)
    return getattr(container, name, None)


def read_sequence(container: object, name: str) -> list[object]:
    """Read a list-valued field off a payload, or ``[]`` if it isn't one.

    Companion to :func:`read_field` for the fields that hold arrays (``content``
    blocks, ``choices``). Accepts any sequence, not just ``list``: JSON only ever
    produces lists, but an SDK model or a test double may hand back a tuple, and
    quietly dropping the whole array over the container type would be a silent
    data loss for a one-character reason.
    """
    value = read_field(container, name)
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


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
    It defaults to ``None``, not ``0.0``, for the same reason token counts do —
    a caller-constructed result must not claim a real observation of zero
    seconds.

    The cache fields are reported as the provider reports them, and the two
    providers disagree: Anthropic counts cache reads and writes *alongside*
    ``input_tokens`` (so the true prompt cost is the sum), while
    OpenAI-compatible providers count cached tokens *within* ``prompt_tokens``
    and have no notion of a cache write. Nothing here caches yet — the fields
    exist because this type is what both downstream branches build on, and
    widening it after the fact would break them.

    ``slots=True`` means instances have no ``__dict__`` — use
    :func:`dataclasses.asdict` rather than ``vars()`` when fanning the fields out
    into span attributes.
    """

    text: str
    provider: str
    model: str
    response_model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    finish_reason: str | None = None
    raw_finish_reason: str | None = None
    latency_s: float | None = None


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

    async def aclose(self) -> None:
        """Release any async resources held by this client.

        A no-op unless the adapter caches an async HTTP client. Callers that own
        the event loop should await this in a ``finally``.
        """
        return None


class LoopBoundAsyncClient(Generic[ClientT]):
    """Caches one async client, keyed on the event loop that created it.

    `asyncio.run()` builds a *fresh* event loop and closes it on the way out.
    An `httpx.AsyncClient` or `AsyncAnthropic` created inside one run holds
    connections bound to that loop, so reusing it in a later run fails — and
    fails obscurely, deep inside a transport, with a message about a closed or
    different loop rather than about caching.

    That is precisely the shape of this codebase's usage: `plan_outreach()` is
    sync and will drive its concurrency through `asyncio.run()`, so calling it
    twice in one process (a test, a management command, two API requests) hits
    the stale-client case immediately. Caching *with* the loop and rebuilding
    when it changes is a small fix for a bug that is otherwise very easy to
    write and very annoying to diagnose.

    **The lock is not decoration.** ``_build_client`` is ``lru_cache``d, so one
    adapter — and therefore one of these — is shared process-wide by every
    request thread. Under a threaded server, two threads each running
    ``asyncio.run()`` are two live loops on this object at once, and an
    unlocked check-then-store can hand thread A the client thread B just built
    for B's loop. That is exactly the cross-loop handout this class exists to
    prevent, reachable by a GIL switch instead of by a stale cache.

    A client whose loop has gone is dropped rather than closed: closing it would
    mean awaiting on a dead loop. Its socket FDs are reclaimed when the orphan is
    garbage-collected — `asyncio.run()` closes the loop but does not close open
    transports, so under ``-W error`` this can surface as a ``ResourceWarning``.
    Awaiting a dead loop to avoid that would be strictly worse.
    """

    def __init__(
        self,
        factory: Callable[[], ClientT],
        closer: Callable[[ClientT], Awaitable[None]],
    ) -> None:
        self._factory = factory
        self._closer = closer
        self._client: ClientT | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def get(self) -> ClientT:
        """Return the client for the *currently running* loop, building it if
        this is a new loop. Must be called from inside a coroutine."""
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._client is None or self._loop is not loop:
                self._client = self._factory()
                self._loop = loop
            # Returned from inside the lock, and as a local: re-reading the
            # attribute after releasing would reintroduce the race.
            return self._client

    async def aclose(self) -> None:
        running = asyncio.get_running_loop()
        with self._lock:
            if self._client is None or self._loop is not running:
                # Either nothing to close, or the cached client belongs to some
                # other loop. Awaiting its close from here would be the very
                # cross-loop use this class exists to prevent -- and clearing
                # the fields would be worse than doing nothing, because under
                # the two-live-loops case this class explicitly designs for it
                # would yank a client another thread is still using.
                return
            client = self._client
            self._client = self._loop = None
        await self._closer(client)
