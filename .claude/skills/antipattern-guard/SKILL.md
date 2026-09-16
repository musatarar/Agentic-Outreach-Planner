---
name: antipattern-guard
description: Catch antipatterns in a coding request or in your own implementation plan and push back before writing them, instead of coding a literal reading of the ask. Use this whenever asked to add, change, or move code in this repo — especially when the request names a location ("add a method to services", "put this in the view", "just hardcode it for now", "make the test pass", "skip the check when testing"), when the fastest way to satisfy the words would put fixture data, test shortcuts, placeholders, or duplicated logic into production modules, or when a shorthand request seems to conflict with what the target module is for. Trigger even when the request looks simple; the failure mode this guards against is doing exactly what was said when it is not what was meant.
---

# Antipattern guard

A request is a compressed description of intent, not a spec. "Add a method into services" from
someone who knows this codebase means "add reusable business logic where business logic lives",
and satisfying the literal words with a function that returns hardcoded rows is a failure even
though it "does what was asked". This skill is about noticing that gap and naming it before any
code exists, so the user reviews a decision rather than a diff they have to throw away.

## The two-step check, before writing code

1. **Read the request through the target's role.** Every module here has a job (table below).
   Ask: does the literal ask fit that job? If not, the user almost certainly meant the version
   that does fit, and the mismatch is the thing to surface.
2. **Read your own plan for antipatterns.** Before the first edit, look at what you are about to
   write and check it against the catalog below. The catalog exists because these are the
   shortcuts that feel like progress and cost a review cycle to remove.

If either step finds something, push back (next section). If neither does, just build it; this
skill is a gate, not a tax on ordinary work.

## How to push back

Do not silently substitute your own interpretation, and do not stop with nothing delivered
unless the readings diverge so far that building the wrong one wastes the work. The default:

- **Name what you noticed** in one or two sentences: the literal reading, why it does not fit,
  and what you believe was meant.
- **Build the version that fits**, under that stated assumption, in the place it belongs.
- **Flag it at the top of your final message** so the user sees the decision first.

Ask a blocking question only when two readings lead to materially different code and no
sensible default exists. When you do ask, offer the concrete options, recommend one, and say
what you would build for each.

If the user reaffirms the literal request after you have raised the concern, that is their
decision. Say so once and build it as asked. The goal is to give them the choice, not to win.

### Example

Request: "add a method into services that returns leads for the demo"

Weak response: a function in `services/` with a list of dicts of agency names and emails.

Strong response, said before coding: "I read this as a reusable selector over Lead rows, not a
function containing demo rows. Demo data already lives in `raw_data/` and is loaded by the
ingest command, so I'm adding a query function in services that takes the filter criteria as
arguments, and leaving the data where it is. Say the word if you actually wanted a fixture."

## Where things belong in this repo

| Concern | Home | Not here |
|---|---|---|
| Reusable business logic, rules, selectors | `project/app/services/` (pure where possible, explicit args, no request or session objects) | views, serializers, model methods with side effects |
| HTTP shape: auth, pagination, throttling, status codes | `project/app/views/`, `serializers/` | services |
| Test data | factory helpers in the test module (`make_lead`, `_lead`) or `tests/fixtures/` | any production module |
| Demo or seed data | `raw_data/*.json` via the `ingest_data` management command | services, migrations, settings |
| Constants and enums | `services/actions.py`, module-level constants | inline magic strings |
| Configuration | environment variables read in `settings.py` with the `_env_*` helpers | hardcoded literals, database rows, API-editable fields |
| Provider selection and retry | `services/llm/config.py`, `errors.py` | call sites |

## Catalog

Each entry: what it looks like, why it is wrong here, what to do instead. When you find one in
your plan, say which entry it is.

### Fixture data in production code
Looks like: literal lead names, emails, dates, or event payloads inside a service, view, or
model; a function whose body is `return [...]` of sample rows; a "demo mode" branch.
Why: the module now has two jobs, and the second one silently becomes production behaviour.
Callers cannot tell the data is fake, tests pass against it, and the real path is untested.
Instead: the function takes its inputs as arguments or queries the ORM. Sample data goes in a
test factory helper or `raw_data/`.

### Test-only branches in production paths
Looks like: `if settings.TESTING`, `if "test" in sys.argv`, `if settings.DEBUG`, an env var
that skips a check, a parameter defaulting to "skip verification".
Why: the tested code is no longer the shipped code, and the skipped step is usually the one
that matters (here: the verifier, the approval gate, sanitization). CLAUDE.md lists these as
human-gated for exactly this reason.
Instead: mock the collaborator at the seam (subclass `LLMClient`, patch `timezone.now`), or
make the real path fast enough. Never let a switch disable a security invariant.

### Placeholder presented as done
Looks like: `return True  # TODO`, `pass`, a function that logs and returns `None`, a stub
adapter that returns canned text outside the gated stub provider.
Why: it reads as finished in a diff and gets built on.
Instead: build it, or say plainly which part is missing and why.

### Making the check pass instead of fixing the cause
Looks like: editing or deleting an existing test, raising a query budget in
`tests_planner_perf.py`, regenerating an eval baseline, adding `# noqa`/`# type: ignore`,
loosening an assertion, catching the exception and continuing.
Why: every one of those is a signal being muted. Budgets and baselines change only by an
explicit human decision.
Instead: find why the check fails. If the check itself is wrong, say so and stop; that
decision belongs to a human.

### Duplicated helper
Looks like: a new `_normalize`, `_days_since`, `_hash_key` when `normalize_copy`, `_days_since`,
or `dedupe_key` already exist; a second copy of a prompt-building function.
Why: two implementations drift, and the security-relevant ones (sanitization, dedupe identity)
must not.
Instead: grep for the behaviour before writing it. Reuse, or extend the existing one.

### Business logic in the wrong layer
Looks like: a rule decision inside a view or serializer; ORM writes in a model method; a
service that takes `request`; a Django signal doing a write.
Why: the rules engine and planner are testable without HTTP because nothing about HTTP leaks
into them. Signals hide writes from the explicit service functions that own them.
Instead: decide in services, expose through views, write through explicit service functions
inside one `transaction.atomic` block.

### Read-then-check where a race is possible
Looks like: `if not Token.objects.filter(used=True).exists(): token.used = True; token.save()`.
Why: two requests both pass the check. This app already has the correct patterns (conditional
UPDATE on login-token redemption, partial unique constraint on the dismiss revoke).
Instead: copy one of those.

### ORM calls in the async provider phase
Looks like: `lead.events.all()` or `.save()` inside the coroutine that calls the provider, or a
module-level Django import in `services/llm/`.
Why: `SynchronousOnlyOperation` at runtime with no static warning, and the LLM layer must stay
importable without Django.
Instead: gather everything the coroutine needs before entering the event loop; keep Django
imports function-local in `runtime.py`.

### Schema shortcuts
Looks like: editing a committed migration, a `RunPython` data step, a non-nullable column with
no default, a plain index add on `outreachaction`, `lead`, or `event`.
Why: the first breaks every environment that already applied it; the others stall Postgres or
require a rewrite. All migrations are human-reviewed.
Instead: additive follow-up migration; nullable-or-default first, backfill via management
command, constrain after; concurrent index with `atomic = False`.

### Configuration in the wrong place
Looks like: a literal timeout or model name at the call site; a default restated in
docker-compose; a new env var without a `.env.example` entry; a setting stored in the database
or exposed through the API.
Why: settings.py owns every default and the env matrix is closed by design, so drift is caught
at boot rather than in production.
Instead: `_env_int` and friends in settings.py, one `.env.example` line, one compose
passthrough, and a check in `checks.py` if a bad value should fail boot.

### Hand-edited generated files
Looks like: a change under `project/app/static/frontend/` without a matching `frontend/src`
change; a lockfile edited by hand.
Why: CI diffs the rebuilt bundle and fails on a mismatch; the next build erases the edit.
Instead: change the source and run the build.

### Swallowed errors and leaked secrets
Looks like: `except Exception: pass`, retrying on every error class, logging a prompt or
completion, printing a token.
Why: retryability is a property of the error class, retries are spend, and raw prompts carry
untrusted lead text.
Instead: catch the specific class, let `errors.py` decide retryability, log identifiers only.

## What this skill is not

It is not a reason to refuse ordinary work, add caveats to every reply, or relitigate a choice
the user already made. A clean request that fits its target gets built without commentary.
Reach for pushback when you would otherwise be writing something from the catalog, and keep it
to the sentences it needs.
