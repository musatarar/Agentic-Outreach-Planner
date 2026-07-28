"""Dump (suggested, edited) copy pairs as JSONL for the copy eval (MUS-39).

This is the quiet payoff of the triage queue. Every correction a reviewer makes
to a generated draft is labeled data about what the model got wrong, and it is
only capturable at the moment of editing -- which is why ``suggested_copy`` is
immutable and edits live in their own append-only table.

The output feeds MUS-21's LLM-judge harness. One JSON object per line, so it
streams and diffs cleanly:

    python manage.py dump_edit_corpus --committed-only > corpus.jsonl
"""

import json

from django.core.management.base import BaseCommand
from django.utils.dateparse import parse_date

from project.app.models import OutreachEdit


class Command(BaseCommand):
    help = "Dump reviewer copy edits as JSONL for the copy eval corpus."

    def add_arguments(self, parser):
        parser.add_argument(
            "--since",
            help="Only edits created on or after this date (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--committed-only",
            action="store_true",
            help="Only the edit that was in place at approval -- what a human actually sent.",
        )
        parser.add_argument("--output", help="Write to this file instead of stdout.")

    def handle(self, *args, **options):
        edits = OutreachEdit.objects.select_related("outreach_action", "outreach_action__lead")

        since = options.get("since")
        if since:
            parsed = parse_date(since)
            if parsed is None:
                self.stderr.write(f"unrecognized --since date: {since}")
                return
            edits = edits.filter(created_at__date__gte=parsed)
        if options["committed_only"]:
            edits = edits.filter(committed=True)

        lines = [json.dumps(self._record(edit), ensure_ascii=False) for edit in edits]
        payload = "\n".join(lines)

        output = options.get("output")
        if output:
            with open(output, "w", encoding="utf-8") as handle:
                handle.write(payload + ("\n" if payload else ""))
            self.stdout.write(f"wrote {len(lines)} edit(s) to {output}")
        else:
            if payload:
                self.stdout.write(payload)
            self.stderr.write(f"{len(lines)} edit(s)")

    @staticmethod
    def _record(edit):
        action = edit.outreach_action
        return {
            "edit_id": edit.id,
            "action_id": action.id,
            "lead_id": action.lead_id,
            "action_type": action.action_type,
            "created_at": edit.created_at.isoformat(),
            "editor": edit.editor,
            "committed": edit.committed,
            # The pair the judge scores: what the model wrote, what went out.
            "suggested": action.suggested_copy,
            "edited": edit.after_text,
            "before_text": edit.before_text,
            "diff_ops": edit.diff_ops,
            "chars_added": edit.chars_added,
            "chars_removed": edit.chars_removed,
            "similarity": edit.similarity,
        }
