"""Structured outputs: a pydantic model in, a validated instance out.

Everything provider-specific about the feature is the ``response_format`` block
built here; everything pydantic-specific is the schema derivation and the
validation. The adapter only threads the block into its request body and hands
the completion text back. Pure stdlib plus pydantic — no Django import, at
module level or anywhere else (see the async seam in CLAUDE.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from .base import LLMResult
from .errors import LLMMalformedResponseError

ModelT = TypeVar("ModelT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class StructuredResult(Generic[ModelT]):
    """A completed call whose text validated against the requested schema.

    ``parsed`` is the model instance; ``result`` is the ordinary
    :class:`~.base.LLMResult`, so usage, latency and finish reason survive the
    structured path unchanged.
    """

    parsed: ModelT
    result: LLMResult


def response_format_for(schema_model: type[BaseModel], *, strict: bool = True) -> dict[str, Any]:
    """The ``response_format`` request field that asks for ``schema_model``."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema_model.__name__,
            "strict": strict,
            "schema": strict_schema(schema_model.model_json_schema()),
        },
    }


def parse(schema_model: type[ModelT], text: str, *, provider: str, label: str) -> ModelT:
    """Validate one completion into ``schema_model``.

    Text that is not JSON and JSON that does not fit the schema are the same
    failure to a caller — the provider ignored the format we asked for — so both
    raise :class:`~.errors.LLMMalformedResponseError`.
    """
    try:
        return schema_model.model_validate_json(text)
    except ValidationError as exc:
        raise LLMMalformedResponseError(
            f"{label} returned a completion that does not match {schema_model.__name__}.",
            provider=provider,
        ) from exc


def strict_schema(node: Any) -> Any:
    """``model_json_schema()`` output with strict mode's closed-object rules applied.

    Strict decoding rejects a schema whose objects allow unlisted keys or leave
    a declared property optional, and pydantic emits neither restriction — so
    every object node, nested and under ``$defs`` alike, gets
    ``additionalProperties: false`` and a ``required`` list naming every
    property. A field the schema leaves out of ``required`` therefore has to be
    nullable in the model rather than merely defaulted.
    """
    if isinstance(node, list):
        return [strict_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    schema = {key: strict_schema(value) for key, value in node.items()}
    if isinstance(schema.get("properties"), dict):
        schema["additionalProperties"] = False
        schema["required"] = list(schema["properties"])
    return schema
