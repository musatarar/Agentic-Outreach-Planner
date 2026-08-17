"""Provider-agnostic LLM client interface.

Adapters implement ``generate``/``agenerate`` returning :class:`LLMResult`
(text plus usage, model and finish reason); ``complete()`` is the text-only
wrapper most callers — and the test suite's mocks — use.
"""

import asyncio
import threading
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from .chat_types import Message, ToolCallRequest, ToolSpec

# The async client an adapter caches: httpx.AsyncClient or AsyncAnthropic.
ClientT = TypeVar("ClientT")

# Normalized finish reasons, shared across provider vocabularies.
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
# Anthropic's "pause_turn" and DeepSeek's "insufficient_system_resource" must
# stay absent: unknown reasons map to None, never to an error bucket.


def normalize_finish_reason(raw: object) -> str | None:
    """Map a provider's stop/finish reason onto our small shared vocabulary.

    Unknown values map to ``None``, not an error bucket — a failed call raises
    instead — and the raw string survives in :attr:`LLMResult.raw_finish_reason`.
    """
    if not isinstance(raw, str):
        return None
    return _FINISH_REASONS.get(raw)


def coerce_token_count(value: object) -> int | None:
    """Return ``value`` as a token count, or ``None`` if it isn't one.

    A missing/unusable count is ``None``, never ``0`` (``0`` is a measurement);
    ``bool`` is excluded because it is an ``int`` subclass.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def read_field(container: object, name: str) -> object:
    """Read ``name`` off a provider payload, whether it is an object (SDK
    model) or a dict (plain JSON / ``model_dump()``)."""
    if isinstance(container, Mapping):
        return container.get(name)
    return getattr(container, name, None)


def read_sequence(container: object, name: str) -> list[object]:
    """Read a list-valued field off a payload, or ``[]`` if it isn't one.
    Accepts tuples too — SDK models and test doubles hand them back."""
    value = read_field(container, name)
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def coerce_text(value: object) -> str | None:
    """Return ``value`` if it is a non-empty string, else ``None``."""
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True, slots=True)
class LLMResult:
    """One completed provider call: the text plus everything it arrived with.

    ``model`` is what we asked for; ``response_model`` is what the provider
    says it served — they can differ. ``finish_reason`` is normalized (see
    :func:`normalize_finish_reason`); ``raw_finish_reason`` is the provider's
    own string. ``latency_s`` times the provider call only — retry backoff
    excluded — and defaults to ``None``, never ``0.0``. Cache fields follow
    each provider's own accounting: Anthropic counts cache reads/writes
    *alongside* ``input_tokens``, OpenAI-compatible providers *within*
    ``prompt_tokens``. ``tool_calls`` (MUS-29) is appended last with a default
    so existing construction stays valid.
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
    tool_calls: tuple[ToolCallRequest, ...] = ()


class LLMClient(ABC):
    """Base class for a single LLM provider.

    ``model`` and ``default_max_tokens`` come from the resolved configuration
    (see :mod:`project.app.services.llm.config`). ``api_key`` is passed
    explicitly by the factory that builds this client; a subclass falls back
    to reading its own env var only when ``api_key`` is ``None``.
    """

    # Configured provider name ("groq", "claude", ...); rides on both
    # LLMResult.provider and LLMError.provider. Subclasses must set it.
    provider_name: str

    def __init__(self, model, default_max_tokens=500, api_key=None):
        self.model = model
        self.default_max_tokens = default_max_tokens
        self.api_key = api_key

    @abstractmethod
    def generate(self, prompt, max_tokens=None, timeout=None) -> LLMResult:
        """Call the provider once and return the full :class:`LLMResult`.

        ``max_tokens`` falls back to ``default_max_tokens``; ``timeout``
        (seconds) overrides the per-attempt request timeout for this call.
        Failures raise the typed errors in :mod:`.errors`.
        """
        raise NotImplementedError

    async def agenerate(self, prompt, max_tokens=None, timeout=None) -> LLMResult:
        """Async counterpart of :meth:`generate`.

        No thread-pool fallback by design — that would silently cap concurrency
        at the pool size. Adapters override with a genuinely async client.
        """
        raise NotImplementedError(
            f"{type(self).__name__} has no native async implementation; call generate() instead."
        )

    async def agenerate_chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResult:
        """Async multi-turn chat with optional tool offers (MUS-29).

        The agent loop's seam; the result may carry :attr:`LLMResult.tool_calls`
        instead of — or alongside — text. Async-only by design: the sync Claude
        client keeps SDK-internal retries, so a sync chat path would
        double-retry underneath the loop's own retry policy.
        """
        raise NotImplementedError(f"{type(self).__name__} has no chat/tool-calling implementation.")

    def complete(self, prompt, max_tokens=None, timeout=None) -> str:
        """Return just the model's text completion for a single user ``prompt``.

        The seam the test suite mocks; ``timeout`` is forwarded to
        :meth:`generate`.
        """
        return self.generate(prompt, max_tokens=max_tokens, timeout=timeout).text

    async def acomplete(self, prompt, max_tokens=None, timeout=None) -> str:
        """Async counterpart of :meth:`complete`."""
        result = await self.agenerate(prompt, max_tokens=max_tokens, timeout=timeout)
        return result.text

    async def aclose(self) -> None:
        """Release any async resources held by this client (no-op unless the
        adapter caches an async HTTP client)."""
        return None


class LoopBoundAsyncClient(Generic[ClientT]):
    """Caches one async client, keyed on the event loop that created it.

    ``asyncio.run()`` builds a fresh loop each call, so a client cached across
    runs holds connections bound to a dead loop; rebuild when the loop changes.
    The lock is required: the ``lru_cache``d adapter is shared process-wide, so
    two threads can have two live loops on this object at once. A client whose
    loop is gone is dropped, not closed — closing would await a dead loop; the
    orphan's sockets are reclaimed at GC (a ``ResourceWarning`` under
    ``-W error``).
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
            # Returned as a local from inside the lock; re-reading the
            # attribute after release would reintroduce the race.
            return self._client

    async def aclose(self) -> None:
        running = asyncio.get_running_loop()
        with self._lock:
            if self._client is None or self._loop is not running:
                # Nothing to close, or the client belongs to another loop:
                # closing it here would be a cross-loop use, and clearing the
                # fields would yank a client another thread is still using.
                return
            client = self._client
            self._client = self._loop = None
        await self._closer(client)
