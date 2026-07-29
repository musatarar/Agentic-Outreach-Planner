"""Anthropic (Claude) adapter.

Wraps the official ``anthropic`` SDK. The API key is read from the environment
by the SDK (``ANTHROPIC_API_KEY``), exactly as the original ``generate_copy``
implementation did.

Every SDK exception is translated into the shared taxonomy in
:mod:`project.app.services.llm.errors` before it leaves this module, so callers
never have to know which vendor SDK produced a failure in order to decide
whether it is worth retrying.
"""

import anthropic

from .base import LLMClient
from .errors import LLMMalformedResponseError, map_anthropic_error

DEFAULT_MODEL = "claude-sonnet-4-6"


class ClaudeClient(LLMClient):
    # Our config.toml name for this provider (not the vendor's). Carried on
    # LLMError.provider so a failure can be joined back to [llm.<name>].
    provider_name = "claude"

    def __init__(self, model=DEFAULT_MODEL, default_max_tokens=500):
        super().__init__(model=model, default_max_tokens=default_max_tokens)

    def complete(self, prompt, max_tokens=None):
        client = anthropic.Anthropic()  # API key comes from the environment
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=max_tokens or self.default_max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.AnthropicError as exc:
            raise map_anthropic_error(exc, self.provider_name) from exc

        # Join only the text blocks (skip thinking/other block types).
        parts = []
        for block in response.content:
            if getattr(block, "type", "") == "text":
                parts.append(block.text)
        text = "".join(parts).strip()
        if not text:
            # A 200 carrying no text block is a contract violation, not a blip.
            # Returning "" instead would land a blank draft in the review queue
            # labelled "shape check failed", pointing the reviewer at the wrong
            # problem entirely.
            raise LLMMalformedResponseError(
                "Claude returned a response with no text content.",
                provider=self.provider_name,
            )
        return text
