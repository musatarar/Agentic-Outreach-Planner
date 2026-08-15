"""Provider-neutral chat/tool-calling data shapes (MUS-29).

Pure stdlib on purpose: these dataclasses cross the LLM adapter boundary in both
directions (request messages out, tool-call requests back), so they must not
import Django, a provider SDK, or anything above ``services/llm``. Each adapter
translates them into its own wire format; nothing outside the adapters may
depend on a provider's block/message types.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool offered to the model: a name, a description, a JSON schema."""

    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """The model asking for one tool execution.

    ``id`` is the provider's correlation id: the matching ``tool_result``
    message must echo it back as ``tool_call_id`` or the provider rejects the
    conversation.
    """

    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Message:
    """One turn of a chat conversation, provider-neutral.

    ``role`` is one of ``"user"``, ``"assistant"``, ``"tool_result"``. A
    ``tool_result`` message carries the executed tool's rendered output in
    ``content`` and the originating call's id in ``tool_call_id``; an assistant
    message may carry both text and the tool calls it requested.
    """

    role: str
    content: str = ""
    tool_calls: tuple[ToolCallRequest, ...] = ()
    tool_call_id: str = ""
