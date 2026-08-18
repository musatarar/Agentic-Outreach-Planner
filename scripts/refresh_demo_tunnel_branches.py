"""Rewrite the generated branch dropdown in `.github/workflows/demo-tunnel.yml`.

Dispatch forms are rendered from the workflow file before any runner exists, so a
`choice` input's options can only ever be literals in the file. Keeping them
current means editing the file — which is what `demo-tunnel-branches.yml` runs
this for. Branch names arrive on stdin, one per line.
"""

import json
import re
import sys
from pathlib import Path

BEGIN = "# <<< generated"
END = "# >>> generated"

DEFAULT_BRANCH = "master"
# GitHub does not document a ceiling on choice options, and a dropdown of every
# branch a busy repo has is unusable well before any ceiling. `dropped()` reports
# what this cost so the cut is never silent.
LIMIT = 20


# Branch names are free to look like YAML syntax (`*odd`, `a: b`), so anything
# not obviously inert gets quoted. Ordinary names stay bare, and their diffs
# stay readable.
PLAIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def as_yaml_scalar(name):
    return name if PLAIN.match(name) else json.dumps(name)


class MarkersNotFound(RuntimeError):
    """The workflow lost its generated block; writing nothing would leave a
    stale dropdown looking freshly generated."""


def render_options(branches, limit=LIMIT):
    """The dropdown's options: master first, then the rest in the given order."""
    rest = [b for b in dict.fromkeys(branches) if b != DEFAULT_BRANCH]
    return [DEFAULT_BRANCH, *rest][:limit]


def dropped(branches, limit=LIMIT):
    """How many branches the cap left out."""
    return max(0, len(render_options(branches, limit=len(branches) + 1)) - limit)


def rewrite(text, branches, limit=LIMIT):
    """Replace the marked block, leaving every other line of the file alone."""
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if BEGIN in line]
    ends = [i for i, line in enumerate(lines) if END in line]
    if not starts or not ends:
        raise MarkersNotFound(f"{BEGIN} / {END} not found")

    start, end = starts[0], ends[0]
    indent = " " * (len(lines[start]) - len(lines[start].lstrip()))
    block = [f"{indent}options:\n"]
    block += [f"{indent}  - {as_yaml_scalar(b)}\n" for b in render_options(branches, limit)]

    return "".join(lines[: start + 1] + block + lines[end:])


def main():
    workflow = Path(__file__).resolve().parent.parent / ".github/workflows/demo-tunnel.yml"
    branches = [line.strip() for line in sys.stdin if line.strip()]
    if not branches:
        raise SystemExit("no branches on stdin")

    cut = dropped(branches)
    if cut:
        print(f"capped at {LIMIT} of {len(branches)} branches; {cut} left out", file=sys.stderr)

    before = workflow.read_text()
    after = rewrite(before, branches)
    workflow.write_text(after)
    print("changed" if after != before else "unchanged")


if __name__ == "__main__":
    main()
