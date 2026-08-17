"""Anthropic (Claude) adapter.

SDK exceptions are translated into the taxonomy in :mod:`.errors` before they
leave this module. The async client is built with ``max_retries=0`` because
``llm/retry.py`` owns that path's retry budget; the sync client keeps the SDK's
default retries because nothing else wraps that path (the asymmetry goes away
when MUS-26 moves the planner async). Both clients get an explicit ``timeout``
— the SDK default is 600s — and a per-call ``timeout=`` rides on the request.
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

# Per-HTTP-attempt timeout, explicit so both providers time out on the same
# schedule.
DEFAULT_TIMEOUT_SECONDS = 60.0


def _open_tool_result_blocks(wire: list[dict[str, object]]) -> list[dict[str, object]] | None:
    """The block list of a trailing tool-result user message, if that is what
    the wire conversation currently ends with — else ``None``. Only *adjacent*
    results merge; a result after some other turn opens a new message.
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
        # ``api_key or None`` hands an unset key to the SDK's own
        # ANTHROPIC_API_KEY lookup; "" would read as a real (invalid) credential.
        self._client = anthropic.Anthropic(api_key=api_key or None, timeout=self.timeout_s)
        self._async_client = LoopBoundAsyncClient(
            # max_retries=0: llm/retry.py owns the budget on this path.
            factory=lambda: anthropic.AsyncAnthropic(
                api_key=self.api_key or None, max_retries=0, timeout=self.timeout_s
            ),
            closer=lambda client: client.close(),
        )

    # -- request construction (shared by both paths) ------------------------

    def _envelope(self, wire_messages, max_tokens, timeout):
        """The model/max_tokens/messages/timeout envelope both builders share."""
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens or self.default_max_tokens,
            "messages": wire_messages,
        }
        # Only when asked for: an unconditional None would override the
        # client-level timeout and restore the SDK's 600s default.
        if timeout is not None:
            kwargs["timeout"] = timeout
        return kwargs

    def _request_kwargs(self, prompt, max_tokens, timeout):
        return self._envelope([{"role": "user", "content": prompt}], max_tokens, timeout)

    def _chat_request_kwargs(self, messages, tools, max_tokens, timeout):
        """Translate provider-neutral chat shapes into Anthropic's wire format.

        Assistant turns become content blocks; a ``tool_result`` turn is a
        *user* message carrying a ``tool_result`` block. Consecutive tool
        results fold into ONE user message — Anthropic's parallel-tool-use
        shape; splitting them degrades silently, so a test checks it (MUS-66).
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
        kwargs = self._envelope(wire, max_tokens, timeout)
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": dict(t.parameters)}
                for t in tools
            ]
        return kwargs

    def _check_credentials(self, client):
        """Fail typed when the SDK could not resolve a credential.

        anthropic==0.109.1 raises a bare ``TypeError`` at send time for a
        missing key, which would escape the taxonomy untyped. Asked of the
        client, not ``os.environ``, so the SDK's own resolution order applies;
        all three mechanisms count (``credentials`` covers the header-injecting
        providers that leave ``api_key``/``auth_token`` as ``None``).
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
        # The SDK builds on httpx; a raw httpx error escaping it is cheap to
        # insure against.
        return map_httpx_error(exc, self.provider_name).with_latency(elapsed)

    # -- the two call paths -------------------------------------------------

    def generate(self, prompt, max_tokens=None, timeout=None) -> LLMResult:
        # Sync client keeps the SDK's default retries -- see module docstring.
        client = self._client
        self._check_credentials(client)

        # Times the provider call only -- see LLMResult.latency_s.
        started = time.perf_counter()
        try:
            response = client.messages.create(**self._request_kwargs(prompt, max_tokens, timeout))
        except (anthropic.AnthropicError, httpx.HTTPError) as exc:
            raise self._mapped(exc, started) from exc
        latency_s = time.perf_counter() - started

        return self._build_result(response, latency_s)

    async def _acall(self, kwargs) -> LLMResult:
        """Send one already-built request on the loop-bound async client;
        credential check, latency clock and error mapping live here once
        (MUS-66)."""
        client = self._async_client.get()
        self._check_credentials(client)

        started = time.perf_counter()
        try:
            response = await client.messages.create(**kwargs)
        except (anthropic.AnthropicError, httpx.HTTPError) as exc:
            raise self._mapped(exc, started) from exc
        latency_s = time.perf_counter() - started

        return self._build_result(response, latency_s)

    async def agenerate(self, prompt, max_tokens=None, timeout=None) -> LLMResult:
        return await self._acall(self._request_kwargs(prompt, max_tokens, timeout))

    async def agenerate_chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResult:
        # Async-only by design — see the base class.
        return await self._acall(self._chat_request_kwargs(messages, tools, max_tokens, timeout))

    async def aclose(self) -> None:
        await self._async_client.aclose()

    def _build_result(self, response, latency_s) -> LLMResult:
        """Turn a Messages response into an :class:`LLMResult`.

        Fields are read via :func:`~.base.read_field` so dict and pydantic
        payloads read identically; a missing ``usage`` count comes back
        ``None``, never ``0``.
        """
        # Join text blocks; collect tool_use blocks (skip other types). A
        # tool_use block is either read or raised on — dropping one silently
        # under-executes the model's calls (a bug until MUS-66). An absent
        # ``input`` is a zero-argument call and reads as {}; a non-Mapping one
        # is unreadable.
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
            # A 200 with neither text nor tool calls is a degenerate sample;
            # returning "" would land a blank draft in the review queue.
            raise LLMEmptyCompletionError(
                "Claude returned a response with no text content and no tool calls.",
                provider=self.provider_name,
            )
        # `stop_reason: tool_use` with no readable tool_use blocks: the text is
        # reasoning, not an answer. Mirrors the same guard in openai_compatible.
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
            # Anthropic EXCLUDES cache reads/writes from input_tokens, so the
            # three counts are not redundant.
            input_tokens=coerce_token_count(read_field(usage, "input_tokens")),
            output_tokens=coerce_token_count(read_field(usage, "output_tokens")),
            cache_read_tokens=coerce_token_count(read_field(usage, "cache_read_input_tokens")),
            cache_write_tokens=coerce_token_count(read_field(usage, "cache_creation_input_tokens")),
            finish_reason=normalize_finish_reason(raw_finish_reason),
            raw_finish_reason=raw_finish_reason,
            latency_s=latency_s,
            tool_calls=tuple(tool_calls),
        )
