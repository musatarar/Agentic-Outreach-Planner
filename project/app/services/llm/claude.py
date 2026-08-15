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
from collections.abc import Mapping, Sequence

import anthropic
import httpx

from .base import (
    FINISH_TOOL_CALLS,
    LLMClient,
    LLMResult,
    LoopBoundAsyncClient,
    coerce_text,
    coerce_token_count,
    normalize_finish_reason,
    read_field,
    read_sequence,
)
from .chat_types import Message, ToolCallRequest, ToolSpec
from .errors import (
    LLMAuthError,
    LLMEmptyCompletionError,
    LLMMalformedResponseError,
    map_anthropic_error,
    map_httpx_error,
)

DEFAULT_MODEL = "claude-sonnet-4-6"

# Per-HTTP-attempt timeout. Passed explicitly rather than left to the SDK
# default so both providers time out on the same schedule; MUS-26 feeds it from
# Django settings and only has to change the caller, not this module.
DEFAULT_TIMEOUT_SECONDS = 60.0


def _open_tool_result_blocks(wire: list[dict[str, object]]) -> list[dict[str, object]] | None:
    """The block list of a trailing tool-result user message, if that is what
    the wire conversation currently ends with — else ``None``.

    "Currently ends with" is the whole point: only results that are *adjacent*
    merge, so a tool result arriving after some other turn opens a new message
    rather than reaching back into an earlier one.
    """
    if not wire:
        return None
    last = wire[-1]
    content = last.get("content")
    if last.get("role") != "user" or not isinstance(content, list):
        return None
    if not all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return None
    return content


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

    def _chat_request_kwargs(self, messages, tools, max_tokens, timeout):
        """Translate provider-neutral chat shapes into Anthropic's wire format.

        An assistant turn becomes content blocks (text first, then its
        ``tool_use`` requests); a ``tool_result`` turn is, per Anthropic's
        convention, a *user* message carrying a ``tool_result`` block that
        echoes the originating call's id.

        Consecutive ``tool_result`` messages fold into ONE user message holding
        every block, which is the shape Anthropic's tool-use contract specifies
        for a turn that requested several tools at once. One message per result
        never 400s — the API merges consecutive user turns — so nothing here
        fails loudly; the docs simply warn that splitting them degrades
        parallel tool use, which is why this is checked by a test rather than
        by the provider (MUS-66).
        """
        wire: list[dict[str, object]] = []
        for m in messages:
            if m.role == "assistant":
                blocks = [{"type": "text", "text": m.content}] if m.content else []
                blocks += [
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": dict(c.arguments)}
                    for c in m.tool_calls
                ]
                wire.append({"role": "assistant", "content": blocks})
            elif m.role == "tool_result":
                block = {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id,
                    "content": m.content,
                }
                open_results = _open_tool_result_blocks(wire)
                if open_results is None:
                    wire.append({"role": "user", "content": [block]})
                else:
                    open_results.append(block)
            else:
                wire.append({"role": "user", "content": m.content})
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens or self.default_max_tokens,
            "messages": wire,
        }
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": dict(t.parameters)}
                for t in tools
            ]
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

    async def agenerate_chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResult:
        # Mirrors agenerate: same credential check, same error mapping, same
        # loop-bound async client. Async-only by design — see the base class.
        client = self._async_client.get()
        self._check_credentials(client)

        started = time.perf_counter()
        try:
            response = await client.messages.create(
                **self._chat_request_kwargs(messages, tools, max_tokens, timeout)
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
        # Join the text blocks; collect the tool_use blocks (skip
        # thinking/other block types). A tool_use block is either read or
        # raised on: dropping one silently (which this did until MUS-66) leaves
        # the loop executing fewer calls than the model asked for, and when the
        # last one drops it hands the turn's narration back as if it were an
        # answer. An absent ``input`` is the zero-argument call every one of
        # this app's agent tools is, and reads as ``{}``; anything else
        # non-Mapping is unreadable and must not become a half-built request.
        parts = []
        tool_calls = []
        for block in read_sequence(response, "content"):
            block_type = read_field(block, "type")
            if block_type == "text":
                parts.append(coerce_text(read_field(block, "text")) or "")
            elif block_type == "tool_use":
                call_id = coerce_text(read_field(block, "id"))
                name = coerce_text(read_field(block, "name"))
                arguments = read_field(block, "input")
                if not (call_id and name):
                    raise LLMMalformedResponseError(
                        "Claude returned a tool_use block with no id or no name.",
                        provider=self.provider_name,
                    )
                if arguments is None:
                    arguments = {}
                if not isinstance(arguments, Mapping):
                    raise LLMMalformedResponseError(
                        "Claude returned a tool_use block whose input is not an object.",
                        provider=self.provider_name,
                    )
                tool_calls.append(ToolCallRequest(id=call_id, name=name, arguments=dict(arguments)))
        text = "".join(parts).strip()
        raw_finish_reason = coerce_text(read_field(response, "stop_reason"))

        if not text and not tool_calls:
            # A 200 carrying neither a text block nor a tool call is a
            # degenerate sample. Returning "" instead would land a blank draft
            # in the review queue labelled "shape check failed", pointing the
            # reviewer at the wrong problem entirely.
            raise LLMEmptyCompletionError(
                "Claude returned a response with no text content and no tool calls.",
                provider=self.provider_name,
            )
        # `stop_reason: tool_use` with nothing readable in the tool_use blocks:
        # the text is the model's reasoning on its way to a call it never
        # managed to make, and finalizing it would publish that reasoning as
        # the outreach email. Mirrors the same guard in openai_compatible.
        if normalize_finish_reason(raw_finish_reason) == FINISH_TOOL_CALLS and not tool_calls:
            raise LLMEmptyCompletionError(
                "Claude signalled a tool call but sent none we could read.",
                provider=self.provider_name,
            )

        usage = read_field(response, "usage")
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
            tool_calls=tuple(tool_calls),
        )
