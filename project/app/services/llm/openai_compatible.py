"""Shared adapter for any OpenAI Chat Completions-compatible provider.

OpenAI (ChatGPT), DeepSeek, and Groq all expose the same
``POST {base_url}/chat/completions`` wire format, so one ``httpx``-based client
covers all three. Subclasses set ``base_url``, ``api_key_env``, ``provider_name``
and a default ``model``. The API key is read from the environment (never from
config.toml).

Failures — transport, HTTP status, and unreadable response bodies alike — are
translated into the shared taxonomy in :mod:`project.app.services.llm.errors`
before they leave this module.
"""

import json
import os

import httpx

from .base import LLMClient
from .errors import LLMAuthError, LLMMalformedResponseError, map_httpx_error

_TIMEOUT_SECONDS = 60.0

# Ways the documented response shape can betray us, all of which mean the same
# thing to a caller: the provider answered with something we can't read.
#   KeyError       -> a promised key is absent
#   IndexError     -> "choices" came back empty
#   TypeError      -> a key held null or the wrong type (e.g. "content": null)
#   AttributeError -> "content" was JSON but not a string, so .strip() is absent
_SHAPE_ERRORS = (KeyError, IndexError, TypeError, AttributeError)


class OpenAICompatibleClient(LLMClient):
    # Subclasses must override these.
    base_url: str
    api_key_env: str
    provider_label = "OpenAI-compatible"
    # Our config.toml name for this provider ("groq", "chatgpt", ...), carried
    # on LLMError.provider so a failure joins back to [llm.<name>]. Distinct
    # from provider_label, which is the human-facing name used in the messages
    # an operator reads.
    provider_name = "openai_compatible"

    def complete(self, prompt, max_tokens=None):
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            # LLMAuthError subclasses RuntimeError, so this raise keeps exactly
            # the exception contract callers had before the taxonomy existed —
            # it just additionally says "don't bother retrying me".
            raise LLMAuthError(
                f"{self.provider_label} provider selected but {self.api_key_env} "
                f"is not set. Add it to your .env file.",
                provider=self.provider_name,
            )

        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": max_tokens or self.default_max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            # Narrow on purpose: transport failures, HTTP status failures and a
            # non-JSON body are the three things this call can legitimately do.
            # Anything else is our bug and should keep its own traceback.
            raise map_httpx_error(exc, self.provider_name) from exc

        return self._extract_text(data)

    def _extract_text(self, data):
        """Pull the completion out of a chat-completions body, or fail typed.

        The happy path is a single subscript chain. Doing it behind a method is
        worth it because every way the chain can break — missing key, empty
        ``choices``, a null ``content``, whitespace-only text — then arrives at
        the caller as one ``LLMMalformedResponseError`` rather than as a
        ``KeyError`` leaking out of the LLM layer with no provider attached.
        """
        try:
            text = data["choices"][0]["message"]["content"].strip()
        except _SHAPE_ERRORS as exc:
            raise map_httpx_error(exc, self.provider_name) from exc
        if not text:
            raise LLMMalformedResponseError(
                f"{self.provider_label} returned an empty completion.",
                provider=self.provider_name,
            )
        return text
