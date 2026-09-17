---
name: plan-feature
description: Plan a feature, behaviour change, or refactor as the smallest diff that satisfies the ask, list every inferred extra separately for the user to accept or defer, and file the confirmed plan as a GitHub issue. Use this whenever the user asks to plan, scope, spec, design, "write up", "think through", "figure out how to add", or "open an issue for" any change to this app, even if they never say "plan" and even if the change sounds small. Also use it, without being asked, the moment you discover mid-implementation that you need a prerequisite change (a new column, a base PR, a "first I have to") — that sub-change is an ask of its own and gets the same minimal plan and the same confirmation. Do not use for executing a change that already went through this skill, or for questions about how existing code works.
---

# Plan a feature at MVP scope

This is an MVP. The failure this skill exists to prevent: an ask like "make leads
multitenant" becoming a plan with four migrations, four management commands, admin
registration, a frontend change, README and CLAUDE.md sections, and a follow-ups list,
because each piece seemed to belong. Each piece is reasonable alone. Together they turn a
week of value into a month of review. `references/example.md` shows that real case and the
minimal plan that should have replaced it.

The rule: the plan contains what the ask *requires*, never what it merely *suggests*.
Everything suggested is a question for the user, not a line item.

## 1. Pin the ask before reading code

Write two things down first:

- **Ask**: the user's request in one sentence, in their words. Do not expand it.
- **Done when**: one to three observable checks a reviewer could run. Each is a behaviour
  ("a reviewer in workspace A cannot list workspace B's leads"), not an artefact ("a
  Tenant model exists").

Every done-when comes from the ask. If a third check appears that the user never mentioned
("only pending drafts can be snoozed"), it is an extra wearing a done-when's clothes: move it
to section 4. If "done when" cannot be written without guessing, ask the user one question
now, before investigating. A plan built on a guess is wrong in proportion to its
thoroughness.

## 2. Find the narrowest seam

Read the registries first (CLAUDE.md, "Where things are"), then only the code the ask
touches. Look for something to extend rather than something to add: an existing field,
service function, view, throttle scope, or test module. The best plan usually adds a few
lines to existing structure.

Keep two lists while reading: what the change **requires**, and what you **noticed**.
Noticed things go to section 4. They never go to section 3, however good they are.

A prerequisite you discover mid-implementation ("the engine needs a real owner on the
lead first") is an ask in its own right and goes through sections 1 to 6 before you build
it. The pull to over-build is strongest here, because nobody asked for the sub-change and
so nobody is holding its scope: a one-column change quietly acquires a services module, a
command-line flag, and a test module of its own. Pin its done-when, plan the column, and
put the rest in front of the user.

## 3. The minimal change

For each "done when", the fewest edits that make it true. One line per edit, and one
line means one line: `path` — what changes — which done-when it serves. The reasoning
behind an edit goes in your chat message, not in the plan. An edit that serves no
done-when is not minimal; move it to section 4.

Write code in the plan only where the shape *is* the decision (a field type, a constraint
name, a throttle rate). Full class bodies and migration tables belong in the PR, not the
plan.

Tests: the ones that prove each done-when, plus the mechanical edits the change forces on
existing tests (name those files). Not a module per new function, and not new coverage of
code the change does not touch.

Sizing check. Any of these means look again, because the minimal change rarely needs them:

- more than one new module, model, migration, endpoint, or management command
- a new abstraction (base class, registry, config layer) with a single user
- a new environment variable, feature flag, or setting
- docs beyond the line a changed command or invariant needs
- frontend edits when the ask was backend, or the reverse
- a follow-ups list longer than the plan itself

None of these is forbidden. Each must trace to a done-when or move to section 4.

## 4. Inferred extras: confirm, never assume

Everything you noticed, wanted, or would "normally" add. One line each: what, why it was
tempting, and its cost (files touched, review flag tripped). The default for every item
is **out**.

The usual suspects: admin registration; a management command for a one-off; denormalising
a column "for later"; the NOT NULL follow-up; a UI affordance for a backend change;
renaming while you're there; the refactor the change "reveals"; extra validation; docs
that never mentioned the changed thing; a nicer error; a second endpoint "for symmetry".

Three that look like hygiene and are not:

- **Extracting a shared module.** A second caller for a private helper does not justify
  moving it into `services/`. Call what exists or copy the three lines; a shared module is
  a later issue once there is a third caller and the duplication has cost something.
- **A new flag or option** on a command, because the new column "should be settable".
  The done-when says what the column holds; the flag is a feature.
- **A test module for the helper you extracted.** If the extraction is out, so are its tests.
  Test the behaviour the ask named, in the module that already tests it.

List the extras a reasonable engineer would actually have added, roughly six at most.
The user has to read and decide on each one, so an item you would never have built
("seed a snoozed row in the demo data") is noise, not diligence. Present the list for
the user to pick from. Until the user says so, an extra is in neither the plan, nor the
issue, nor the estimate.

## 5. Human-review flags

CLAUDE.md names what always needs a human before merge (migrations, auth and throttles,
the approval gate, sanitisation and the verifier, flag default flips, provider spend,
LoginToken, `.claude/`, CI). Name every flag the minimal change trips. If a different
minimal change avoids a flag, say so: that is often the cheaper plan, and the user should
get to choose.

## 6. Confirm with the user

Show the plan in chat using the issue template below, then ask three things (use the
AskUserQuestion tool when it is available, otherwise ask in plain text):

1. Does the minimal change match what you meant? If not, fix section 1, not section 3.
2. Which extras, if any, come into this plan?
3. Which remaining extras get their own later issue, and which are simply dropped?

Wait for the answer. If there is no way to ask (an unattended run, a subagent), stop here,
present the plan, and say plainly that no issue was filed. Never file on an assumption.

## 7. File the issue

Only after confirmation:

- Search existing issues for a duplicate first (`search_issues` from the GitHub MCP tools,
  or `gh issue list --search`). Update or comment on a match instead of creating another.
- Create one issue. Title: the ask as an imperative sentence. Body: the template, with
  accepted extras folded into "Change" and the rest under "Deferred". Deferred items get
  their own issues only when the user asked for that; otherwise the list is the record.
- Use labels that already exist in the repo. Do not create labels.
- Reply with the issue URL. Do not also write a plan file into the repo: the issue is the
  record, and a plan document is itself a piece of scope.

The issue is a checklist for whoever implements it, not an essay for whoever reviews the
plan. Budget: about forty lines. A plan that runs past that is usually explaining itself;
cut the explanations first (they were already in chat), and if it is still long, the
change is too big: split it by done-when into separate issues and say which goes first.

## Issue template

```markdown
## Ask
<one sentence, the user's words>

## Done when
- <observable check>

## Change
- `path` — what — serves done-when N

## Tests
- <test that proves done-when N> (file)
- mechanical edits: <files>, no assertion changes

## Human review
<CLAUDE.md flags tripped, or "none">

## Deferred (confirmed out of scope)
- <extra> — <why tempting> — <dropped | later issue #N>

## Gates
CLAUDE.md commands; plus <any this change makes relevant, e.g. frontend rebuild>
```
