---
name: checkpointing-work
description: Use when a step or todo on a tracked ticket just completed, when stopping for human review or a merge, when context is about to be compacted or the session is ending, or when something was deliberately left undone — any point where a fresh agent would need to know where the work stands.
---

# Checkpointing Work

## Overview

A checkpoint is a comment on the ticket that owns the work, written so that an agent
with **zero conversation context** can execute the next step from it alone. Chat is
not a checkpoint — the chat dies, the ticket persists. Post to the ticket, then
mirror the headline and next step to chat.

**The test:** could someone who never saw this conversation continue from this
comment without re-exploring the repo? If no, it is a status update, not a checkpoint.

## When to Use

- A step or todo in a multi-step plan completed — always, not only when stopping
- Stopping for human review, merge, or approval
- Context is about to be compacted, or the session is ending
- Direction changed, a decision was made, or scope was deliberately left undone
- Not for: narrating progress mid-step, or every tool call

## Required slots

The comment **is** these six parts, in this order. Every slot appears in every
checkpoint; write "none" rather than dropping one.

1. **Headline** — `Checkpoint — step N of M complete` (or `blocked` / `stopping for
   review`), plus the commit SHA and branch name.
2. **What changed** — the substantive decisions and the reasoning a reviewer would
   want to argue with. Not a file list; the diff already lists files.
3. **Evidence** — the commands actually run and their real numbers: test counts, exit
   codes, red counts, lint results. Never the word "verified" without the number
   behind it. If something was not run, say which and why.
4. **Deliberate leftovers** — anything transitional, broken on purpose, out of scope,
   or skipped, and why. This is the slot that stops the next agent from "fixing"
   intended state.
5. **Repo coordinates** — branch, worktree path, base commit, whether the tree is
   clean, and any stray uncommitted state elsewhere worth knowing about.
6. **Exact next step** — the next action in enough detail to begin without re-planning,
   and what it is waiting on ("waiting on continue", "waiting on the merge").

## Quick reference

| Slot | Failure it prevents |
|---|---|
| Headline + SHA | Next agent cannot tell which commit the claims describe |
| What changed | Decisions get silently re-litigated or reversed |
| Evidence | "Done" that was never actually run |
| Deliberate leftovers | Next agent "repairs" intentional transitional state |
| Repo coordinates | Work lands in the wrong worktree, or on top of stale base |
| Exact next step | Next agent re-plans from the ticket description |

## Linear specifics

- Post with `save_comment` on the ticket that owns the work — the sub-issue if the
  work is scoped to one, the parent if it spans them.
- **The connector's WAF rejects code-shaped bodies**: regex literals, shell commands,
  SQL, glob patterns, long flag strings. Write them as prose — "the modules flag",
  "the backend test glob", "lowercase letters, digits and underscores". Chunk very
  long bodies into separate comments rather than fighting the rejection.
- Keep the numbered `step N of M` scheme consistent with the ticket's own execution
  protocol, so the sequence reads as one log.

## Common mistakes

- **Posting only to chat.** The most common and most expensive one.
- **Claiming completion without numbers.** "Tests pass" is not evidence; "96 tests
  green via unittest discovery over the gate test directory" is.
- **Describing the diff instead of the decisions.** The next agent can read a diff.
- **Omitting the known-broken state.** If three files still reference a deleted API by
  design, say so, or the next agent will treat it as a bug.
- **A vague next step.** "Continue with the tests" forces re-planning; name the files,
  the classes, and the acceptance condition.
