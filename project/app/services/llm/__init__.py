"""Provider-agnostic LLM layer.

The active provider/model/key are resolved from the database (see
:mod:`project.app.services.llm.config`). Call :func:`get_llm_client` to
obtain the configured adapter; all adapters expose the same
:meth:`~project.app.services.llm.base.LLMClient.complete` interface.

To add a provider: implement an :class:`LLMClient` subclass and register it in
``_REGISTRY`` below. Adapters implement :meth:`LLMClient.generate`, which
returns an :class:`LLMResult` (text plus usage, model and finish reason);
``complete()`` is the text-only wrapper most callers want.

Every adapter raises only the typed errors in
:mod:`project.app.services.llm.errors`, re-exported here so callers import the
whole LLM contract — client factory and failure taxonomy — from one place.
"""

from functools import lru_cache

from . import config
from .base import (
    FINISH_CONTENT_FILTER,
    FINISH_LENGTH,
    FINISH_STOP,
    FINISH_TOOL_CALLS,
    LLMClient,
    LLMResult,
    normalize_finish_reason,
)
from .chatgpt import ChatGPTClient
from .claude import ClaudeClient
from .deepseek import DeepSeekClient
from .errors import (
    LLMAuthError,
    LLMBadRequestError,
    LLMError,
    LLMMalformedResponseError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMTransientError,
    LLMUnexpectedError,
    wrap_unexpected,
)
from .groq import GroqClient
from .stub import StubClient

_REGISTRY = {
    "claude": ClaudeClient,
    "chatgpt": ChatGPTClient,
    "deepseek": DeepSeekClient,
    "groq": GroqClient,
    # Benchmarking only, and unreachable from the app -- see stub.py's module
    # docstring for the three independent reasons why. Registered here rather
    # than constructed directly by the benchmark so it goes through the same
    # factory as every real adapter and cannot drift away from their interface.
    # The only consumer of this entry is `build_client("stub")`.
    #
    # Safe to sit in the same dict as the real four because the one place that
    # reads this registry by name (`views.py`, the "test connection" endpoint)
    # keys it on an `LLMProvider` *database row*, and `seed_llm_catalog` never
    # creates one for "stub".
    "stub": StubClient,
}


@lru_cache(maxsize=None)
def _build_client(provider, model, max_tokens, api_key):
    """Construct and cache a client for this exact (provider, model,
    max_tokens, api_key) tuple.

    Keying the cache on the resolved api_key (not just ``provider``) is what
    fixes the stale-client bug: saving a new configuration (different model,
    max_tokens, or key) changes the cache key, so the next call builds a
    fresh client instead of silently reusing one built under the old config.
    A bare provider-string key would keep serving the old client forever.
    """
    try:
        client_cls = _REGISTRY[provider]
    except KeyError:
        raise ValueError(
            f"Unknown LLM provider '{provider}'. Valid options: {', '.join(sorted(_REGISTRY))}."
        )

    kwargs = {"api_key": api_key}
    if model is not None:
        kwargs["model"] = model
    if max_tokens is not None:
        kwargs["default_max_tokens"] = max_tokens
    return client_cls(**kwargs)


def _resolve_build_args(provider):
    provider_cfg = config.get_provider_config(provider)
    model = provider_cfg.get("model")
    max_tokens = provider_cfg.get("max_tokens")
    api_key, _key_source = config.resolve_active_key(provider)
    return provider, model, max_tokens, api_key


def get_llm_client():
    """Return the LLM client for the currently active provider."""
    return _build_client(*_resolve_build_args(config.get_provider()))


def build_client(provider):
    """Return the LLM client for an explicitly named provider.

    Like :func:`get_llm_client`, but selects ``provider`` instead of the
    active one -- used by the copy eval harness to score a chosen provider
    without changing the saved active configuration. An unknown name raises
    ``ValueError``.
    """
    return _build_client(*_resolve_build_args(provider))


__all__ = [
    "LLMClient",
    "LLMResult",
    "normalize_finish_reason",
    "FINISH_STOP",
    "FINISH_LENGTH",
    "FINISH_CONTENT_FILTER",
    "FINISH_TOOL_CALLS",
    "get_llm_client",
    "build_client",
    "LLMError",
    "LLMRateLimitError",
    "LLMTimeoutError",
    "LLMTransientError",
    "LLMAuthError",
    "LLMBadRequestError",
    "LLMMalformedResponseError",
    "LLMUnexpectedError",
    "wrap_unexpected",
]
