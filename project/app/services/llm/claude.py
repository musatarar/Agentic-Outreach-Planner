"""Anthropic (Claude) adapter.

Wraps the official ``anthropic`` SDK. Both SDK clients are built once, in
``__init__`` -- not per call -- using the explicit ``api_key`` passed in by the
factory (:mod:`project.app.services.llm`), which resolves it from the database
or the environment; when ``api_key`` is falsy the SDK falls back to its own
``ANTHROPIC_API_KEY`` lookup.

Every SDK exception is translated into the shared taxonomy in
:mod:`project.app.services.llm.errors` before it leaves this module, so callers
never have to know which vendor SDK produced a failure in order to decide
whether it is worth retrying.

The **async** client is constructed with ``max_retries=0``; the sync one is not.
That asymmetry is deliberate and follows one rule: whoever owns the retry budget
owns it alone.

``llm/retry.py`` wraps the async path, so leaving the SDK's default of two
retries there would mean two *silent* HTTP attempts underneath every one of ours
-- invisible to a per-attempt span and doubling the effective budget.

Nothing wraps the sync path (``outreach.generate_copy`` and the red-team eval
call ``complete()`` bare, and the shared helper is async-only), so turning the
SDK's retries off there would delete real resilience and replace it with
nothing. It stays on until MUS-26 moves the planner onto the async path, at
which point this asymmetry disappears on its own.

Both clients get an explicit ``timeout``: the SDK default is a 600-second read
timeout, which is not a bound anyone would choose for a 500-token completion. A
caller can tighten it for one call by passing ``timeout=`` to ``generate`` /
``complete``, which rides on the request rather than the client -- that is what
the config "test connection" endpoint uses to fail fast instead of hanging.
"""

import time

import anthropic
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
from .errors import LLMAuthError, LLMMalformedResponseError, map_anthropic_error, map_httpx_error

DEFAULT_MODEL = "claude-sonnet-4-6"

# Per-HTTP-attempt timeout. Passed explicitly rather than left to the SDK
# default so both providers time out on the same schedule; MUS-26 feeds it from
# Django settings and only has to change the caller, not this module.
DEFAULT_TIMEOUT_SECONDS = 60.0


class ClaudeClient(LLMClient):
    # Our configured name for this provider (not the vendor's). Carried on
    # LLMResult.provider and LLMError.provider so both a success and a failure
    # can be joined back to the configured provider.
    provider_name = "claude"

    def __init__(
        self,
        model=DEFAULT_MODEL,
        default_max_tokens=500,
        api_key=None,
        timeout_s=DEFAULT_TIMEOUT_SECONDS,
    ):
        super().__init__(model=model, default_max_tokens=default_max_tokens, api_key=api_key)
        self.timeout_s = timeout_s
        # Built once, here, rather than per call. ``api_key or None`` is what
        # hands an unset key back to the SDK's own ANTHROPIC_API_KEY lookup --
        # an empty string would be taken as a real (invalid) credential.
        self._client = anthropic.Anthropic(api_key=api_key or None, timeout=self.timeout_s)
        self._async_client = LoopBoundAsyncClient(
            # max_retries=0: llm/retry.py owns the budget on this path.
            factory=lambda: anthropic.AsyncAnthropic(
                api_key=self.api_key or None, max_retries=0, timeout=self.timeout_s
            ),
            closer=lambda client: client.close(),
        )

    # -- request construction (shared by both paths) ------------------------

    def _request_kwargs(self, prompt, max_tokens, timeout):
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens or self.default_max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        # Per-request override only when asked for: passing it unconditionally
        # would override the client-level timeout with None on every ordinary
        # call, restoring the SDK's 600-second default by accident.
        if timeout is not None:
            kwargs["timeout"] = timeout
        return kwargs

    def _check_credentials(self, client):
        """Fail typed when the SDK could not resolve a credential.

        anthropic==0.109.1 constructs a keyless client happily and only fails at
        send time, with a bare ``TypeError`` ("Could not resolve authentication
        method") that is NOT an ``AnthropicError`` -- so it would escape the LLM
        layer untyped, with no ``.retryable`` for the retry helper and no
        ``.provider`` for a span, and Claude would be the one provider whose
        missing key isn't an ``LLMAuthError``.

        Asked of the constructed client rather than of ``os.environ`` so the
        question is "did the SDK resolve a credential?" -- the SDK's own
        resolution order (env, explicit arg, ...) rather than a second copy of
        it that can drift.

        All THREE mechanisms count, not just the two static ones. ``credentials``
        covers the profile-on-disk and workload-identity-federation providers,
        which authenticate by injecting an ``Authorization`` header per request
        and leave ``api_key`` and ``auth_token`` as ``None``. The SDK's own
        error names all three ("Expected one of api_key, auth_token, or
        credentials to be set"); checking only the first two would reject a
        deployment that works, and reject it non-retryably.
        """
        if client.api_key or client.auth_token or getattr(client, "credentials", None):
            return
        raise LLMAuthError(
            "Claude provider selected but ANTHROPIC_API_KEY is not set. Add it to your .env file.",
            provider=self.provider_name,
        )

    def _mapped(self, exc, started):
        """Translate a provider exception and stamp it with how long it took."""
        elapsed = time.perf_counter() - started
        if isinstance(exc, anthropic.AnthropicError):
            return map_anthropic_error(exc, self.provider_name).with_latency(elapsed)
        # The SDK normally wraps transport failures into APIConnectionError, but
        # it builds on httpx and a raw one escaping is cheap to insure against --
        # and expensive to debug if it ever does.
        return map_httpx_error(exc, self.provider_name).with_latency(elapsed)

    # -- the two call paths -------------------------------------------------

    def generate(self, prompt, max_tokens=None, timeout=None) -> LLMResult:
        # max_retries deliberately left at the SDK default on the sync client --
        # see the module docstring. Nothing wraps this path in retries yet.
        client = self._client
        self._check_credentials(client)

        # Times the provider call and nothing else -- not our response parsing,
        # and not any backoff the retry helper adds between attempts. See
        # LLMResult.latency_s.
        started = time.perf_counter()
        try:
            response = client.messages.create(**self._request_kwargs(prompt, max_tokens, timeout))
        except (anthropic.AnthropicError, httpx.HTTPError) as exc:
            raise self._mapped(exc, started) from exc
        latency_s = time.perf_counter() - started

        return self._build_result(response, latency_s)

    async def agenerate(self, prompt, max_tokens=None, timeout=None) -> LLMResult:
        client = self._async_client.get()
        self._check_credentials(client)

        started = time.perf_counter()
        try:
            response = await client.messages.create(
                **self._request_kwargs(prompt, max_tokens, timeout)
            )
        except (anthropic.AnthropicError, httpx.HTTPError) as exc:
            raise self._mapped(exc, started) from exc
        latency_s = time.perf_counter() - started

        return self._build_result(response, latency_s)

    async def aclose(self) -> None:
        await self._async_client.aclose()

    def _build_result(self, response, latency_s) -> LLMResult:
        """Turn a Messages response into an :class:`LLMResult`.

        Every field is read through
        :func:`~project.app.services.llm.base.read_field`, so a ``model_dump()``d
        or ``with_raw_response`` payload reads identically to a pydantic model —
        a token count going quietly missing because the container was the other
        kind is the worst sort of loss for a billing number. ``usage`` is
        genuinely optional on some surfaces, and a missing count must come back
        as ``None`` rather than ``0`` -- see
        :func:`~project.app.services.llm.base.coerce_token_count`.
        """
        # Join only the text blocks (skip thinking/other block types).
        parts = []
        for block in read_sequence(response, "content"):
            if read_field(block, "type") == "text":
                parts.append(coerce_text(read_field(block, "text")) or "")
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

        usage = read_field(response, "usage")
        raw_finish_reason = coerce_text(read_field(response, "stop_reason"))
        return LLMResult(
            text=text,
            provider=self.provider_name,
            model=self.model,
            response_model=coerce_text(read_field(response, "model")),
            # Anthropic reports cache reads/writes separately and EXCLUDES them
            # from input_tokens, so these three are not redundant -- a consumer
            # wanting the true prompt cost has to add them up itself.
            input_tokens=coerce_token_count(read_field(usage, "input_tokens")),
            output_tokens=coerce_token_count(read_field(usage, "output_tokens")),
            cache_read_tokens=coerce_token_count(read_field(usage, "cache_read_input_tokens")),
            cache_write_tokens=coerce_token_count(read_field(usage, "cache_creation_input_tokens")),
            finish_reason=normalize_finish_reason(raw_finish_reason),
            raw_finish_reason=raw_finish_reason,
            latency_s=latency_s,
        )
