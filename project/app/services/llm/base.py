"""Provider-agnostic LLM client interface.

Every provider adapter (Claude, ChatGPT, DeepSeek, Groq, ...) implements
``complete`` so the rest of the app can generate text without knowing which
provider is configured. The active provider is chosen in ``config.toml`` and
resolved by :func:`project.app.services.llm.get_llm_client`.
"""

from abc import ABC, abstractmethod


class LLMClient(ABC):
    """Base class for a single LLM provider.

    ``model`` and ``default_max_tokens`` come from the provider's section of
    ``config.toml`` (see :mod:`project.app.services.llm.config`).
    """

    def __init__(self, model, default_max_tokens=500):
        self.model = model
        self.default_max_tokens = default_max_tokens

    @abstractmethod
    def complete(self, prompt, max_tokens=None):
        """Return the model's text completion for a single user ``prompt``.

        ``max_tokens`` falls back to ``default_max_tokens`` when not supplied.
        """
        raise NotImplementedError
