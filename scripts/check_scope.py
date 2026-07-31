#!/usr/bin/env python3
"""Workflow-gate brain (MUS-53): classify PRs and enforce the contract workflow.

Classifies every PR by its base/head branch names and enforces the
contract -> skeleton -> mini-PR rules mechanically: file-map scope, shared-file
and protected-path rejection, AST-based expectedFailure marker accounting, and
contract lint. All verdict logic lives here, in tested Python — the workflow
YAML only sequences calls.

Modes::

    python scripts/check_scope.py --classify                  # class/feature/component
    python scripts/check_scope.py --gate                      # full per-class enforcement
    python scripts/check_scope.py --validate-contract PATH    # contract lint

Base/head branch names come from --base/--head or GITHUB_BASE_REF/GITHUB_HEAD_REF.
Local self-check before pushing a mini PR::

    python scripts/check_scope.py --base origin/feat/x --component scope_engine

Exit codes: 0 pass, 1 gate/lint/classification failure, 2 usage error. Failure
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
from pathlib import Path

import yaml

DEFAULT_BRANCH = "master"
CONTRACTS_DIR = "docs/contracts"
TEST_MODULE_GLOB = "project/app/tests*.py"

# The enforcement layer must be outside the reach of the thing it enforces: a
# contract's file map may not claim any of these, and every feat/* -flow PR
# fails if its diff touches one. Changes to them go through meta/** PRs to
# master only. Hardcoded here, never read from a contract.
PROTECTED_PATHS = (
    ".github/workflows/**",
    "scripts/check_scope.py",
    "scripts/red_proof.py",
    "scripts/tests/**",
    "CLAUDE.md",
    "docs/contracts/TEMPLATE.md",
    "docs/ci.md",
    "docs/rulesets/**",
    # The landing gate checks the rules-eval baseline; a mini PR that could
    # edit the golden set or baseline could quietly weaken that gate.
    "evals/golden/**",
    "evals/baselines/**",
    "evals/run_rules_eval.py",
)

# Contract lint checks these section headings exist (presence, not quality —
# prose being *meaningful* is review-only and documented as such).
REQUIRED_SECTIONS = ("interfaces", "data shapes", "error contract", "file map")


class GateError(Exception):
    """A verdict-level failure with a message meant to be read mid-loop."""


class FileMapError(GateError):
    """The contract's file-map block is missing or malformed."""


@dataclass
class FileMap:
    components: dict
    shared: list
    feature: str | None = None


@dataclass
class Classification:
    pr_class: str  # meta | skeleton | mini | contract | landing | normal
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
# file map parsing + contract lint
# ---------------------------------------------------------------------------


def extract_file_map_block(text):
    blocks = re.findall(r"```ya?ml[^\n]*\n(.*?)\n```", text, flags=re.DOTALL)
    maps = [b for b in blocks if b.lstrip().startswith("# file-map")]
    if not maps:
        raise FileMapError(
            "no fenced ```yaml block starting with '# file-map' found in the contract"
        )
    if len(maps) > 1:
        raise FileMapError("multiple '# file-map' blocks found; a contract must have exactly one")
    return maps[0]


def _as_path_list(value, what):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise FileMapError(f"{what} must be a path or a list of paths, got: {value!r}")


def parse_file_map(text):
    block = extract_file_map_block(text)
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1} of the file-map block" if mark else ""
        raise FileMapError(f"file-map YAML failed to parse{where}: {exc}") from exc
    if not isinstance(data, dict):
        raise FileMapError("file-map must be a YAML mapping")
    raw_components = data.get("components")
    if not isinstance(raw_components, dict) or not raw_components:
        raise FileMapError("file-map must declare a non-empty 'components' mapping")
    components = {}
    for name, spec in raw_components.items():
        if not isinstance(spec, dict):
            raise FileMapError(f"component '{name}' must be a mapping with 'files' and 'tests'")
        components[name] = {
            "files": _as_path_list(spec.get("files"), f"component '{name}' files"),
            "tests": _as_path_list(spec.get("tests"), f"component '{name}' tests"),
        }
    shared = _as_path_list(data.get("shared"), "'shared'")
    feature = data.get("feature")
    return FileMap(components=components, shared=shared, feature=feature)


def validate_contract_text(text):
    """Contract lint. Returns a list of error strings; empty means clean."""
    errors = []
    headings = [
        line.lstrip("#").strip().lower() for line in text.splitlines() if line.startswith("#")
    ]
    for section in REQUIRED_SECTIONS:
        if not any(h.startswith(section) for h in headings):
            errors.append(f"missing required section: '{section}' (add a '## ...' heading for it)")

    try:
        fmap = parse_file_map(text)
    except FileMapError as exc:
        errors.append(str(exc))
        return errors

    owner = {}
    for name, spec in fmap.components.items():
        if not spec["tests"]:
            errors.append(f"component '{name}' must declare at least one test module")
        for test_path in spec["tests"]:
            if not fnmatchcase(test_path, TEST_MODULE_GLOB):
                errors.append(
                    f"component '{name}' tests must be Django test modules matching "
                    f"{TEST_MODULE_GLOB}, got: {test_path}"
                )
        for path in spec["files"] + spec["tests"]:
            if path in owner and owner[path] != name:
                errors.append(
                    f"{path} must belong to exactly one component "
                    f"(claimed by '{owner[path]}' and '{name}')"
                )
            owner[path] = name
    for path in fmap.shared:
        if path in owner:
            errors.append(
                f"shared file {path} is also owned by component '{owner[path]}' "
                "(shared and owned must not overlap)"
            )
    if "assembly" not in fmap.components:
        errors.append("an 'assembly' component is required (integration wiring must be owned)")
    for name, spec in fmap.components.items():
        for path in spec["files"] + spec["tests"]:
            if is_protected(path):
                errors.append(f"protected path claimed by component '{name}': {path}")
    for path in fmap.shared:
        if is_protected(path):
            errors.append(f"protected path in shared: {path}")
    return errors


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
                "skeleton/mini/contract PRs must target the feature integration branch "
                "'feat/<feature>' directly"
            )
        feature = rest
        prefix = f"{base}--"
        segment = head[len(prefix) :] if head.startswith(prefix) else ""
        if not segment or "/" in segment:
            raise GateError(
                f"branch does not follow workflow naming: head '{head}' under base '{base}' "
                f"must be '{prefix}skeleton', '{prefix}contract*', or '{prefix}<component>' "
                "where <component> is a file-map component key"
            )
        if segment == "skeleton":
            return Classification("skeleton", feature=feature)
        if segment.startswith("contract"):
            return Classification("contract", feature=feature)
        return Classification("mini", feature=feature, component=segment)
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


def tracked_test_modules(ref, cwd="."):
    proc = _git("ls-tree", "-r", "--name-only", ref, cwd=cwd)
    if proc.returncode != 0:
        raise GateError(f"git ls-tree {ref} failed: {proc.stderr.strip()}")
    return {p for p in proc.stdout.splitlines() if fnmatchcase(p, TEST_MODULE_GLOB)}


# ---------------------------------------------------------------------------
# marker accounting (AST — immune to comments and strings)
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
# per-class gates
# ---------------------------------------------------------------------------


def _contract_path(feature):
    return f"{CONTRACTS_DIR}/{feature}.md"


def _load_contract(feature, head_ref, cwd):
    path = _contract_path(feature)
    text = file_at_ref(head_ref, path, cwd)
    if text is None:
        return None, [
            f"contract missing: {path} does not exist at the PR head (rule: contract-required)"
        ]
    errors = validate_contract_text(text)
    if errors:
        return None, [f"contract lint [{path}]: {e} (rule: contract-lint)" for e in errors]
    return parse_file_map(text), []


def _protected_failures(diff):
    return [
        f"protected path modified: {p} (rule: protected-paths; gate infrastructure changes "
        f"go through a meta/** PR to {DEFAULT_BRANCH})"
        for p in diff
        if is_protected(p)
    ]


def _sibling_sweep(own_tests, base_ref, head_ref, cwd, failures):
    sweep = tracked_test_modules(base_ref, cwd) | tracked_test_modules(head_ref, cwd)
    for module in sorted(sweep - set(own_tests)):
        try:
            base_count = count_markers_at(base_ref, module, cwd)
            head_count = count_markers_at(head_ref, module, cwd)
        except GateError as exc:
            failures.append(str(exc))
            continue
        if base_count != head_count:
            failures.append(
                f"marker count changed in sibling module {module} ({base_count} -> {head_count}) "
                "— a mini PR may not remove another component's markers or add markers anywhere "
                "(rule: marker-accounting)"
            )


def _gate_skeleton(cls, base_ref, head_ref, diff, cwd):
    _, failures = _load_contract(cls.feature, head_ref, cwd)
    failures += _protected_failures(diff)
    return failures, {}


def _gate_mini(cls, base_ref, head_ref, diff, cwd):
    fmap, failures = _load_contract(cls.feature, head_ref, cwd)
    if failures:
        return failures, {}
    if cls.component not in fmap.components:
        return [
            f"component '{cls.component}' is not declared in the contract file map "
            f"({_contract_path(cls.feature)}) (rule: component-declared)"
        ], {}
    spec = fmap.components[cls.component]
    own = set(spec["files"]) | set(spec["tests"])
    shared = set(fmap.shared)
    for path in diff:
        if is_protected(path):
            failures += _protected_failures([path])
        elif path in shared:
            failures.append(
                f"shared file touched: {path} — shared files are written in the skeleton and "
                "owned by no component (rule: no-shared)"
            )
        elif path not in own:
            failures.append(
                f"diff outside component file map: {path} (component '{cls.component}' owns "
                "only its declared files and tests) (rule: mini-scope)"
            )
    for test_path in spec["tests"]:
        if file_at_ref(head_ref, test_path, cwd) is None:
            failures.append(
                f"file map out of date: test module {test_path} is missing at the PR head "
                "(renamed or deleted); open a contract-change PR to update the map "
                "(rule: file-map-current)"
            )
            continue
        try:
            remaining = count_markers_at(head_ref, test_path, cwd)
        except GateError as exc:
            failures.append(str(exc))
            continue
        if remaining:
            failures.append(
                f"{remaining} expectedFailure marker(s) remaining in {test_path} — a mini PR "
                "must take its component's markers to zero (rule: markers-zero)"
            )
    _sibling_sweep(spec["tests"], base_ref, head_ref, cwd, failures)
    outputs = {}
    if not failures:
        outputs["test_targets"] = " ".join(t[:-3].replace("/", ".") for t in spec["tests"])
    return failures, outputs


def _gate_contract(cls, base_ref, head_ref, diff, cwd):
    fmap, failures = _load_contract(cls.feature, head_ref, cwd)
    if failures:
        return failures, {}
    failures = _protected_failures(diff)
    contract_path = _contract_path(cls.feature)
    map_tests = {t for spec in fmap.components.values() for t in spec["tests"]}
    for path in diff:
        if is_protected(path):
            continue  # already reported above
        if path != contract_path and path not in map_tests:
            failures.append(
                f"contract-change PR may only touch {contract_path} and the map's test "
                f"modules; found: {path} (rule: contract-change-shape)"
            )
    contract_in_diff = contract_path in diff
    sweep = tracked_test_modules(base_ref, cwd) | tracked_test_modules(head_ref, cwd)
    for module in sorted(sweep):
        try:
            base_count = count_markers_at(base_ref, module, cwd)
            head_count = count_markers_at(head_ref, module, cwd)
        except GateError as exc:
            failures.append(str(exc))
            continue
        if base_count != head_count and not contract_in_diff:
            failures.append(
                f"marker count changed in {module} ({base_count} -> {head_count}) without a "
                "paired contract diff — marker changes must accompany a contract revision "
                "(rule: markers-with-contract)"
            )
    return failures, {}


def _gate_landing(cls, base_ref, head_ref, diff, cwd):
    fmap, failures = _load_contract(cls.feature, head_ref, cwd)
    if failures:
        return failures, {}
    failures = _protected_failures(diff)
    map_tests = sorted({t for spec in fmap.components.values() for t in spec["tests"]})
    for test_path in map_tests:
        if file_at_ref(head_ref, test_path, cwd) is None:
            failures.append(
                f"file map out of date: test module {test_path} is missing at the PR head "
                "(rule: file-map-current)"
            )
            continue
        try:
            remaining = count_markers_at(head_ref, test_path, cwd)
        except GateError as exc:
            failures.append(str(exc))
            continue
        if remaining:
            failures.append(
                f"markers remain across feature test modules: {test_path} ({remaining} "
                "expectedFailure marker(s)) — every component must land before the feature "
                "does (rule: landing-markers-zero)"
            )
    diff_set = set(diff)
    others = (tracked_test_modules(base_ref, cwd) | tracked_test_modules(head_ref, cwd)) - set(
        map_tests
    )
    for module in sorted(others):
        if module not in diff_set:
            continue
        try:
            base_count = count_markers_at(base_ref, module, cwd)
            head_count = count_markers_at(head_ref, module, cwd)
        except GateError as exc:
            failures.append(str(exc))
            continue
        if head_count > base_count:
            failures.append(
                f"markers added outside the feature's file map: {module} "
                f"({base_count} -> {head_count}) (rule: no-new-markers)"
            )
    return failures, {}


_GATES = {
    "skeleton": _gate_skeleton,
    "mini": _gate_mini,
    "contract": _gate_contract,
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
    mode.add_argument("--validate-contract", metavar="PATH", help="lint a contract file and exit")
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
        "--component", help="local self-check: gate HEAD as a mini PR for this component"
    )
    args = parser.parse_args(argv)

    if args.validate_contract:
        errors = validate_contract_text(Path(args.validate_contract).read_text(encoding="utf-8"))
        if errors:
            print(f"contract lint: FAIL — {args.validate_contract}")
            for error in errors:
                print(f"  - {error}")
            return 1
        print(f"contract lint: PASS — {args.validate_contract}")
        return 0

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
