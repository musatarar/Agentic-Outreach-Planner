# ADR: Run Composer state — one active run, enforced by the database

**Status:** accepted (MUS-47)
**Context:** `docs/contracts/run-composer.md`

## The decision

A composer run is a durable, resumable object (`PlannerRun`) with exactly one instance
active at a time, and the *database* enforces the "exactly one" — not a check in a view.

## Why a run is durable at all

The composer's whole premise is that the person paying gets to stop between stages. That
makes the gaps between stages long: someone scopes a run, classifies, goes to lunch, comes
back and decides whether to buy the read. Session state cannot survive that, and neither
can a request-scoped object. The run has to be a row.

It also has to be findable without being remembered. `GET /api/runs/active/` on page mount
is what turns "I closed the tab" from data loss into a resume — which is why `active` is a
queryable property of the row rather than something the client tracks.

## Why the database enforces single-active, not the view

The obvious implementation is a read-then-write: query for an active run, 409 if one
exists, otherwise insert. It is also wrong, in the specific way this repo has already been
bitten by twice — `plan_outreach`'s `open_keys` race is documented in its own source, and
`LoginToken` consume was deliberately built as a conditional UPDATE for the same reason.
Two POSTs that interleave between the read and the insert both see an empty result and both
create a run.

So the constraint lives in the schema:

```python
active_sentinel = BooleanField(null=True, default=True)

UniqueConstraint(fields=["active_sentinel"], condition=Q(active_sentinel=True),
                 name="pr_one_active_run")
```

`NULL` is distinct from `NULL` in both SQLite and Postgres, so any number of terminal runs
coexist while at most one row can hold `True`. `create_run` inserts unconditionally and
translates the resulting `IntegrityError` into `RunConflict(active_run_id=...)`, which the
view renders as a 409 carrying the active run's id — so the frontend can offer to resume it
rather than just refusing.

A second `CheckConstraint` pins the sentinel to the status it claims to describe, so a
future edit that flips one without the other fails at the write instead of quietly
producing a run that is active-but-completed.

## Why six statuses, not four

MUS-48 proposes `draft → classified → read → generated`. That set has no terminal state,
which means the single-active slot is never released and the second run can never be
created.

`completed` and `discarded` are the two ways a run ends, and both are reachable, recorded
(actor + timestamp), and tested. `generated` deliberately stays *active*: MUS-50 requires
failed rows to remain selectable for retry, so generation cannot be the thing that closes
the run. The human closes it.

## What this does not solve

Concurrent runs. The ticket defers them on the grounds that two runs generating for the
same lead raises dedupe questions against the triage queue that are not worth solving yet,
and this design agrees by making concurrency structurally impossible rather than merely
discouraged. When it does become worth solving, the sentinel column is the thing to drop,
and `already_queued` is the mechanism that will have to grow up.

## Relationship to the agent loop's state

`AgentLeadRun` / `AgentStep` (MUS-29) solve a different problem — crash-resume *inside* a
single provider-call sequence, at per-step granularity, with a claim CAS because several
workers may race. The composer has no workers and no in-flight loop to resume: its stages
are separated by human decisions, not by network calls. A stage boundary is a status, and
that is all the durability it needs.

Deliberately not unified. The composer does not run the agent loop (a named non-goal), so
the two state machines never touch.
