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
import time

import httpx

from .base import (
    LLMClient,
    LLMResult,
    coerce_text,
    coerce_token_count,
    normalize_finish_reason,
)
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
    # Our config.toml name for this provider ("groq", "chatgpt", ...), carried
    # on LLMError.provider so a failure joins back to [llm.<name>]. Annotated
    # rather than defaulted, like base_url above: a subclass that forgets it
    # should fail loudly instead of tagging its errors with a provider name
    # that matches no config section.
    provider_name: str
    provider_label = "OpenAI-compatible"

    def generate(self, prompt, max_tokens=None) -> LLMResult:
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

        # Times the provider call and nothing else -- not our response parsing,
        # and (once the retry helper wraps this) not any backoff between
        # attempts. See LLMResult.latency_s.
        started = time.perf_counter()
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
        except (httpx.HTTPError, httpx.InvalidURL, json.JSONDecodeError) as exc:
            # Narrow on purpose: transport failures, HTTP status failures, a
            # malformed base_url and a non-JSON body are what this call can
            # legitimately do. Anything else is our bug and keeps its traceback.
            # InvalidURL is listed separately because — unlike every other httpx
            # failure — it derives from Exception, not from httpx.HTTPError, so
            # a bad base_url would otherwise escape the LLM layer untyped.
            raise map_httpx_error(exc, self.provider_name) from exc
        latency_s = time.perf_counter() - started

        return self._build_result(data, latency_s)

    def _build_result(self, data, latency_s) -> LLMResult:
        """Turn a chat-completions body into an :class:`LLMResult`.

        The text is the one field we insist on: every way its subscript chain
        can break — missing key, empty ``choices``, a null ``content``,
        whitespace-only text — arrives at the caller as a single
        ``LLMMalformedResponseError`` rather than as a ``KeyError`` leaking out
        of the LLM layer with no provider attached.

        Everything else is best-effort. ``usage`` and ``model`` are not part of
        the contract we depend on: Groq sends them, and the spec permits their
        absence, so a provider that omits them yields ``None`` and a healthy
        result — not an error, and emphatically not a zero.
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

        usage = _mapping_or_empty(data.get("usage"))
        first_choice = _first_choice(data)
        raw_finish_reason = coerce_text(first_choice.get("finish_reason"))
        return LLMResult(
            text=text,
            provider=self.provider_name,
            model=self.model,
            response_model=coerce_text(data.get("model")),
            input_tokens=coerce_token_count(usage.get("prompt_tokens")),
            output_tokens=coerce_token_count(usage.get("completion_tokens")),
            finish_reason=normalize_finish_reason(raw_finish_reason),
            raw_finish_reason=raw_finish_reason,
            latency_s=latency_s,
        )


def _mapping_or_empty(value):
    """``value`` when it is a dict, else ``{}`` -- so a provider sending
    ``"usage": null`` costs us a ``None`` field, not an exception."""
    return value if isinstance(value, dict) else {}


def _first_choice(data):
    """The first ``choices`` entry as a dict, or ``{}``.

    Reached only after the text extraction above succeeded, so ``choices[0]``
    exists; the guard is for the metadata *around* it being a shape we didn't
    expect.
    """
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        return _mapping_or_empty(choices[0])
    return {}
