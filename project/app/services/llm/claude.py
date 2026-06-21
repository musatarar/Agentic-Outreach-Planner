"""Anthropic (Claude) adapter.

Wraps the official ``anthropic`` SDK. The API key is read from the environment
by the SDK (``ANTHROPIC_API_KEY``), exactly as the original ``generate_copy``
implementation did.
"""

import anthropic

from .base import LLMClient

DEFAULT_MODEL = "claude-sonnet-4-6"


class ClaudeClient(LLMClient):
    def __init__(self, model=DEFAULT_MODEL, default_max_tokens=500):
        super().__init__(model=model, default_max_tokens=default_max_tokens)

    def complete(self, prompt, max_tokens=None):
        client = anthropic.Anthropic()  # API key comes from the environment
        response = client.messages.create(
            model=self.model,
            max_tokens=max_tokens or self.default_max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        # Join only the text blocks (skip thinking/other block types).
        parts = []
        for block in response.content:
            if getattr(block, "type", "") == "text":
                parts.append(block.text)
        return "".join(parts).strip()
