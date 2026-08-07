---
name: resuming-from-checkpoint
description: Use when picking up tracked work with no conversation context for it — a bare ticket ID or ticket URL, "continue", "restart from step N", a pasted handoff prompt, or the first turn after a context refresh or compaction.
---

# Resuming From a Checkpoint

## Overview

**The ticket is the plan. The checkpoints are the log. The repo is the truth.**

Read the first two, then verify against the third *before touching anything*. A
checkpoint describes what was true when it was written; commits, merges, and stray
uncommitted work may have moved since.

Goal: reach the point of doing real work without re-exploring what the ticket already
specifies.

## Procedure

1. **Read the ticket.** `get_issue` on the identifier. The description holds the spec,
   the file-by-file plan, and the numbered execution protocol. Do not re-derive any of
   it from the codebase.
2. **Read the checkpoints.** `list_comments` on the same ticket — **order by
   `createdAt`, not the default `updatedAt`**. Under the default, an edited older
   comment sorts first and you will resume from the wrong step.
3. **Take the next step from the newest checkpoint**, unless the human's message
   overrides it ("restart from step 3" wins over a checkpoint that says step 4).
4. **Verify the coordinates before acting.** REQUIRED, in one batch:
   - `git worktree list` — which worktree owns this branch
   - `git -C <worktree> log --oneline -3` — does the checkpoint's SHA exist, and is it HEAD
   - `git -C <worktree> status --short` — is the tree clean, or is there work no
     checkpoint mentions
   - `git fetch` then compare against the base branch — is the branch still current, or
     does it need a rebase
5. **Reconcile out loud.** If the repo disagrees with the checkpoint — extra commits,
   uncommitted work, a moved base, stray edits in the main checkout — state the
   discrepancy in one or two sentences before proceeding. Discarding work is the
   human's call; preserve it (`git stash push -m`) rather than resetting.
6. **Then work**, in the worktree the branch lives in. Never in the main checkout.

## Read next, only if needed

Pull these on demand — not upfront:

| Question | Where the answer already is |
|---|---|
| What are the repo's rules and commands? | `CLAUDE.md` |
| What is the enforced branch/PR flow? | `CLAUDE.md`, `docs/ci.md` |
| What did the previous step actually change? | `git show <SHA>` on the checkpoint's commit |
| What must not be touched? | The protected-path list in the gate script |

## Red flags — stop and verify

- "The checkpoint says it is done, so it is done"
- Reading only the newest comment, or only the ticket description
- Editing files before knowing which worktree and branch the work belongs to
- Re-reading the whole codebase to rebuild a plan the ticket already contains
- Resetting or discarding uncommitted work you did not create

## Emitting a handoff prompt

When handing off to an agent that cannot read the ticket, the prompt **is** these
parts, in this order:

1. The ticket URL and a one-line statement of the goal
2. Repo coordinates: worktree path, branch, HEAD SHA, base branch, tree state
3. The exact next step, and its acceptance condition
4. The behavioural constraints that are not discoverable from the code — never merge,
   worktree-only, protected paths, stop-and-wait points
5. Pointers to where the spec lives (ticket section, file paths) — **not** a restatement
   of it
6. The verification commands to run, with the numbers currently expected

Point at the spec; do not paste it. A handoff prompt that restates the plan drifts from
the ticket the moment the ticket is edited.
