"""Repo hygiene: the worktree layout CLAUDE.md mandates must stay untracked."""

import subprocess

from django.conf import settings
from django.test import SimpleTestCase

REPO_ROOT = settings.BASE_DIR
WORKTREE_PATH = ".claude/worktrees/some-task"


class GitignoreTests(SimpleTestCase):
    def test_the_documented_worktree_path_is_ignored(self):
        """One worktree per branch under `.claude/worktrees/<name>` is the git
        rule in CLAUDE.md; unignored, every one of them shows up as untracked
        and is one `git add -A` away from being committed into the repo."""
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", WORKTREE_PATH],
            cwd=REPO_ROOT,
            capture_output=True,
        )

        self.assertEqual(ignored.returncode, 0, f"{WORKTREE_PATH} is not ignored")
