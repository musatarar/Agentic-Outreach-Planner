"""Shared adapter for any OpenAI Chat Completions-compatible provider.

OpenAI, DeepSeek, and Groq share the ``POST {base_url}/chat/completions`` wire
format; subclasses set ``base_url``, ``api_key_env``, ``provider_name`` and a
default ``model``. Failures are translated into :mod:`.errors` before leaving
this module, and the async path caches an ``httpx.AsyncClient`` per event loop
(see :class:`~.base.LoopBoundAsyncClient`).
"""

import json
import os
import time
from collections.abc import Sequence

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
    map_httpx_error,
)

# Per-HTTP-attempt timeout.
DEFAULT_TIMEOUT_SECONDS = 60.0

# Ways the documented response shape can betray us — all "the provider
# answered with something we can't read":
#   KeyError       -> a promised key is absent
#   IndexError     -> "choices" came back empty
#   TypeError      -> a key held null or the wrong type (e.g. "content": null)
#   AttributeError -> "content" was JSON but not a string, so .strip() is absent
_SHAPE_ERRORS = (KeyError, IndexError, TypeError, AttributeError)

# What a request can legitimately fail with; anything else is our bug and
# keeps its own traceback. httpx.InvalidURL is named separately because it
# derives from Exception, not httpx.HTTPError, and would otherwise escape
# untyped.
_REQUEST_ERRORS = (httpx.HTTPError, httpx.InvalidURL, json.JSONDecodeError)


class OpenAICompatibleClient(LLMClient):
    # Subclasses must override these. provider_name is annotated rather than
    # defaulted so a subclass that forgets it fails loudly.
    base_url: str
    api_key_env: str
    provider_name: str
    provider_label = "OpenAI-compatible"

    def __init__(
        self,
        model,
        default_max_tokens=500,
        api_key=None,
        timeout_s=DEFAULT_TIMEOUT_SECONDS,
    ):
        super().__init__(model=model, default_max_tokens=default_max_tokens, api_key=api_key)
        self.timeout_s = timeout_s
        self._async_client = LoopBoundAsyncClient(
            # Connection limits stay at httpx's default (100), above the planned
            # 8-way semaphore. Before raising that: requests beyond the pool
            # queue *inside* httpx, landing the wait in latency_s and mapping
            # PoolTimeout to a retryable timeout. Size limits to the semaphore.
            factory=lambda: httpx.AsyncClient(timeout=self.timeout_s),
            closer=lambda client: client.aclose(),
        )

    # -- request construction (shared by both paths) ------------------------

    def _api_key(self):
        # The explicit DB-resolved key wins; the env var is the fallback.
        api_key = self.api_key or os.environ.get(self.api_key_env)
        if api_key:
            return api_key
        # LLMAuthError subclasses RuntimeError, keeping the pre-taxonomy
        # exception contract.
        raise LLMAuthError(
            f"{self.provider_label} provider selected but {self.api_key_env} "
            f"is not set. Add it to your .env file.",
            provider=self.provider_name,
        )

    def _post(self, body):
        """One request body, addressed and authenticated: ``(url, kwargs)``."""
        return f"{self.base_url}/chat/completions", {
            "headers": {
                "Authorization": f"Bearer {self._api_key()}",
                "Content-Type": "application/json",
            },
            "json": body,
        }

    def _request(self, prompt, max_tokens):
        return self._post(
            {
                "model": self.model,
                "max_tokens": max_tokens or self.default_max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
        )

    def _chat_request(self, messages, tools, max_tokens):
        """Chat-shaped counterpart of :meth:`_request` (MUS-29): same endpoint,
        tools in the ``{"type": "function", "function": {...}}`` envelope."""
        body = {
            "model": self.model,
            "max_tokens": max_tokens or self.default_max_tokens,
            "messages": [_wire_chat_message(m) for m in messages],
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": dict(t.parameters),
                    },
                }
                for t in tools
            ]
        return self._post(body)

    # -- the two call paths -------------------------------------------------

    def generate(self, prompt, max_tokens=None, timeout=None) -> LLMResult:
        url, kwargs = self._request(prompt, max_tokens)
        # The clock stops before raise_for_status()/.json() so this matches
        # Claude's timing -- see LLMResult.latency_s.
        started = time.perf_counter()
        try:
            response = httpx.post(
                url,
                timeout=timeout if timeout is not None else self.timeout_s,
                **kwargs,
            )
            latency_s = time.perf_counter() - started
            response.raise_for_status()
            data = response.json()
        except _REQUEST_ERRORS as exc:
            raise map_httpx_error(exc, self.provider_name).with_latency(
                time.perf_counter() - started
            ) from exc

        return self._build_result(data, latency_s)

    async def _apost(self, url, kwargs, timeout) -> LLMResult:
        """Send one already-built request on the async client — client, timing,
        status check and error mapping live here once (MUS-66)."""
        # The default timeout lives on the AsyncClient; only repeated here when
        # this one call overrides it.
        if timeout is not None:
            kwargs["timeout"] = timeout
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

    async def agenerate(self, prompt, max_tokens=None, timeout=None) -> LLMResult:
        url, kwargs = self._request(prompt, max_tokens)
        return await self._apost(url, kwargs, timeout)

    async def agenerate_chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResult:
        # Async-only by design; see the base class.
        url, kwargs = self._chat_request(messages, tools, max_tokens)
        return await self._apost(url, kwargs, timeout)

    async def aclose(self) -> None:
        await self._async_client.aclose()

    def _read_tool_calls(self, message: object) -> tuple[ToolCallRequest, ...]:
        """Collect ``message.tool_calls`` into :class:`ToolCallRequest`s.

        Every entry is either read or raised on — dropping one silently
        under-executes the model's calls (a bug until MUS-66).
        ``function.arguments`` arrives as a JSON *string*, and ``"{}"``, ``""``,
        ``"null"`` and an omitted key all mean "no arguments"; anything else
        that does not parse to a JSON object raises.
        """
        calls = []
        for entry in read_sequence(message, "tool_calls"):
            function = read_field(entry, "function")
            call_id = coerce_text(read_field(entry, "id"))
            name = coerce_text(read_field(function, "name"))
            if not (call_id and name):
                raise LLMMalformedResponseError(
                    f"{self.provider_label} returned a tool call with no id or no name.",
                    provider=self.provider_name,
                )
            calls.append(
                ToolCallRequest(
                    id=call_id,
                    name=name,
                    arguments=self._read_arguments(read_field(function, "arguments")),
                )
            )
        return tuple(calls)

    def _read_arguments(self, raw: object) -> dict:
        """One entry's ``function.arguments`` as a mapping; raise if it isn't one."""
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return {}
        if not isinstance(raw, str):
            raise LLMMalformedResponseError(
                f"{self.provider_label} returned tool-call arguments that are not a JSON string.",
                provider=self.provider_name,
            )
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMMalformedResponseError(
                f"{self.provider_label} returned tool-call arguments that are not valid JSON.",
                provider=self.provider_name,
            ) from exc
        if arguments is None:
            return {}
        if not isinstance(arguments, dict):
            raise LLMMalformedResponseError(
                f"{self.provider_label} returned tool-call arguments that are not a JSON object.",
                provider=self.provider_name,
            )
        return arguments

    def _build_result(self, data, latency_s) -> LLMResult:
        """Turn a chat-completions body into an :class:`LLMResult`.

        The message must say *something* — text, tool calls, or both;
        ``content`` is legitimately ``null`` on a pure tool-call response
        (MUS-29). ``usage``/``model`` are optional per spec: absence yields
        ``None``, not an error and not a zero.
        """
        try:
            message = data["choices"][0]["message"]
        except _SHAPE_ERRORS as exc:
            raise map_httpx_error(exc, self.provider_name) from exc
        tool_calls = self._read_tool_calls(message)
        content = read_field(message, "content")
        text = content.strip() if isinstance(content, str) else ""

        first_choice = _first_choice(data)
        raw_finish_reason = coerce_text(read_field(first_choice, "finish_reason"))
        finish_reason = normalize_finish_reason(raw_finish_reason)

        if not text and not tool_calls:
            raise LLMEmptyCompletionError(
                f"{self.provider_label} returned an empty completion.",
                provider=self.provider_name,
            )
        # A signalled tool call with none surviving parsing: `content` is the
        # model's reasoning, not an answer. Checked here, a fact about the
        # response, so every caller gets it -- not just `run_agent_lead`.
        if finish_reason == FINISH_TOOL_CALLS and not tool_calls:
            raise LLMEmptyCompletionError(
                f"{self.provider_label} signalled a tool call but sent none we could read.",
                provider=self.provider_name,
            )

        usage = _mapping_or_empty(data.get("usage"))
        prompt_details = _mapping_or_empty(usage.get("prompt_tokens_details"))
        return LLMResult(
            text=text,
            provider=self.provider_name,
            model=self.model,
            response_model=coerce_text(data.get("model")),
            input_tokens=coerce_token_count(usage.get("prompt_tokens")),
            output_tokens=coerce_token_count(usage.get("completion_tokens")),
            # Cached tokens are reported WITHIN prompt_tokens (unlike Anthropic)
            # and there is no cache-write notion; Groq omits the block entirely.
            cache_read_tokens=coerce_token_count(prompt_details.get("cached_tokens")),
            finish_reason=normalize_finish_reason(raw_finish_reason),
            raw_finish_reason=raw_finish_reason,
            latency_s=latency_s,
            tool_calls=tool_calls,
        )


def _wire_chat_message(message: Message) -> dict[str, object]:
    """One provider-neutral :class:`~.chat_types.Message`, chat-completions shaped.

    Tool-call ``arguments`` are re-serialized to the JSON string the wire
    format demands; ``content`` is ``null``, not ``""``, on a text-less
    assistant turn — some providers reject the empty string.
    """
    if message.role == "assistant":
        wire: dict[str, object] = {"role": "assistant", "content": message.content or None}
        if message.tool_calls:
            wire["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(dict(c.arguments))},
                }
                for c in message.tool_calls
            ]
        return wire
    if message.role == "tool_result":
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content}
    return {"role": message.role, "content": message.content}


def _mapping_or_empty(value):
    """``value`` when it is a dict, else ``{}`` -- so a provider sending
    ``"usage": null`` costs us a ``None`` field, not an exception."""
    return value if isinstance(value, dict) else {}


def _first_choice(data):
    """The first ``choices`` entry as a dict, or ``{}`` — ``choices[0]`` exists
    by now; this guards the surrounding metadata's shape."""
    choices = read_sequence(data, "choices")
    if choices:
        return _mapping_or_empty(choices[0])
    return {}
