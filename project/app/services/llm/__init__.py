"""Provider-agnostic LLM layer.

The active provider is chosen in ``config.toml``. Call :func:`get_llm_client`
to obtain the configured adapter; all adapters expose the same
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
from .base import LLMClient, LLMResult
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
)
from .groq import GroqClient

_REGISTRY = {
    "claude": ClaudeClient,
    "chatgpt": ChatGPTClient,
    "deepseek": DeepSeekClient,
    "groq": GroqClient,
}


@lru_cache(maxsize=None)
def _build_client(provider):
    try:
        client_cls = _REGISTRY[provider]
    except KeyError:
        raise ValueError(
            f"Unknown LLM provider '{provider}'. Valid options: {', '.join(sorted(_REGISTRY))}."
        )

    provider_cfg = config.get_provider_config(provider)
    kwargs = {}
    if "model" in provider_cfg:
        kwargs["model"] = provider_cfg["model"]
    if "max_tokens" in provider_cfg:
        kwargs["default_max_tokens"] = provider_cfg["max_tokens"]
    return client_cls(**kwargs)


def get_llm_client():
    """Return the LLM client for the provider named in ``config.toml``."""
    return _build_client(config.get_provider())


def build_client(provider):
    """Return the LLM client for an explicitly named configured provider.

    Like :func:`get_llm_client`, but selects ``provider`` (which must have a
    ``[llm.<provider>]`` section in ``config.toml``) instead of the active one.
    Used by the copy eval harness to score a chosen provider without mutating
    ``config.toml``; an unknown name raises ``ValueError``.
    """
    return _build_client(provider)


__all__ = [
    "LLMClient",
    "LLMResult",
    "get_llm_client",
    "build_client",
    "LLMError",
    "LLMRateLimitError",
    "LLMTimeoutError",
    "LLMTransientError",
    "LLMAuthError",
    "LLMBadRequestError",
    "LLMMalformedResponseError",
]
