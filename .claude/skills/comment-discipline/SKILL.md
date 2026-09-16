---
name: comment-discipline
description: Write one-line comments or none, never a paragraph. Use this whenever adding or editing code in this repo — Python, TypeScript, tests, scripts, settings, migrations — and specifically before writing any comment or docstring, including when the code feels subtle enough to deserve an explanation. Also use when reviewing a diff for comment bloat. The failure it prevents is the five-line block that narrates what was considered, what the code deliberately does not do, and why the obvious alternative was rejected.
---

# One line, or none

Assume the reader understands the code. A comment earns its place only when the code
cannot say the thing itself, and then it says it in one line.

## The rule

1. **Default is no comment.** Code that reads as what it does needs nothing.
2. **If it is not obvious, add one line.** "Not obvious" means a reader who knows this
   codebase would stop and ask *why is it like that* — a non-local constraint, an
   ordering requirement, a security invariant, a surprising API.
3. **Never write the second line.** If one line cannot carry it, see "When one line is
   not enough" below. Writing two is not one of the options.

## Never goes in a comment

- **Alternatives considered.** "Rather than X", "instead of Y", "we could have Z" —
  the code says what it does; what it isn't is not a fact about the code.
- **What the code does not do.** Non-goals, deferred work, "this does not handle…".
- **Design history.** "Used to be", "after the refactor", ticket narratives, dates.
- **Restating the code.** `# increment the counter` above `count += 1`.
- **Arguments.** A comment states; it does not persuade. Point at `SECURITY.md` or
  `CLAUDE.md` instead of re-arguing what they already settle.

The long version has homes: the commit message, the PR description, `SECURITY.md`, or a
GitHub issue. Those are read by people deciding; a comment is read by everyone, forever.

## When one line is not enough

That is a signal about the code, not a licence for a paragraph.

- The comment is doing a name's job → rename the variable or function, or extract the
  block into a function whose name is the sentence.
- The comment is doing a constant's job → name the literal.
- The comment is really documentation → `README.md`, `SECURITY.md`, or the docstring of
  the module, in one line.
- The reasoning is genuinely about the decision, not the code → commit message.

One carve-out: a **case table** — one line per case, no prose (`errors.py` mapping a
response shape to an exception class, `verify.py` listing the strictness levels). That is
data laid out readably, not narration. It still gets no prose paragraph above it.

## Docstrings

- Module and class: one line of purpose. Nothing about structure, history, or rationale.
- Function: only when the name and signature genuinely leave something out. Most do not.
- Test: none. Test names are behavioural sentences here; the sentence is the docstring.

## Editing existing code

Leave comments you are not touching alone — a comment sweep is its own reviewed change,
not a rider on a feature. If you rewrite the code a comment describes, the replacement
comment follows this rule. Never extend an existing block to explain your addition.

## Worked examples, from this repo

Adding the constant:

```python
# Markers in README.md between which --table writes. Same idiom
# run_copy_eval.py --table already uses.          # ← the second sentence is history
TABLE_START = "<!-- PLANNER-BENCH-TABLE -->"
```

```python
# Markers in README.md between which --table writes.
TABLE_START = "<!-- PLANNER-BENCH-TABLE -->"
```

Compressing rather than deleting — the fact survives, the essay does not:

```python
# Supersede the failed-attempt rows this run replaces (they were let
# through the open-item rule on purpose), so a lead that failed
# Monday and succeeded Tuesday does not show both. Deleted rather
# than marked: a failed attempt carries no draft, so there is nothing
# a reviewer decided about it.
```

```python
# Delete the failed-attempt rows this run replaces, so one lead shows one row.
```

Non-obvious, so it keeps its line:

```python
# `reason` quotes note snippets, so sanitize it before the trusted region.
```

Obvious, so it gets nothing:

```python
# Return the leads ordered by name       # ← delete
return leads.order_by("name")
```

## What this skill is not

It is not a ban on comments. Deleting the one line that says *why* is the same failure in
the other direction: the next reader reinvents the bug the line prevented. Keep the fact,
drop the essay.
