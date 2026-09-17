#!/usr/bin/env python3
"""Call the configured provider for real and print the structured result.

The test suite mocks every provider, so this is the only thing that shows
whether a live endpoint accepts the ``response_format`` block we build and
answers with JSON that validates. Hand-run, never in CI: it spends one
completion per run.

Usage::

    python scripts/try_structured_output.py                     # the configured provider
    python scripts/try_structured_output.py --provider chatgpt
    python scripts/try_structured_output.py --text "Dana and Wu meet Tuesday."
    python scripts/try_structured_output.py --async             # the agenerate_structured path
    python scripts/try_structured_output.py --no-schema         # same prompt, unconstrained

Provider, model and key come from the environment (``.env`` included); the
input comes from the command line, never from the database, so no
lead-controlled text reaches the provider. Everything it shows you goes to the
terminal — it writes no file and no log.

Exits 0 when the completion validated, 1 when the provider failed or ignored
the schema.
"""

import argparse
import asyncio
import json
import os
import sys

# Make the project package importable when run as a standalone script.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "project.settings")

import django  # noqa: E402

# Only for its .env loading: the LLM layer reads os.environ, not settings.
django.setup()

from pydantic import BaseModel  # noqa: E402

from project.app.services.llm import (  # noqa: E402
    build_client,
    errors,
    get_llm_client,
    response_format_for,
)
from project.app.services.llm.chat_types import Message  # noqa: E402


class CalendarEvent(BaseModel):
    name: str
    date: str
    participants: list[str]


INSTRUCTION = "Extract the event information."
SAMPLE_TEXT = "Alice and Bob are going to a science fair on Friday."


def _rule(title):
    print(f"\n--- {title} " + "-" * max(0, 68 - len(title)))


def _report(result):
    """Everything the call produced: the completion, the parsed fields, the meter."""
    _rule("raw completion")
    print(result.result.text)

    _rule("parsed")
    print(f"type         {type(result.parsed).__name__}")
    print(f"name         {result.parsed.name!r}")
    print(f"date         {result.parsed.date!r}")
    print(f"participants {result.parsed.participants!r}")

    _rule("call")
    meta = result.result
    print(f"served model {meta.response_model or meta.model}")
    print(f"finish       {meta.finish_reason} ({meta.raw_finish_reason})")
    print(f"tokens       in={meta.input_tokens} out={meta.output_tokens}")
    print(f"latency      {meta.latency_s:.2f}s" if meta.latency_s else "latency      n/a")


async def _structured_async(client, turns):
    try:
        return await client.agenerate_structured(turns, CalendarEvent)
    finally:
        await client.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--provider", help="Override LLM_PROVIDER for this run.")
    parser.add_argument("--text", default=SAMPLE_TEXT, help="The text to extract from.")
    parser.add_argument(
        "--async",
        dest="run_async",
        action="store_true",
        help="Exercise agenerate_structured instead of generate_structured.",
    )
    parser.add_argument(
        "--no-schema",
        action="store_true",
        help="Send the same prompt with no response_format, to compare.",
    )
    args = parser.parse_args()

    client = build_client(args.provider) if args.provider else get_llm_client()
    print(f"provider {client.provider_name}, model {client.model}")

    try:
        if args.no_schema:
            _rule("raw completion (unconstrained)")
            print(client.complete(f"{INSTRUCTION}\n\n{args.text}"))
            return 0

        _rule("response_format sent")
        print(json.dumps(response_format_for(CalendarEvent), indent=2))

        turns = [
            Message(role="system", content=INSTRUCTION),
            Message(role="user", content=args.text),
        ]
        if args.run_async:
            result = asyncio.run(_structured_async(client, turns))
        else:
            result = client.generate_structured(turns, CalendarEvent)
    except errors.LLMMalformedResponseError as exc:
        print(f"\nThe provider ignored the schema: {exc}", file=sys.stderr)
        print("Re-run with --no-schema to see what it says instead.", file=sys.stderr)
        return 1
    except errors.LLMError as exc:
        print(f"\n{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    _report(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
