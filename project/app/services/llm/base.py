"""Provider-agnostic LLM client interface.

Every provider adapter (Claude, ChatGPT, DeepSeek, Groq, ...) implements
``complete`` so the rest of the app can generate text without knowing which
provider is configured. The active provider/model/key are resolved from the
database by :func:`project.app.services.llm.get_llm_client` (see
:mod:`project.app.services.llm.config`).
"""

from abc import ABC, abstractmethod


class LLMClient(ABC):
    """Base class for a single LLM provider.

    ``model`` and ``default_max_tokens`` come from the resolved configuration
    (see :mod:`project.app.services.llm.config`). ``api_key`` is passed
    explicitly by the factory that builds this client; a subclass falls back
    to reading its own env var only when ``api_key`` is ``None``.
    """

    def __init__(self, model, default_max_tokens=500, api_key=None):
        self.model = model
        self.default_max_tokens = default_max_tokens
        self.api_key = api_key

    @abstractmethod
    def complete(self, prompt, max_tokens=None, timeout=None):
        """Return the model's text completion for a single user ``prompt``.

        ``max_tokens`` falls back to ``default_max_tokens`` when not supplied.
        ``timeout`` (seconds) overrides the provider's default request timeout
        for this single call when supplied -- used by the config "test
        connection" endpoint to fail fast instead of hanging.
        """
        raise NotImplementedError
