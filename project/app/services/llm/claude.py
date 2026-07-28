"""Anthropic (Claude) adapter.

Wraps the official ``anthropic`` SDK. The SDK client is built once, in
``__init__`` -- not per call -- using the explicit ``api_key`` passed in by
the factory (:mod:`project.app.services.llm`); when ``api_key`` is ``None``
the SDK falls back to its own ``ANTHROPIC_API_KEY`` env var lookup.
"""

import anthropic

from .base import LLMClient

DEFAULT_MODEL = "claude-sonnet-4-6"


class ClaudeClient(LLMClient):
    def __init__(self, model=DEFAULT_MODEL, default_max_tokens=500, api_key=None):
        super().__init__(model=model, default_max_tokens=default_max_tokens, api_key=api_key)
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def complete(self, prompt, max_tokens=None, timeout=None):
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens or self.default_max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if timeout is not None:
            kwargs["timeout"] = timeout
        response = self._client.messages.create(**kwargs)
        # Join only the text blocks (skip thinking/other block types).
        parts = []
        for block in response.content:
            if getattr(block, "type", "") == "text":
                parts.append(block.text)
        return "".join(parts).strip()
