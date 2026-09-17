#!/usr/bin/env python3
"""Run one lead's inference rules against the real provider and print what came back.

The test suite mocks every provider, so this is the only thing that shows
whether a live model answers a verdict per candidate rule, quotes the lead's own
words as evidence, and stays inside the schema. Hand-run, never in CI: it spends
one completion per run.

Usage::

    python scripts/try_inference.py                       # first owned lead, its owner's rules
    python scripts/try_inference.py --lead lead_007
    python scripts/try_inference.py --owner bd@lockedin.example
    python scripts/try_inference.py --provider chatgpt
    python scripts/try_inference.py --today 2026-03-17
    python scripts/try_inference.py --prompt-only          # print the prompt, spend nothing

Reads the local database, so run ``python manage.py migrate``,
``python scripts/populate_demo_data.py`` and ``python manage.py
seed_rules_catalog`` first. Unlike try_structured_output.py this does send
stored CRM text to the provider — sanitized and fenced, exactly as the app
would. Everything it shows goes to the terminal; it writes no file and no log.

Exits 0 when the call returned a section, 1 when the provider failed or the
database has nothing to ask about.
"""

import argparse
import datetime
import json
import os
import sys

# Make the project package importable when run as a standalone script.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "project.settings")

import django  # noqa: E402

django.setup()

from project.app.models import Lead, OutreachRule  # noqa: E402
from project.app.rules import inference, services  # noqa: E402
from project.app.services.llm import build_client, errors, get_llm_client  # noqa: E402


class _Recorder:
    """Wraps the real client so the script can show the prompt and the raw reply."""

    def __init__(self, client):
        self.client = client
        self.prompt = None
        self.result = None

    def generate_structured(self, input, schema_model, **kwargs):
        self.prompt = input
        structured = self.client.generate_structured(input, schema_model, **kwargs)
        self.result = structured.result
        return structured


def _rule(title):
    print(f"\n--- {title} " + "-" * max(0, 68 - len(title)))


def _resolve_lead(lead_id, owner_email):
    leads = Lead.objects.prefetch_related("events").filter(owner__isnull=False)
    if owner_email:
        leads = leads.filter(owner__username=owner_email)
    if lead_id:
        leads = leads.filter(pk=lead_id)
    return leads.order_by("pk").first()


def _candidates(owner):
    return list(services.enabled_rules_for(owner).filter(kind=OutreachRule.KIND_INFERENCE))


def _report(recorder, section):
    if recorder.result is not None:
        _rule("raw completion")
        print(recorder.result.text)

    _rule("section returned")
    print(json.dumps(section, indent=2, ensure_ascii=False))

    if not section["verdicts"] and section["unevaluable_rule_ids"]:
        print("\nEvery candidate came back unevaluable — the reply missed the schema.")

    meta = recorder.result
    if meta is None:
        return
    _rule("call")
    print(f"served model {meta.response_model or meta.model}")
    print(f"finish       {meta.finish_reason} ({meta.raw_finish_reason})")
    print(f"tokens       in={meta.input_tokens} out={meta.output_tokens}")
    print(f"cache        read={meta.cache_read_tokens} write={meta.cache_write_tokens}")
    print(f"latency      {meta.latency_s:.2f}s" if meta.latency_s else "latency      n/a")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lead", help="Lead id to judge (default: the first owned lead).")
    parser.add_argument("--owner", help="Email of the user whose rules to use.")
    parser.add_argument("--provider", help="Override LLM_PROVIDER for this run.")
    parser.add_argument("--today", help="Run date as YYYY-MM-DD (default: today).")
    parser.add_argument(
        "--prompt-only",
        action="store_true",
        help="Print the prompt that would be sent and stop, without calling a provider.",
    )
    args = parser.parse_args()

    lead = _resolve_lead(args.lead, args.owner)
    if lead is None:
        print(
            "No owned lead matched. Seed the database first:\n"
            "  python scripts/populate_demo_data.py && python manage.py seed_rules_catalog",
            file=sys.stderr,
        )
        return 1

    candidates = _candidates(lead.owner)
    if not candidates:
        print(
            f"{lead.owner.username} has no enabled inference rules to ask about; "
            "add one in the rules catalog, or run: python manage.py seed_rules_catalog",
            file=sys.stderr,
        )
        return 1

    today = datetime.date.fromisoformat(args.today) if args.today else datetime.date.today()
    candidates.sort(key=lambda rule: rule.pk)
    print(f"lead {lead.pk} ({lead.agency_name}), owner {lead.owner.username}, today {today}")
    print(f"candidates {[rule.pk for rule in candidates]}")

    if args.prompt_only:
        _rule("prompt")
        print(inference.build_prompt(candidates, lead, today))
        return 0

    client = build_client(args.provider) if args.provider else get_llm_client()
    print(f"provider {client.provider_name}, model {client.model}")
    recorder = _Recorder(client)

    _rule("prompt")
    print(inference.build_prompt(candidates, lead, today))
    try:
        section = inference.infer(candidates, lead, today, client=recorder)
    except NotImplementedError as exc:
        print(f"\n{exc}", file=sys.stderr)
        print("Structured outputs live on the OpenAI-compatible adapters today.", file=sys.stderr)
        return 1
    except errors.LLMError as exc:
        print(f"\n{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    _report(recorder, section)
    return 0


if __name__ == "__main__":
    sys.exit(main())
