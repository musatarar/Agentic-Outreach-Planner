"""Provider-agnostic LLM layer.

Call :func:`get_llm_client` for the configured adapter. Adapters raise only the
typed errors in :mod:`.errors`, re-exported here; new providers register in
``_REGISTRY``.
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
from .deepseek import DeepSeekClient
from .errors import (
    LLMAuthError,
    LLMBadRequestError,
    LLMEmptyCompletionError,
    LLMError,
    LLMMalformedResponseError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMTransientError,
    LLMUnexpectedError,
    wrap_unexpected,
)
from .groq import GroqClient
from .structured import StructuredResult, response_format_for
from .stub import StubClient

_REGISTRY = {
    "chatgpt": ChatGPTClient,
    "deepseek": DeepSeekClient,
    "groq": GroqClient,
    # Benchmarking only; its constructor refuses without the explicit opt-in
    # (see stub.py), so naming it as LLM_PROVIDER buys nothing. Registered so
    # `build_client("stub")` goes through the same factory as real adapters.
    "stub": StubClient,
}


@lru_cache(maxsize=None)
def _build_client(provider, model, api_key):
    """Construct and cache a client for this exact (provider, model, api_key)
    tuple — keyed on the full tuple so an environment change builds a fresh
    client instead of reusing a stale one.
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
    return client_cls(**kwargs)


def _resolve_build_args(provider):
    model = config.get_provider_config(provider).get("model")
    return provider, model, config.resolve_api_key(provider)


def get_llm_client():
    """Return the LLM client for the currently active provider."""
    return _build_client(*_resolve_build_args(config.get_provider()))


def build_client(provider):
    """Return the LLM client for an explicitly named provider (used by the
    copy eval harness). An unknown name raises ``ValueError``.
    """
    return _build_client(*_resolve_build_args(provider))


__all__ = [
    "LLMClient",
    "LLMResult",
    "StructuredResult",
    "response_format_for",
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
    "LLMEmptyCompletionError",
    "LLMUnexpectedError",
    "wrap_unexpected",
]
