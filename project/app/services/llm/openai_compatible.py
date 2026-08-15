"""Shared adapter for any OpenAI Chat Completions-compatible provider.

OpenAI (ChatGPT), DeepSeek, and Groq all expose the same
``POST {base_url}/chat/completions`` wire format, so one ``httpx``-based client
covers all three. Subclasses set ``base_url``, ``api_key_env``, ``provider_name``
and a default ``model``. The API key comes from the explicit ``api_key`` passed
in by the factory (:mod:`project.app.services.llm`), which resolves it from the
database; ``api_key_env`` is read directly only as a fallback when ``api_key``
is unset.

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

# Per-HTTP-attempt timeout. A constructor argument rather than a module
# constant so MUS-26 only has to wire Django settings into the caller; the value
# is unchanged from the constant it replaces.
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
    # Our configured name for this provider ("groq", "chatgpt", ...), carried
    # on LLMError.provider so a failure joins back to the configured provider.
    # Annotated rather than defaulted, like base_url above: a subclass that
    # forgets it should fail loudly instead of tagging its errors with a
    # provider name that matches no configuration.
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
            # Connection limits are left at httpx's defaults (100 connections),
            # comfortably above the 8-way semaphore MUS-26 plans. Worth knowing
            # before raising that number: requests beyond the pool size queue
            # *inside* httpx, so the wait lands in latency_s (which is supposed
            # to be provider time only) and a PoolTimeout would map to a
            # retryable LLMTimeoutError -- a local queueing problem that looks
            # like a provider blip and answers it with more load. Size limits to
            # the semaphore when that config arrives.
            factory=lambda: httpx.AsyncClient(timeout=self.timeout_s),
            closer=lambda client: client.aclose(),
        )

    # -- request construction (shared by both paths) ------------------------

    def _api_key(self):
        # The explicit key resolved from the database wins; the provider's own
        # env var is the fallback for a deployment that never saved one.
        api_key = self.api_key or os.environ.get(self.api_key_env)
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
        """Chat-shaped counterpart of :meth:`_request` (MUS-29).

        Same endpoint, same headers; the body carries the full conversation
        translated by :func:`_wire_chat_message` plus, when offered, the tools
        in the ``{"type": "function", "function": {...}}`` envelope every
        OpenAI-compatible provider expects.
        """
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
        # Times the provider call and nothing else. httpx.post is
        # non-streaming, so the body is fully read by the time it returns --
        # which means the clock has to stop HERE, before raise_for_status() and
        # .json(), or Claude and this adapter would be reporting different
        # things under the same field name. See LLMResult.latency_s.
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
        """Send one already-built request on the async client.

        The whole async path — client, timing, status check, error mapping —
        lives here once, so the completion and chat entry points differ only in
        which builder produced ``(url, kwargs)``. They were byte-for-byte
        copies of each other before MUS-66, which is one place too many for the
        next fix to the latency clock or the error taxonomy to land.
        """
        # The default timeout lives on the AsyncClient, so it is only repeated
        # here when this one call is overriding it.
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
        # Mirrors agenerate: same client, timeout, and error mapping — only the
        # request body differs. Async-only by design; see the base class.
        url, kwargs = self._chat_request(messages, tools, max_tokens)
        return await self._apost(url, kwargs, timeout)

    async def aclose(self) -> None:
        await self._async_client.aclose()

    def _read_tool_calls(self, message: object) -> tuple[ToolCallRequest, ...]:
        """Collect ``message.tool_calls`` into :class:`ToolCallRequest`s.

        Every entry is either read or raised on. Dropping one silently (which
        this did until MUS-66) leaves the loop executing fewer calls than the
        model asked for, with no error and no log line to say so, and when the
        last entry drops it promotes the turn's narration to the draft — the
        provider said "tool call", we heard "email".

        ``function.arguments`` arrives as a JSON *string* on this wire format,
        and "no arguments" is spelled at least four ways across the providers
        sharing it: ``"{}"``, ``""``, ``"null"``, or the key omitted entirely.
        All four are the same request, and all four of this app's agent tools
        take zero arguments — treating the blank spellings as a contract
        violation failed the run on a *correct* provider response. Anything
        else that does not parse to a JSON object still raises: the alternative
        is executing a tool with silently-wrong arguments.
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

        The one thing we insist on is that the message says *something* — text,
        tool calls, or both. Every way the subscript chain down to ``message``
        can break (missing key, empty ``choices``) arrives at the caller as a
        single ``LLMMalformedResponseError`` rather than as a ``KeyError``
        leaking out of the LLM layer with no provider attached. ``content`` is
        legitimately ``null`` on a pure tool-call response (MUS-29), so missing
        text is only an error when there are no tool calls either.

        Everything else is best-effort. ``usage`` and ``model`` are not part of
        the contract we depend on: Groq sends them, and the spec permits their
        absence, so a provider that omits them yields ``None`` and a healthy
        result — not an error, and emphatically not a zero.
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
        # The provider says the model wanted to call a tool, and not one call
        # survived parsing. Whatever sits in `content` is the model talking to
        # itself on the way to that call, NOT an answer -- handing it back lets
        # the agent loop finalize a monologue as the outreach email, which is
        # how "To write a tailored outreach email, I should start by gathering
        # more context about the lead." reached a reviewer as Suggested Copy.
        #
        # Checked here rather than in the loop because it is a fact about the
        # response, so every caller gets it rather than just `run_agent_lead`.
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
            # Unlike Anthropic, OpenAI-compatible providers report cached tokens
            # as a breakdown WITHIN prompt_tokens rather than alongside it, and
            # have no notion of a cache write. Groq omits the block entirely.
            cache_read_tokens=coerce_token_count(prompt_details.get("cached_tokens")),
            finish_reason=normalize_finish_reason(raw_finish_reason),
            raw_finish_reason=raw_finish_reason,
            latency_s=latency_s,
            tool_calls=tool_calls,
        )


def _wire_chat_message(message: Message) -> dict[str, object]:
    """One provider-neutral :class:`~.chat_types.Message`, chat-completions shaped.

    An assistant turn's tool calls ride in the ``tool_calls`` array with
    ``arguments`` re-serialized to the JSON string this wire format demands
    (the inverse of the ``json.loads`` in ``_read_tool_calls``); a
    ``tool_result`` turn is the ``role: "tool"`` message echoing
    ``tool_call_id``. ``content`` is ``null``, not ``""``, on a text-less
    assistant turn — some providers reject an empty string there.
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
    """The first ``choices`` entry as a dict, or ``{}``.

    Reached only after the text extraction above succeeded, so ``choices[0]``
    exists; the guard is for the metadata *around* it being a shape we didn't
    expect.
    """
    choices = read_sequence(data, "choices")
    if choices:
        return _mapping_or_empty(choices[0])
    return {}
