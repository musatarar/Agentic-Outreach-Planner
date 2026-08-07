#!/usr/bin/env python3
"""Red-proof (MUS-64): prove a skeleton's marked tests fail for the right reason.

The skeleton's expectedFailure-marked tests are what every downstream gate
measures against, so their genuineness cannot rest on an instruction in
CLAUDE.md. This script mechanizes the old "strip markers locally and confirm
red" ritual:

1. AST-transform the *working copy* (uncommitted) to remove every
   ``@unittest.expectedFailure`` / bare ``@expectedFailure`` marker in the
   target test modules (``--modules``, defaulting to every backend test module
   in the checkout — there is no contract or file map to read them from).
2. Run each marked module for real via ``manage.py test`` with a JSON-recording
   test runner.
3. Classify every formerly-marked test:
   - AssertionError            -> red for the right reason (good)
   - NotImplementedError       -> red for the right reason (good)
   - passes                    -> vacuous test -> FAIL, naming the test
   - ImportError / collection  -> broken scaffolding -> FAIL, naming the module
   - any other error type      -> FAIL, naming the test (conservative)
4. Emit a module -> red-count summary table to the job log.

No markers anywhere is a PASS no-op, not a failure: a frontend-only skeleton has
nothing here to prove, and whether a skeleton went red *at all* is the skeleton
gate's question (its red delta spans both stacks), not this script's.

Usage::

    python scripts/red_proof.py [--modules project/app/tests_x.py ...]

Runs from the repository root. CI runs it on skeleton PRs only; the checkout is
disposable there. Locally, note that it rewrites the marked test modules in
place — run it in a clean worktree or restore them afterwards.
"""

import argparse
import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import check_scope  # noqa: E402

RESULT_FILE_ENV = "RED_PROOF_RESULT_FILE"
GOOD_FAILURE_TYPES = ("AssertionError", "NotImplementedError")
COLLECTION_ERROR_TYPES = ("ImportError", "ModuleNotFoundError", "SyntaxError")


# ---------------------------------------------------------------------------
# marker stripping (AST transform of the working copy)
# ---------------------------------------------------------------------------


class _MarkerStripper(ast.NodeTransformer):
    def __init__(self):
        self.marked = []  # (class_name | None, test_name | None); None test = whole class
        self._class = None

    def visit_ClassDef(self, node):
        if any(check_scope._is_marker(d) for d in node.decorator_list):
            self.marked.append((node.name, None))
        node.decorator_list = [d for d in node.decorator_list if not check_scope._is_marker(d)]
        previous, self._class = self._class, node.name
        self.generic_visit(node)
        self._class = previous
        return node

    def _visit_function(self, node):
        if any(check_scope._is_marker(d) for d in node.decorator_list):
            self.marked.append((self._class, node.name))
        node.decorator_list = [d for d in node.decorator_list if not check_scope._is_marker(d)]
        self.generic_visit(node)
        return node

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function


def strip_expected_failure_markers(source):
    """-> (stripped_source, marked) where marked is [(class_name, test_name), ...]."""
    tree = ast.parse(source)
    stripper = _MarkerStripper()
    tree = stripper.visit(tree)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n", stripper.marked


# ---------------------------------------------------------------------------
# JSON-recording Django test runner (used via manage.py test --testrunner)
# ---------------------------------------------------------------------------


class _RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []

    def _record(self, test, status, err=None):
        exc_type = err[0].__name__ if err else None
        self.records.append({"id": test.id(), "status": status, "exc": exc_type})

    def addSuccess(self, test):
        super().addSuccess(test)
        self._record(test, "pass")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, "fail", err)

    def addError(self, test, err):
        super().addError(test, err)
        self._record(test, "error", err)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._record(test, "skip")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._record(test, "xfail", err)

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._record(test, "xpass")


try:  # Django is a repo dependency; guard only so --help works anywhere.
    from django.test.runner import DiscoverRunner
except ImportError:  # pragma: no cover
    DiscoverRunner = None

if DiscoverRunner is not None:

    class JsonResultRunner(DiscoverRunner):
        """DiscoverRunner that dumps per-test outcomes to $RED_PROOF_RESULT_FILE."""

        def get_resultclass(self):
            return _RecordingResult

        def suite_result(self, suite, result, **kwargs):
            out = os.environ.get(RESULT_FILE_ENV)
            records = getattr(result, "records", None)
            if out and records is not None:
                Path(out).write_text(json.dumps(records), encoding="utf-8")
            return super().suite_result(suite, result, **kwargs)


# ---------------------------------------------------------------------------
# per-test classification
# ---------------------------------------------------------------------------


def _is_marked(record_id, marked):
    parts = record_id.split(".")
    if len(parts) < 2:
        return False
    class_name, test_name = parts[-2], parts[-1]
    for marked_class, marked_test in marked:
        if marked_test is None and class_name == marked_class:
            return True
        if (class_name, test_name) == (marked_class, marked_test):
            return True
    return False


def _classify_module(dotted, records, marked):
    """-> (red_count, problems). Empty problems means every marked test is red."""
    problems = []
    reds = 0
    seen_marked = set()
    for record in records:
        record_id, status, exc = record["id"], record["status"], record["exc"]
        if status == "error" and (exc in COLLECTION_ERROR_TYPES or "_FailedTest" in record_id):
            problems.append(
                f"broken scaffolding: {dotted} failed to collect ({exc}) — an import or "
                "collection error is not a red test"
            )
            continue
        if not _is_marked(record_id, marked):
            continue
        seen_marked.add(record_id)
        if status == "fail":
            reds += 1  # AssertionError family: red for the right reason
        elif status == "error" and exc == "NotImplementedError":
            reds += 1
        elif status == "pass":
            problems.append(
                f"vacuous: {record_id} passes once its marker is stripped — a marked test "
                "must fail against the unimplemented stub it targets"
            )
        elif status == "error":
            problems.append(
                f"wrong failure type: {record_id} raised {exc}; a red test must fail with "
                "AssertionError or NotImplementedError"
            )
        else:
            problems.append(f"unexpected outcome '{status}' for marked test {record_id}")
    expected = {f"{cls}.{name}" for cls, name in marked if name is not None}
    reported = {".".join(r.split(".")[-2:]) for r in seen_marked}
    for missing in sorted(expected - reported):
        if not any(p.startswith("broken scaffolding") for p in problems):
            problems.append(f"marked test never ran: {dotted}.{missing}")
    return reds, problems


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def discover_default_modules():
    """-> sorted repo-relative paths of every backend test module in the checkout.

    The default target set when ``--modules`` is omitted. Deliberately the same
    non-recursive glob the gate's own marker sweep uses
    (``check_scope.TEST_MODULE_GLOB``), so red-proof and the skeleton gate can
    never disagree about which modules count. Pure: a filesystem read, no git.
    """
    return sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in REPO_ROOT.glob(check_scope.TEST_MODULE_GLOB)
    )


def _run_module(dotted, python, manage):
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        result_file = tmp.name
    env = dict(os.environ)
    env[RESULT_FILE_ENV] = result_file
    proc = subprocess.run(
        [python, manage, "test", dotted, "--testrunner", "scripts.red_proof.JsonResultRunner"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    try:
        payload = Path(result_file).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, proc
    finally:
        Path(result_file).unlink(missing_ok=True)
    return json.loads(payload), proc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--modules",
        nargs="*",
        metavar="PATH",
        help="repo-relative test modules to prove (default: every project/app/tests*.py)",
    )
    parser.add_argument("--manage", default="manage.py", help="path to manage.py")
    parser.add_argument("--python", default=sys.executable, help="python interpreter to use")
    args = parser.parse_args(argv)

    modules = discover_default_modules() if args.modules is None else args.modules

    marked_by_module = {}
    for test_path in modules:
        path = REPO_ROOT / test_path
        if not path.exists():
            print(f"red-proof: FAIL — test module {test_path} does not exist in the checkout")
            return 1
        source = path.read_text(encoding="utf-8")
        try:
            stripped, marked = strip_expected_failure_markers(source)
        except SyntaxError as exc:
            print(f"red-proof: FAIL — cannot parse {test_path}: {exc}")
            return 1
        if marked:
            marked_by_module[test_path] = marked
            path.write_text(stripped, encoding="utf-8")

    if not marked_by_module:
        # Nothing to prove is not a failure — see the module docstring. The
        # skeleton gate is what refuses a skeleton that went red nowhere.
        print("red-proof: PASS — no expectedFailure markers found; nothing to prove")
        return 0

    all_problems = []
    summary = []
    for test_path, marked in marked_by_module.items():
        dotted = test_path[:-3].replace("/", ".")
        records, proc = _run_module(dotted, args.python, args.manage)
        if records is None:
            print(f"red-proof: FAIL — test run for {dotted} produced no results")
            print(proc.stdout)
            print(proc.stderr)
            return 1
        reds, problems = _classify_module(dotted, records, marked)
        summary.append((dotted, reds))
        all_problems.extend(problems)

    print("Red-proof summary (module -> red tests):")
    for dotted, reds in summary:
        print(f"  {dotted:<50} {reds} red")
    if all_problems:
        print("red-proof: FAIL —")
        for index, problem in enumerate(all_problems, 1):
            print(f"  {index}. {problem}")
        return 1
    print("red-proof: PASS — every marked test fails for the right reason")
    return 0


if __name__ == "__main__":
    sys.exit(main())
