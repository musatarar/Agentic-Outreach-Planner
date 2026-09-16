# Worked example: "make leads and events multitenant"

The real plan filed for this ask is issue #123 in this repo (now closed). It is a good
plan by most standards, and exactly the failure this skill prevents. Contrast the two.

## What was filed

Two new models, four migrations (two on hot tables), a new `services/tenancy.py` with four
functions, a new permission class applied to every data view, a change to the sign-in
view's user creation (an auth change, human-gated), four management commands, changes to
the demo-data script, Django admin registration for two models and two admin list edits,
frontend type, hook and nav changes plus a bundle rebuild, a README section, two CLAUDE.md
edits, a test-utility module, edits to every lead factory and every `plan_outreach` call,
and a four-item follow-ups list. Around thirty files. Every item is defensible. Nobody
asked for most of them.

## The same ask through this skill

**Ask.** Leads and events belong to a workspace; a signed-in user sees only their own.

**Done when.**
1. A user in workspace A cannot list, compose, or review a lead or draft in workspace B.
2. The planner run for workspace A never reads workspace B's leads.

**Change.**
- `models/lead.py` — `Lead.tenant` nullable FK to a new `Tenant(slug, name)`; the user's
  side is a `Tenant.members` M2M to the Django user, so no profile model is needed —
  serves 1, 2
- one migration for the `CreateModel` and the `AddField` — flag: hot-table, human review
- `views/leads.py`, `views/review.py`, `views/outreach.py` — filter by
  the caller's tenant (`Tenant.objects.filter(members=request.user).first()`); no tenant returns an empty list — serves 1
- `services/outreach.py` `plan_outreach(tenant, ...)` — one extra filter per query — serves 2
- `scripts/populate_demo_data.py` — seed one tenant and attach the allowlisted users — keeps
  the quickstart working, which is a done-when in disguise

**Tests.** `tests_tenancy.py`: cross-workspace list/compose/review is 404 or empty; the
planner for A never touches B (query the perf pin, do not bump it). Mechanical edits: lead
factories gain a tenant argument.

**Human review.** One migration on `lead`.

**Deferred, to confirm with the user.**
- `Event.tenant` denormalised — tempting for a join-free query; events are only reached
  through their lead today — drop
- `TenantMembership` as its own model with a `created_at` — tempting for auditing; a plain
  M2M does the same job now — drop
- management commands `create_tenant`, `add_tenant_member`, `backfill_tenant` — tempting
  for ops; the shell and the demo script cover an MVP — later issue if ops need it
- `ingest_data --tenant` required option — tempting for safety; one seeded tenant makes it
  a default — drop
- admin registration — nice — later
- workspace name in the nav — nice — later, separate frontend issue
- README and CLAUDE.md sections — one CLAUDE.md line ("lead, event and outreach queries are
  tenant-scoped; never add an unscoped one") is required; the rest is later
- NOT NULL follow-up migration — required eventually, not for done-when — later

Roughly eight files instead of thirty, one migration instead of four, no auth change, and
the same two behaviours proven. The deferred list is longer than the plan, which is the
point: the user decides what comes back in, and each item that does is a deliberate cost.
