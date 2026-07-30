"""Shared adapter for any OpenAI Chat Completions-compatible provider.

OpenAI (ChatGPT), DeepSeek, and Groq all expose the same
``POST {base_url}/chat/completions`` wire format, so one ``httpx``-based client
covers all three. Subclasses set ``base_url``, ``api_key_env``, ``provider_name``
and a default ``model``. The API key is read from the environment (never from
config.toml).

Failures — transport, HTTP status, and unreadable response bodies alike — are
translated into the shared taxonomy in :mod:`project.app.services.llm.errors`
before they leave this module.

Both a sync and a native async path are provided. The async one holds a real
``httpx.AsyncClient`` (connection reuse across a concurrent run is most of the
point), cached per event loop — see
:class:`~project.app.services.llm.base.LoopBoundAsyncClient` for why that
qualifier is load-bearing.
"""

import json
import os
import time

import httpx

from .base import (
    LLMClient,
    LLMResult,
    LoopBoundAsyncClient,
    coerce_text,
    coerce_token_count,
    normalize_finish_reason,
    read_field,
    read_sequence,
)
from .errors import LLMAuthError, LLMMalformedResponseError, map_httpx_error

# Per-HTTP-attempt timeout. A constructor argument rather than a module
# constant so MUS-26 only has to wire config.toml into the caller; the value is
# unchanged from the constant it replaces.
DEFAULT_TIMEOUT_SECONDS = 60.0

# Ways the documented response shape can betray us, all of which mean the same
# thing to a caller: the provider answered with something we can't read.
#   KeyError       -> a promised key is absent
#   IndexError     -> "choices" came back empty
#   TypeError      -> a key held null or the wrong type (e.g. "content": null)
#   AttributeError -> "content" was JSON but not a string, so .strip() is absent
_SHAPE_ERRORS = (KeyError, IndexError, TypeError, AttributeError)

# What a chat-completions request can legitimately fail with. Narrow on purpose:
# transport failures, HTTP status failures, a malformed base_url and a non-JSON
# body. Anything else is our bug and should keep its own traceback.
# httpx.InvalidURL is listed separately because -- unlike every other httpx
# failure -- it derives from Exception, not from httpx.HTTPError, so a bad
# base_url would otherwise escape the LLM layer untyped.
_REQUEST_ERRORS = (httpx.HTTPError, httpx.InvalidURL, json.JSONDecodeError)


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

    def __init__(self, model, default_max_tokens=500, timeout_s=DEFAULT_TIMEOUT_SECONDS):
        super().__init__(model=model, default_max_tokens=default_max_tokens)
        self.timeout_s = timeout_s
        self._async_client = LoopBoundAsyncClient(
            factory=lambda: httpx.AsyncClient(timeout=self.timeout_s),
            closer=lambda client: client.aclose(),
        )

    # -- request construction (shared by both paths) ------------------------

    def _api_key(self):
        api_key = os.environ.get(self.api_key_env)
        if api_key:
            return api_key
        # LLMAuthError subclasses RuntimeError, so this raise keeps exactly the
        # exception contract callers had before the taxonomy existed — it just
        # additionally says "don't bother retrying me".
        raise LLMAuthError(
            f"{self.provider_label} provider selected but {self.api_key_env} "
            f"is not set. Add it to your .env file.",
            provider=self.provider_name,
        )

    def _request(self, prompt, max_tokens):
        return f"{self.base_url}/chat/completions", {
            "headers": {
                "Authorization": f"Bearer {self._api_key()}",
                "Content-Type": "application/json",
            },
            "json": {
                "model": self.model,
                "max_tokens": max_tokens or self.default_max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
        }

    # -- the two call paths -------------------------------------------------

    def generate(self, prompt, max_tokens=None) -> LLMResult:
        url, kwargs = self._request(prompt, max_tokens)
        # Times the provider call and nothing else. httpx.post is
        # non-streaming, so the body is fully read by the time it returns --
        # which means the clock has to stop HERE, before raise_for_status() and
        # .json(), or Claude and this adapter would be reporting different
        # things under the same field name. See LLMResult.latency_s.
        started = time.perf_counter()
        try:
            response = httpx.post(url, timeout=self.timeout_s, **kwargs)
            latency_s = time.perf_counter() - started
            response.raise_for_status()
            data = response.json()
        except _REQUEST_ERRORS as exc:
            raise map_httpx_error(exc, self.provider_name).with_latency(
                time.perf_counter() - started
            ) from exc

        return self._build_result(data, latency_s)

    async def agenerate(self, prompt, max_tokens=None) -> LLMResult:
        url, kwargs = self._request(prompt, max_tokens)
        # The timeout lives on the AsyncClient, so it is not repeated here.
        client = self._async_client.get()
        started = time.perf_counter()
        try:
            response = await client.post(url, **kwargs)
            latency_s = time.perf_counter() - started
            response.raise_for_status()
            data = response.json()
        except _REQUEST_ERRORS as exc:
            raise map_httpx_error(exc, self.provider_name).with_latency(
                time.perf_counter() - started
            ) from exc

        return self._build_result(data, latency_s)

    async def aclose(self) -> None:
        await self._async_client.aclose()

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
        prompt_details = _mapping_or_empty(usage.get("prompt_tokens_details"))
        first_choice = _first_choice(data)
        raw_finish_reason = coerce_text(read_field(first_choice, "finish_reason"))
        return LLMResult(
            text=text,
            provider=self.provider_name,
            model=self.model,
            response_model=coerce_text(data.get("model")),
            input_tokens=coerce_token_count(usage.get("prompt_tokens")),
            output_tokens=coerce_token_count(usage.get("completion_tokens")),
            # Unlike Anthropic, OpenAI-compatible providers report cached tokens
            # as a breakdown WITHIN prompt_tokens rather than alongside it, and
            # have no notion of a cache write. Groq omits the block entirely.
            cache_read_tokens=coerce_token_count(prompt_details.get("cached_tokens")),
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
    choices = read_sequence(data, "choices")
    if choices:
        return _mapping_or_empty(choices[0])
    return {}
