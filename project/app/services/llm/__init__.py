"""Provider-agnostic LLM layer.

The active provider is chosen in ``config.toml``. Call :func:`get_llm_client`
to obtain the configured adapter; all adapters expose the same
:meth:`~project.app.services.llm.base.LLMClient.complete` interface.

To add a provider: implement an :class:`LLMClient` subclass and register it in
``_REGISTRY`` below.
"""

from functools import lru_cache

from . import config
from .base import LLMClient
from .chatgpt import ChatGPTClient
from .claude import ClaudeClient
from .deepseek import DeepSeekClient
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
            f"Unknown LLM provider '{provider}'. "
            f"Valid options: {', '.join(sorted(_REGISTRY))}."
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


__all__ = ["LLMClient", "get_llm_client"]
