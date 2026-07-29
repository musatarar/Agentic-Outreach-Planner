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
import httpx

from .base import LLMClient
from .errors import LLMAuthError, LLMMalformedResponseError, map_anthropic_error, map_httpx_error

DEFAULT_MODEL = "claude-sonnet-4-6"


class ClaudeClient(LLMClient):
    # Our config.toml name for this provider (not the vendor's). Carried on
    # LLMError.provider so a failure can be joined back to [llm.<name>].
    provider_name = "claude"

    def __init__(self, model=DEFAULT_MODEL, default_max_tokens=500):
        super().__init__(model=model, default_max_tokens=default_max_tokens)

    def complete(self, prompt, max_tokens=None):
        client = anthropic.Anthropic()  # API key comes from the environment
        if not (client.api_key or client.auth_token):
            # anthropic==0.109.1 constructs a keyless client happily and only
            # fails at send time, with a bare TypeError ("Could not resolve
            # authentication method") that is NOT an AnthropicError -- so it
            # would escape the LLM layer untyped, with no .retryable for the
            # retry helper and no .provider for a span, and Claude would be the
            # one provider whose missing key isn't an LLMAuthError.
            #
            # Asked of the constructed client rather than of os.environ so the
            # question is "did the SDK resolve a credential?", which is the SDK's
            # own resolution order (env, explicit arg, ...) rather than a second
            # copy of it that can drift.
            raise LLMAuthError(
                "Claude provider selected but ANTHROPIC_API_KEY is not set. "
                "Add it to your .env file.",
                provider=self.provider_name,
            )

        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=max_tokens or self.default_max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.AnthropicError as exc:
            raise map_anthropic_error(exc, self.provider_name) from exc
        except httpx.HTTPError as exc:
            # The SDK normally wraps transport failures into APIConnectionError,
            # but it builds on httpx and a raw one escaping is cheap to insure
            # against -- and expensive to debug if it ever does.
            raise map_httpx_error(exc, self.provider_name) from exc

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
