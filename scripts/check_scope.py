#!/usr/bin/env python3
"""Workflow-gate brain (MUS-64): classify PRs and enforce the skeleton -> component flow.

Linear owns the plan (self-contained sub-issues per component); the repo owns the
proof. A skeleton PR plants red tests per component — BE modules behind
``@unittest.expectedFailure``, FE node:test files with todo tests — and each
component PR must take exactly its own artifacts to zero red while every other
test file's red count stays frozen. Landing requires the feature to carry no more
red per file than master already had. All verdict logic lives here, in tested
Python — the workflow YAML only sequences calls.

Modes::

    python scripts/check_scope.py --classify   # class/feature/component
    python scripts/check_scope.py --gate       # full per-class enforcement (default)

Base/head branch names come from --base/--head or GITHUB_BASE_REF/GITHUB_HEAD_REF.
Local self-check before pushing a component PR::

    python scripts/check_scope.py --base origin/feat/<x> --component <component>

Test artifacts are addressed by convention and looked up at the BASE ref (a PR
cannot self-create its own artifact): BE ``project/app/tests_<fslug>_<component>.py``
and FE ``frontend/tests/<fslug>_<component>.test.ts``, where ``<fslug>`` is the
feature name lowercased with dashes mapped to underscores.

Exit codes: 0 pass, 1 gate/classification failure, 2 usage error. Failure
messages name the offending file and the rule broken, so a Claude Code session
can act on them mid-loop.
"""

import argparse
import ast
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from fnmatch import fnmatchcase

DEFAULT_BRANCH = "master"
TEST_MODULE_GLOB = "project/app/tests*.py"
FE_TEST_GLOB = "frontend/tests/*.test.ts"

# The enforcement layer must be outside the reach of the thing it enforces:
# every feat/* -flow PR fails if its diff touches one of these. Changes to them
# go through meta/** PRs to master only. Hardcoded here, never configurable.
PROTECTED_PATHS = (
    ".github/workflows/**",
    "scripts/check_scope.py",
    "scripts/red_proof.py",
    "scripts/tests/**",
    "CLAUDE.md",
    "docs/ci.md",
    "docs/rulesets/**",
    # The landing gate checks the rules-eval baseline; a component PR that could
    # edit the golden set or baseline could quietly weaken that gate.
    "evals/golden/**",
    "evals/baselines/**",
    "evals/run_rules_eval.py",
)

# Component slugs name test artifacts, so they are restricted to characters that
# are valid in a Python module name (and in a filename on every platform).
COMPONENT_SLUG_RE = re.compile(r"[a-z0-9_]+")


class GateError(Exception):
    """A verdict-level failure with a message meant to be read mid-loop."""


@dataclass
class Classification:
    pr_class: str  # meta | skeleton | component | landing | normal
    feature: str = ""
    component: str = ""


# ---------------------------------------------------------------------------
# protected paths
# ---------------------------------------------------------------------------


def is_protected(path):
    for pattern in PROTECTED_PATHS:
        if pattern.endswith("/**"):
            if path.startswith(pattern[:-2]):
                return True
        elif path == pattern:
            return True
    return False


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def _branch_name(ref):
    for prefix in ("refs/heads/", "origin/"):
        if ref.startswith(prefix):
            ref = ref[len(prefix) :]
    return ref


def classify(base, head):
    """Map every (base, head) shape to an explicit class — or raise GateError."""
    base, head = _branch_name(base), _branch_name(head)
    if base == DEFAULT_BRANCH:
        if head.startswith("meta/"):
            return Classification("meta")
        if head.startswith("feat/"):
            rest = head[len("feat/") :]
            if not rest or "--" in rest or "/" in rest:
                raise GateError(
                    f"branch does not follow workflow naming: head '{head}' targets "
                    f"{DEFAULT_BRANCH}, but only a feature integration branch named "
                    "'feat/<feature>' may land there"
                )
            return Classification("landing", feature=rest)
        return Classification("normal")
    if base.startswith("feat/"):
        rest = base[len("feat/") :]
        if "--" in rest or "/" in rest:
            raise GateError(
                f"stacked PR rejected: base '{base}' is itself a workflow branch; "
                "skeleton/component PRs must target the feature integration branch "
                "'feat/<feature>' directly"
            )
        feature = rest
        prefix = f"{base}--"
        segment = head[len(prefix) :] if head.startswith(prefix) else ""
        if not segment or "/" in segment:
            raise GateError(
                f"branch does not follow workflow naming: head '{head}' under base '{base}' "
                f"must be '{prefix}skeleton' or '{prefix}<component>' where <component> "
                "matches [a-z0-9_]+"
            )
        if segment == "skeleton":
            return Classification("skeleton", feature=feature)
        if not COMPONENT_SLUG_RE.fullmatch(segment):
            raise GateError(
                f"branch does not follow workflow naming: component slug '{segment}' in "
                f"head '{head}' must match [a-z0-9_]+ (lowercase letters, digits, "
                "underscores — the slug names the component's test artifacts)"
            )
        return Classification("component", feature=feature, component=segment)
    return Classification("normal")


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------


def _git(*args, cwd="."):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _ref_exists(ref, cwd="."):
    return _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=cwd).returncode == 0


def resolve_ref(name, cwd="."):
    """Branch name -> git ref, preferring the origin-tracking ref when present."""
    if name.startswith(("origin/", "refs/")):
        if _ref_exists(name, cwd):
            return name
        raise GateError(f"cannot resolve git ref '{name}'")
    for candidate in (f"origin/{name}", name):
        if _ref_exists(candidate, cwd):
            return candidate
    raise GateError(f"cannot resolve git ref for branch '{name}' (is it fetched?)")


def changed_files(base_ref, head_ref, cwd="."):
    proc = _git("diff", "--name-only", f"{base_ref}...{head_ref}", cwd=cwd)
    if proc.returncode != 0:
        raise GateError(f"git diff {base_ref}...{head_ref} failed: {proc.stderr.strip()}")
    return [line for line in proc.stdout.splitlines() if line.strip()]


def file_at_ref(ref, path, cwd="."):
    proc = _git("show", f"{ref}:{path}", cwd=cwd)
    return proc.stdout if proc.returncode == 0 else None


def tracked_matching(ref, pattern, cwd="."):
    proc = _git("ls-tree", "-r", "--name-only", ref, cwd=cwd)
    if proc.returncode != 0:
        raise GateError(f"git ls-tree {ref} failed: {proc.stderr.strip()}")
    return {p for p in proc.stdout.splitlines() if fnmatchcase(p, pattern)}


# ---------------------------------------------------------------------------
# BE marker accounting (AST — immune to comments and strings)
# ---------------------------------------------------------------------------


def _is_marker(decorator):
    return (isinstance(decorator, ast.Name) and decorator.id == "expectedFailure") or (
        isinstance(decorator, ast.Attribute) and decorator.attr == "expectedFailure"
    )


def count_markers_in_source(source):
    tree = ast.parse(source)
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            count += sum(1 for decorator in node.decorator_list if _is_marker(decorator))
    return count


def count_markers_at(ref, path, cwd="."):
    source = file_at_ref(ref, path, cwd)
    if source is None:
        return 0
    try:
        return count_markers_in_source(source)
    except SyntaxError as exc:
        raise GateError(
            f"cannot parse {path} at {ref} for marker accounting: {exc} (rule: marker-accounting)"
        ) from exc


# ---------------------------------------------------------------------------
# FE todo accounting (textual — must work on `git show` blobs, so no JS parser;
# deliberately over-approximate: a todo-looking token in a comment counts and
# fails loudly, because undercounting is the dangerous direction)
# ---------------------------------------------------------------------------

# `todo:` option keys whose value is not the literal `false`, plus `.todo(`
# call sites (test.todo / it.todo / describe.todo).
_FE_TODO_OPTION_RE = re.compile(r"\btodo\s*:\s*([^\s,})]*)")
_FE_TODO_CALL_RE = re.compile(r"\.todo\s*\(")


def count_fe_todos_in_source(source):
    count = sum(1 for m in _FE_TODO_OPTION_RE.finditer(source) if m.group(1) != "false")
    return count + len(_FE_TODO_CALL_RE.findall(source))


def count_fe_todos_at(ref, path, cwd="."):
    source = file_at_ref(ref, path, cwd)
    return 0 if source is None else count_fe_todos_in_source(source)


# One entry per test stack: (tracked-file glob, per-file counter, red-unit name).
_STACKS = (
    (TEST_MODULE_GLOB, count_markers_at, "expectedFailure marker"),
    (FE_TEST_GLOB, count_fe_todos_at, "FE todo"),
)


# ---------------------------------------------------------------------------
# test-artifact addressing
# ---------------------------------------------------------------------------


def _feature_slug(feature):
    slug = feature.lower().replace("-", "_")
    if not COMPONENT_SLUG_RE.fullmatch(slug):
        raise GateError(
            f"feature '{feature}' cannot name test artifacts: '{slug}' must match "
            "[a-z0-9_]+ after lowercasing and dash->underscore mapping "
            "(rule: feature-slug)"
        )
    return slug


def _artifact_paths(feature, component):
    stem = f"{_feature_slug(feature)}_{component}"
    return f"project/app/tests_{stem}.py", f"frontend/tests/{stem}.test.ts"


# ---------------------------------------------------------------------------
# per-class gates
# ---------------------------------------------------------------------------


def _protected_failures(diff):
    return [
        f"protected path modified: {p} (rule: protected-paths; gate infrastructure changes "
        f"go through a meta/** PR to {DEFAULT_BRANCH})"
        for p in diff
        if is_protected(p)
    ]


def _count_deltas(pattern, counter, exclude, base_ref, head_ref, cwd, failures):
    """Yield (path, base_count, head_count) over base∪head files matching pattern."""
    sweep = tracked_matching(base_ref, pattern, cwd) | tracked_matching(head_ref, pattern, cwd)
    for path in sorted(sweep - exclude):
        try:
            yield path, counter(base_ref, path, cwd), counter(head_ref, path, cwd)
        except GateError as exc:
            failures.append(str(exc))


def _red_total(ref, cwd):
    total = 0
    for pattern, counter, _ in _STACKS:
        for path in tracked_matching(ref, pattern, cwd):
            total += counter(ref, path, cwd)
    return total


def _gate_skeleton(cls, base_ref, head_ref, diff, cwd):
    failures = _protected_failures(diff)
    try:
        base_total = _red_total(base_ref, cwd)
        head_total = _red_total(head_ref, cwd)
    except GateError as exc:
        failures.append(str(exc))
        return failures, {}
    # Delta-based, never absolute: master carries a permanent, deliberate
    # expectedFailure baseline (tests_redteam.py), so "some markers exist" is
    # meaningless — what proves a skeleton is that it CHANGES the red total.
    if base_total == head_total:
        failures.append(
            f"skeleton PR changes no red tests: expectedFailure markers + FE todos total "
            f"{head_total} at both base and head — a skeleton must add (or remove) at least "
            "one red test; marker-neutral fixes ride a component PR instead "
            "(rule: skeleton-delta)"
        )
    return failures, {}


def _gate_component(cls, base_ref, head_ref, diff, cwd):
    failures = _protected_failures(diff)
    try:
        be_path, fe_path = _artifact_paths(cls.feature, cls.component)
    except GateError as exc:
        failures.append(str(exc))
        return failures, {}
    # Artifact lookup is at the BASE ref, never head: a component PR cannot
    # self-create the artifact it is then measured against.
    be_at_base = file_at_ref(base_ref, be_path, cwd) is not None
    fe_at_base = file_at_ref(base_ref, fe_path, cwd) is not None
    if not (be_at_base or fe_at_base):
        failures.append(
            f"no test artifact for component '{cls.component}': neither {be_path} nor "
            f"{fe_path} exists at the base ref — the skeleton PR plants a component's "
            "red tests before its component PR runs (rule: artifact-exists)"
        )
        return failures, {}
    own = ({be_path} if be_at_base else set()) | ({fe_path} if fe_at_base else set())
    for path in sorted(own):
        if file_at_ref(head_ref, path, cwd) is None:
            failures.append(
                f"test artifact {path} is missing at the PR head (renamed or deleted) — a "
                "component PR must keep its artifact and take its red count to zero "
                "(rule: artifact-current)"
            )
    if be_at_base:
        try:
            remaining = count_markers_at(head_ref, be_path, cwd)
        except GateError as exc:
            failures.append(str(exc))
        else:
            if remaining:
                failures.append(
                    f"{remaining} expectedFailure marker(s) remaining in {be_path} — a "
                    "component PR must take its own markers to zero (rule: markers-zero)"
                )
    if fe_at_base:
        remaining = count_fe_todos_at(head_ref, fe_path, cwd)
        if remaining:
            failures.append(
                f"{remaining} FE todo(s) remaining in {fe_path} — a component PR must take "
                "its own todo tests to zero (rule: todos-zero)"
            )
    # Sibling sweep over BOTH stacks: every other test file's red count is
    # frozen. The own artifact is excluded only if it existed at base — a file
    # the PR created at the artifact path is swept like any sibling, closing
    # the self-creation leak.
    for pattern, counter, what in _STACKS:
        for path, base_count, head_count in _count_deltas(
            pattern, counter, own, base_ref, head_ref, cwd, failures
        ):
            if base_count != head_count:
                failures.append(
                    f"{what} count changed in sibling test file {path} "
                    f"({base_count} -> {head_count}) — a component PR may not change red "
                    "counts outside its own artifacts (rule: sibling-frozen)"
                )
    outputs = {}
    if not failures and be_at_base:
        outputs["test_targets"] = be_path[:-3].replace("/", ".")
    return failures, outputs


def _gate_landing(cls, base_ref, head_ref, diff, cwd):
    failures = _protected_failures(diff)
    # Per-file delta rule (head <= base), never absolute zero: tolerates
    # master's permanent redteam xfail baseline while still guaranteeing the
    # feature lands no new red anywhere.
    for pattern, counter, what in _STACKS:
        for path, base_count, head_count in _count_deltas(
            pattern, counter, set(), base_ref, head_ref, cwd, failures
        ):
            if head_count > base_count:
                failures.append(
                    f"{what} count increased in {path} ({base_count} -> {head_count}) — a "
                    f"feature may not land carrying more red than {DEFAULT_BRANCH} already "
                    "has; finish (or re-skeleton) the component first (rule: landing-delta)"
                )
    return failures, {}


_GATES = {
    "skeleton": _gate_skeleton,
    "component": _gate_component,
    "landing": _gate_landing,
}


def run_gate(cls, base_ref, head_ref, cwd="."):
    """-> (failures, outputs, note). Empty failures means the gate passes."""
    if cls.pr_class in ("meta", "normal"):
        return (
            [],
            {},
            f"class '{cls.pr_class}' is exempt from workflow gating; normal CI still applies",
        )
    diff = changed_files(base_ref, head_ref, cwd)
    if not diff:
        return [], {}, "empty diff — nothing to enforce"
    failures, outputs = _GATES[cls.pr_class](cls, base_ref, head_ref, diff, cwd)
    return failures, outputs, None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def write_github_output(mapping):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in mapping.items():
            fh.write(f"{key}={value}\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--classify", action="store_true", help="print/emit class, feature, component"
    )
    mode.add_argument("--gate", action="store_true", help="full per-class enforcement (default)")
    parser.add_argument(
        "--base",
        default=os.environ.get("GITHUB_BASE_REF", ""),
        help="base branch name (default: $GITHUB_BASE_REF)",
    )
    parser.add_argument(
        "--head",
        default=os.environ.get("GITHUB_HEAD_REF", ""),
        help="head branch name (default: $GITHUB_HEAD_REF)",
    )
    parser.add_argument("--base-ref", help="explicit git ref for the base (default: origin/<base>)")
    parser.add_argument("--head-ref", help="explicit git ref for the head (default: HEAD in CI)")
    parser.add_argument(
        "--component", help="local self-check: gate HEAD as a component PR for this component"
    )
    args = parser.parse_args(argv)

    base, head = args.base, args.head
    if not base:
        print("error: --base (or GITHUB_BASE_REF) is required")
        return 2
    if not head and args.component:
        head = f"{_branch_name(base)}--{args.component}"
    if not head:
        print("error: --head (or GITHUB_HEAD_REF) is required")
        return 2

    try:
        cls = classify(base, head)
    except GateError as exc:
        print(f"classification error: {exc}")
        return 1

    ids = {"class": cls.pr_class, "feature": cls.feature, "component": cls.component}
    if args.classify:
        write_github_output(ids)
        print(f"class={cls.pr_class} feature={cls.feature} component={cls.component}")
        return 0

    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    try:
        base_ref = args.base_ref or resolve_ref(
            base if base.startswith("origin/") else _branch_name(base)
        )
        if args.head_ref:
            head_ref = args.head_ref
        elif in_actions or args.component:
            head_ref = "HEAD"
        else:
            head_ref = resolve_ref(_branch_name(head))
        failures, outputs, note = run_gate(cls, base_ref, head_ref)
    except GateError as exc:
        failures, outputs, note = [str(exc)], {}, None

    label = f"class '{cls.pr_class}'" + (f", feature '{cls.feature}'" if cls.feature else "")
    if cls.component:
        label += f", component '{cls.component}'"
    if failures:
        print(f"workflow-gate: FAIL — {label}")
        for index, failure in enumerate(failures, 1):
            print(f"  {index}. {failure}")
        return 1
    write_github_output({**ids, **outputs})
    if note:
        print(note)
    if outputs.get("test_targets"):
        print(f"scoped test targets: {outputs['test_targets']}")
    print(f"workflow-gate: PASS — {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
