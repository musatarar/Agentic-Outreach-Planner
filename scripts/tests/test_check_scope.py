"""Tier-1 unit suite for scripts/check_scope.py (MUS-64).

Every gate verdict is proven here against throwaway git repositories built in
tmpdirs — no GitHub, no network. Two properties of the real repo are baked into
the fixtures deliberately, because the gate's semantics only make sense against
them:

* master carries a **permanent** expectedFailure baseline (the real one lives in
  project/app/tests_redteam.py, a documented KNOWN GAP). Every red count here is
  therefore delta-based; a fixture module named tests_baseline.py stands in for
  it, so an absolute-zero rule sneaking back in fails these tests loudly.
* red lives in **two stacks** — BE expectedFailure markers and FE node:test
  todos — so the sweeps are exercised over both.

The ConsistencyTests at the bottom pull the cross-file couplings (workflow job
names <-> ruleset required-check names) into CI facts.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import check_scope as cs  # noqa: E402

CHECK_SCOPE = REPO_ROOT / "scripts" / "check_scope.py"


def clean_env(extra=None):
    """Subprocess env with any ambient GITHUB_* stripped so tests are hermetic."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
    env.update(extra or {})
    return env


def run_cli(cwd, *args, env_extra=None):
    return subprocess.run(
        [sys.executable, str(CHECK_SCOPE), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=clean_env(env_extra),
    )


class Repo:
    """A throwaway git repository with scripted commits and branches."""

    def __init__(self, root: Path):
        self.root = root
        self.git("init", "-q", "-b", "master")
        self.git("config", "user.email", "gate-tests@example.com")
        self.git("config", "user.name", "Gate Tests")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=str(self.root), check=True, capture_output=True, text=True
        )

    def write(self, rel, content):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def delete(self, rel):
        (self.root / rel).unlink()

    def move(self, src, dst):
        self.git("mv", src, dst)

    def commit_all(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", msg)

    def branch(self, name, from_):
        self.git("checkout", "-q", from_)
        self.git("checkout", "-q", "-b", name)

    def checkout(self, name):
        self.git("checkout", "-q", name)


# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------


def service_stub(component, implemented=False):
    body = f'    return "{component}-ok"' if implemented else "    raise NotImplementedError"
    return f"def {component}_value():\n{body}\n"


def tests_module_src(component, marked=1, form="unittest.expectedFailure", plain_tests=0):
    """A Django-style test module with `marked` expectedFailure-decorated tests."""
    lines = ["import unittest", ""]
    if form == "expectedFailure":
        lines.insert(1, "from unittest import expectedFailure")
    lines += ["", f"class Demo{component.title()}Tests(unittest.TestCase):"]
    for i in range(marked):
        lines += [
            f"    @{form}",
            f"    def test_{component}_expectation_{i}(self):",
            f"        self.assertEqual('{component}-ok', 'not-implemented-{i}')",
            "",
        ]
    for i in range(plain_tests):
        lines += [
            f"    def test_{component}_plain_{i}(self):",
            "        self.assertTrue(True)",
            "",
        ]
    if marked == 0 and plain_tests == 0:
        lines += [
            f"    def test_{component}_done(self):",
            "        self.assertTrue(True)",
            "",
        ]
    return "\n".join(lines) + "\n"


def fe_test_src(component, todos=1, plain_tests=0, style="option"):
    """A node:test file carrying `todos` todo tests (option-object or .todo style)."""
    lines = ["import test from 'node:test';", "import assert from 'node:assert/strict';", ""]
    for i in range(todos):
        if style == "call":
            lines += [f"test.todo('{component} expectation {i}');", ""]
        else:
            lines += [
                f"test('{component} expectation {i}', {{ todo: true }}, () => {{",
                f"  assert.equal('{component}-ok', 'not-implemented-{i}');",
                "});",
                "",
            ]
    for i in range(plain_tests):
        lines += [
            f"test('{component} works {i}', () => {{",
            f"  assert.equal('{component}-ok', '{component}-ok');",
            "});",
            "",
        ]
    if todos == 0 and plain_tests == 0:
        lines += [f"test('{component} done', () => {{", "  assert.ok(true);", "});", ""]
    return "\n".join(lines)


def make_base_repo(root: Path) -> Repo:
    """master (carrying the permanent-baseline marker) plus an empty feat/demo."""
    repo = Repo(root)
    repo.write("README.md", "demo repo\n")
    repo.write("CLAUDE.md", "workflow doc\n")
    repo.write("project/app/tests.py", "# base app tests, no markers\n")
    # Stands in for the real tests_redteam.py KNOWN GAP xfail: permanent red on
    # master that no gate may ever demand be removed.
    repo.write("project/app/tests_baseline.py", tests_module_src("baseline", marked=1))
    repo.commit_all("base")
    repo.branch("feat/demo", "master")
    repo.commit_all("feature branch start")
    return repo


def make_feature_repo(root: Path) -> Repo:
    """make_base_repo + a landed skeleton on feat/demo: three components, both stacks.

    alpha is BE-only (2 markers), beta spans both stacks (1 marker + 1 todo),
    gamma is FE-only (2 todos). No contract file — Linear owns the plan now, and
    artifact paths are pure convention: tests_demo_<component>.py and
    frontend/tests/demo_<component>.test.ts.
    """
    repo = make_base_repo(root)
    repo.write("project/app/services/demo_alpha.py", service_stub("alpha"))
    repo.write("project/app/tests_demo_alpha.py", tests_module_src("alpha", marked=2))
    repo.write("project/app/services/demo_beta.py", service_stub("beta"))
    repo.write("project/app/tests_demo_beta.py", tests_module_src("beta", marked=1))
    repo.write("frontend/tests/demo_beta.test.ts", fe_test_src("beta", todos=1))
    repo.write("frontend/src/demo_gamma.ts", "export const gammaValue = () => 'pending';\n")
    repo.write("frontend/tests/demo_gamma.test.ts", fe_test_src("gamma", todos=2))
    repo.commit_all("skeleton")
    return repo


class GateRepoTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = make_feature_repo(Path(tmp.name))

    def gate(self, base, head):
        return run_cli(self.repo.root, "--gate", "--base", base, "--head", head)

    def assert_gate(self, proc, code, *fragments):
        out = proc.stdout + proc.stderr
        self.assertEqual(
            proc.returncode, code, f"exit {proc.returncode}, expected {code}\n--- output ---\n{out}"
        )
        for fragment in fragments:
            self.assertIn(fragment, out)

    def implement_alpha(self):
        """Correct component-PR state: alpha implemented, its markers at zero."""
        self.repo.write("project/app/services/demo_alpha.py", service_stub("alpha", True))
        self.repo.write("project/app/tests_demo_alpha.py", tests_module_src("alpha", marked=0))

    def implement_beta(self, be_marked=0, fe_todos=0):
        self.repo.write("project/app/services/demo_beta.py", service_stub("beta", True))
        self.repo.write(
            "project/app/tests_demo_beta.py", tests_module_src("beta", marked=be_marked)
        )
        self.repo.write("frontend/tests/demo_beta.test.ts", fe_test_src("beta", todos=fe_todos))

    def implement_gamma(self, fe_todos=0):
        self.repo.write("frontend/src/demo_gamma.ts", "export const gammaValue = () => 'ok';\n")
        self.repo.write("frontend/tests/demo_gamma.test.ts", fe_test_src("gamma", todos=fe_todos))


# ---------------------------------------------------------------------------
# protected paths
# ---------------------------------------------------------------------------


class ProtectedPathTests(unittest.TestCase):
    def test_gate_infrastructure_paths_are_protected(self):
        for path in (
            ".github/workflows/ci.yml",
            ".github/workflows/workflow-gate.yml",
            "scripts/check_scope.py",
            "scripts/red_proof.py",
            "scripts/tests/test_check_scope.py",
            "scripts/tests/nested/deep.py",
            "CLAUDE.md",
            "docs/ci.md",
            "docs/rulesets/master.json",
            "evals/golden/cases.json",
            "evals/baselines/rules.json",
            "evals/run_rules_eval.py",
        ):
            with self.subTest(path):
                self.assertTrue(cs.is_protected(path))

    def test_product_and_doc_paths_are_not_protected(self):
        for path in (
            "project/app/services/outreach.py",
            "project/app/tests_demo_alpha.py",
            "frontend/tests/demo_beta.test.ts",
            "README.md",
            "SECURITY.md",
            "docs/plans/whatever.md",
            "scripts/populate_demo_data.py",
            "evals/run_copy_eval.py",
            "requirements-dev.txt",
        ):
            with self.subTest(path):
                self.assertFalse(cs.is_protected(path))

    def test_contracts_template_is_no_longer_protected(self):
        # The contract machinery is gone; the template went with it, so the path
        # must not linger in PROTECTED_PATHS as an unexplained tripwire.
        self.assertFalse(cs.is_protected("docs/contracts/TEMPLATE.md"))
        self.assertNotIn("docs/contracts/TEMPLATE.md", cs.PROTECTED_PATHS)

    def test_directory_glob_requires_the_separator(self):
        self.assertTrue(cs.is_protected("docs/rulesets/feat.json"))
        self.assertFalse(cs.is_protected("docs/rulesets_notes.md"))
        self.assertFalse(cs.is_protected("scripts/tests_helper.py"))

    def test_exact_patterns_match_exactly(self):
        self.assertFalse(cs.is_protected("CLAUDE.md.bak"))
        self.assertFalse(cs.is_protected("docs/ci.md.orig"))
        self.assertFalse(cs.is_protected("frontend/CLAUDE.md"))


# ---------------------------------------------------------------------------
# BE marker accounting (AST) + FE todo accounting (textual)
# ---------------------------------------------------------------------------


class MarkerAccountingTests(unittest.TestCase):
    def test_marker_count_qualified_decorator(self):
        self.assertEqual(cs.count_markers_in_source(tests_module_src("a", marked=2)), 2)

    def test_marker_count_bare_decorator(self):
        src = tests_module_src("a", marked=2, form="expectedFailure")
        self.assertEqual(cs.count_markers_in_source(src), 2)

    def test_marker_count_immune_to_comments_and_strings(self):
        src = (
            "import unittest\n\n"
            "# @unittest.expectedFailure  <- a comment, not a marker\n"
            'NOTE = "@expectedFailure inside a string is not a marker"\n\n'
            "class T(unittest.TestCase):\n"
            "    @unittest.expectedFailure\n"
            "    def test_real(self):\n"
            '        """Docstring mentioning @unittest.expectedFailure."""\n'
            "        self.assertEqual(1, 2)\n"
        )
        self.assertEqual(cs.count_markers_in_source(src), 1)

    def test_marker_count_counts_class_level_marker(self):
        src = (
            "import unittest\n\n\n"
            "@unittest.expectedFailure\n"
            "class T(unittest.TestCase):\n"
            "    def test_a(self):\n"
            "        self.assertEqual(1, 2)\n"
        )
        self.assertEqual(cs.count_markers_in_source(src), 1)

    def test_marker_count_at_ref_missing_file_is_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_feature_repo(Path(tmp))
            # The feature's modules do not exist on master — absent means zero
            # red, which is what makes the landing delta rule work.
            self.assertEqual(
                cs.count_markers_at("master", "project/app/tests_demo_alpha.py", cwd=repo.root), 0
            )
            self.assertEqual(
                cs.count_markers_at("feat/demo", "project/app/tests_demo_alpha.py", cwd=repo.root),
                2,
            )

    def test_marker_count_at_ref_unparseable_raises_named_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_base_repo(Path(tmp))
            repo.checkout("feat/demo")
            repo.write("project/app/tests_broken.py", "class T(:\n")
            repo.commit_all("syntactically broken test module")
            with self.assertRaises(cs.GateError) as ctx:
                cs.count_markers_at("feat/demo", "project/app/tests_broken.py", cwd=repo.root)
            self.assertIn("marker-accounting", str(ctx.exception))


class FeTodoCountingTests(unittest.TestCase):
    def test_option_key_true_counts(self):
        src = "test('x', { todo: true }, () => {});\n"
        self.assertEqual(cs.count_fe_todos_in_source(src), 1)

    def test_option_key_false_does_not_count(self):
        src = "test('x', { todo: false }, () => {});\n"
        self.assertEqual(cs.count_fe_todos_in_source(src), 0)

    def test_option_key_string_reason_counts(self):
        # node:test accepts a reason string; a reason is still a todo.
        src = "test('x', { todo: 'blocked on the engine' }, () => {});\n"
        self.assertEqual(cs.count_fe_todos_in_source(src), 1)

    def test_option_key_tolerates_whitespace_variants(self):
        for src in ("{todo:true}", "{ todo : true }", "{\n  todo: true,\n}"):
            with self.subTest(src):
                self.assertEqual(cs.count_fe_todos_in_source(src), 1)

    def test_call_form_counts_every_runner_entry_point(self):
        src = "test.todo('a');\nit.todo('b');\ndescribe.todo('c');\n"
        self.assertEqual(cs.count_fe_todos_in_source(src), 3)

    def test_call_form_tolerates_space_before_paren(self):
        self.assertEqual(cs.count_fe_todos_in_source("test.todo ('a');\n"), 1)

    def test_option_and_call_forms_add_up(self):
        src = fe_test_src("beta", todos=2) + fe_test_src("beta", todos=1, style="call")
        self.assertEqual(cs.count_fe_todos_in_source(src), 3)

    def test_no_todos_is_zero(self):
        self.assertEqual(cs.count_fe_todos_in_source(fe_test_src("beta", todos=0)), 0)
        self.assertEqual(cs.count_fe_todos_in_source(""), 0)

    def test_over_counting_is_pinned_deliberate_behaviour(self):
        # The counter is textual so it can run on `git show` blobs. It therefore
        # over-approximates: a todo-looking token in a comment counts. Pinned on
        # purpose — over-counting fails loudly, under-counting would let a red
        # test slip through a component gate silently.
        self.assertEqual(cs.count_fe_todos_in_source("// todo: wire this up\n"), 1)
        self.assertEqual(cs.count_fe_todos_in_source("/* obj.todo(x) in prose */\n"), 1)
        # Upper-case TODO comments are the common case and do NOT count, so
        # ordinary source comments stay quiet.
        self.assertEqual(cs.count_fe_todos_in_source("// TODO: ordinary code comment\n"), 0)

    def test_todo_count_at_ref_missing_file_is_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_feature_repo(Path(tmp))
            self.assertEqual(
                cs.count_fe_todos_at("master", "frontend/tests/demo_beta.test.ts", cwd=repo.root), 0
            )
            self.assertEqual(
                cs.count_fe_todos_at(
                    "feat/demo", "frontend/tests/demo_gamma.test.ts", cwd=repo.root
                ),
                2,
            )


# ---------------------------------------------------------------------------
# artifact addressing
# ---------------------------------------------------------------------------


class ArtifactPathTests(unittest.TestCase):
    def test_feature_slug_maps_dashes_and_case(self):
        self.assertEqual(cs._feature_slug("mus-47"), "mus_47")
        self.assertEqual(cs._feature_slug("MUS-47"), "mus_47")

    def test_feature_slug_rejects_unmappable_names(self):
        for feature in ("demo.v2", "demo/two", "demo two", "demo+"):
            with self.subTest(feature), self.assertRaises(cs.GateError) as ctx:
                cs._feature_slug(feature)
            self.assertIn("feature-slug", str(ctx.exception))

    def test_artifact_paths_are_feature_prefixed(self):
        be, fe = cs._artifact_paths("mus-47", "scope_engine")
        self.assertEqual(be, "project/app/tests_mus_47_scope_engine.py")
        self.assertEqual(fe, "frontend/tests/mus_47_scope_engine.test.ts")

    def test_artifact_paths_cannot_collide_with_a_product_module(self):
        # The real risk this prefix exists to kill: a bare tests_<component>.py
        # would bind to an existing product suite (e.g. tests_queue.py).
        be, _ = cs._artifact_paths("mus-47", "queue")
        self.assertNotEqual(be, "project/app/tests_queue.py")
        self.assertEqual(be, "project/app/tests_mus_47_queue.py")


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


class ClassificationTests(unittest.TestCase):
    def test_classify_skeleton_pr(self):
        c = cs.classify("feat/demo", "feat/demo--skeleton")
        self.assertEqual((c.pr_class, c.feature), ("skeleton", "demo"))

    def test_classify_component_pr_extracts_component(self):
        c = cs.classify("feat/demo", "feat/demo--scope_engine")
        self.assertEqual(
            (c.pr_class, c.feature, c.component), ("component", "demo", "scope_engine")
        )

    def test_classify_contract_is_now_an_ordinary_component_slug(self):
        # The contract PR class is gone: nothing is special about the word.
        c = cs.classify("feat/demo", "feat/demo--contract")
        self.assertEqual((c.pr_class, c.component), ("component", "contract"))

    def test_classify_rejects_dashed_component_slug(self):
        with self.assertRaises(cs.GateError) as ctx:
            cs.classify("feat/demo", "feat/demo--contract-v2")
        self.assertIn("component slug", str(ctx.exception))

    def test_classify_rejects_uppercase_component_slug(self):
        with self.assertRaises(cs.GateError) as ctx:
            cs.classify("feat/demo", "feat/demo--ScopeEngine")
        self.assertIn("component slug", str(ctx.exception))

    def test_classify_rejects_punctuated_component_slugs(self):
        for head in ("feat/demo--alpha.v2", "feat/demo--alpha beta", "feat/demo--alpha+beta"):
            with self.subTest(head), self.assertRaises(cs.GateError) as ctx:
                cs.classify("feat/demo", head)
            self.assertIn("component slug", str(ctx.exception))

    def test_classify_accepts_digits_and_underscores_in_slug(self):
        c = cs.classify("feat/mus-47", "feat/mus-47--scope_engine_2")
        self.assertEqual((c.pr_class, c.component), ("component", "scope_engine_2"))

    def test_classify_feature_landing(self):
        c = cs.classify("master", "feat/demo")
        self.assertEqual((c.pr_class, c.feature), ("landing", "demo"))

    def test_classify_workflow_head_cannot_land_on_master(self):
        with self.assertRaises(cs.GateError) as ctx:
            cs.classify("master", "feat/demo--alpha")
        self.assertIn("workflow naming", str(ctx.exception))

    def test_classify_meta_pr(self):
        self.assertEqual(cs.classify("master", "meta/workflow-rework").pr_class, "meta")

    def test_classify_normal_pr_noop(self):
        self.assertEqual(cs.classify("master", "musansht/mus-99-bugfix").pr_class, "normal")
        self.assertEqual(cs.classify("master", "revert-12-something").pr_class, "normal")

    def test_classify_malformed_head_under_feat_base_fails(self):
        for head in ("fix-typo", "feat/other--alpha", "feat/demo-alpha", "feat/demo--"):
            with self.subTest(head), self.assertRaises(cs.GateError) as ctx:
                cs.classify("feat/demo", head)
            self.assertIn("workflow naming", str(ctx.exception))

    def test_classify_stacked_base_rejected(self):
        with self.assertRaises(cs.GateError) as ctx:
            cs.classify("feat/demo--alpha", "feat/demo--alpha_fix")
        self.assertIn("stacked", str(ctx.exception).lower())

    def test_classify_strips_ref_prefixes(self):
        c = cs.classify("refs/heads/feat/demo", "origin/feat/demo--alpha")
        self.assertEqual((c.pr_class, c.feature, c.component), ("component", "demo", "alpha"))

    def test_classify_cli_writes_github_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_feature_repo(Path(tmp))
            out_file = Path(tmp) / "github_output"
            proc = run_cli(
                repo.root,
                "--classify",
                env_extra={
                    "GITHUB_BASE_REF": "feat/demo",
                    "GITHUB_HEAD_REF": "feat/demo--alpha",
                    "GITHUB_OUTPUT": str(out_file),
                },
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            written = out_file.read_text()
            self.assertIn("class=component", written)
            self.assertIn("feature=demo", written)
            self.assertIn("component=alpha", written)

    def test_classify_cli_malformed_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_feature_repo(Path(tmp))
            proc = run_cli(
                repo.root, "--classify", "--base", "feat/demo", "--head", "renamed-wrong"
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("workflow naming", proc.stdout + proc.stderr)


# ---------------------------------------------------------------------------
# component-PR enforcement
# ---------------------------------------------------------------------------


class ComponentGateTests(GateRepoTestCase):
    def test_be_only_component_zeroing_markers_passes_and_emits_targets(self):
        self.repo.branch("feat/demo--alpha", "feat/demo")
        self.implement_alpha()
        self.repo.commit_all("implement alpha")
        proc = self.gate("feat/demo", "feat/demo--alpha")
        self.assert_gate(proc, 0, "PASS", "scoped test targets: project.app.tests_demo_alpha")

    def test_be_marker_remaining_rejected(self):
        self.repo.branch("feat/demo--alpha", "feat/demo")
        self.repo.write("project/app/services/demo_alpha.py", service_stub("alpha", True))
        self.repo.write("project/app/tests_demo_alpha.py", tests_module_src("alpha", marked=1))
        self.repo.commit_all("one marker left")
        self.assert_gate(
            self.gate("feat/demo", "feat/demo--alpha"),
            1,
            "1 expectedFailure marker(s) remaining in project/app/tests_demo_alpha.py",
            "rule: markers-zero",
        )

    def test_fe_only_component_zeroing_todos_passes_without_targets(self):
        self.repo.branch("feat/demo--gamma", "feat/demo")
        self.implement_gamma(fe_todos=0)
        self.repo.commit_all("implement gamma")
        proc = self.gate("feat/demo", "feat/demo--gamma")
        self.assert_gate(proc, 0, "PASS")
        # No BE artifact -> no scoped suite to run; ci-ok's npm test covers FE.
        self.assertNotIn("scoped test targets", proc.stdout + proc.stderr)

    def test_fe_todo_remaining_rejected(self):
        self.repo.branch("feat/demo--gamma", "feat/demo")
        self.implement_gamma(fe_todos=1)
        self.repo.commit_all("one todo left")
        self.assert_gate(
            self.gate("feat/demo", "feat/demo--gamma"),
            1,
            "1 FE todo(s) remaining in frontend/tests/demo_gamma.test.ts",
            "rule: todos-zero",
        )

    def test_both_stack_component_must_zero_both(self):
        self.repo.branch("feat/demo--beta", "feat/demo")
        self.implement_beta(be_marked=0, fe_todos=0)
        self.repo.commit_all("implement beta on both stacks")
        self.assert_gate(self.gate("feat/demo", "feat/demo--beta"), 0, "PASS")

    def test_both_stack_component_leaving_fe_red_rejected(self):
        self.repo.branch("feat/demo--beta", "feat/demo")
        self.implement_beta(be_marked=0, fe_todos=1)
        self.repo.commit_all("beta BE done, FE still red")
        self.assert_gate(
            self.gate("feat/demo", "feat/demo--beta"),
            1,
            "FE todo(s) remaining in frontend/tests/demo_beta.test.ts",
        )

    def test_both_stack_component_leaving_be_red_rejected(self):
        self.repo.branch("feat/demo--beta", "feat/demo")
        self.implement_beta(be_marked=1, fe_todos=0)
        self.repo.commit_all("beta FE done, BE still red")
        self.assert_gate(
            self.gate("feat/demo", "feat/demo--beta"),
            1,
            "expectedFailure marker(s) remaining in project/app/tests_demo_beta.py",
        )

    def test_component_with_no_artifact_at_base_rejected(self):
        self.repo.branch("feat/demo--delta", "feat/demo")
        self.repo.write("project/app/services/demo_delta.py", service_stub("delta", True))
        self.repo.commit_all("component the skeleton never planted")
        self.assert_gate(
            self.gate("feat/demo", "feat/demo--delta"),
            1,
            "no test artifact for component 'delta'",
            "rule: artifact-exists",
        )

    def test_component_cannot_self_create_its_artifact(self):
        self.repo.branch("feat/demo--delta", "feat/demo")
        # Bringing your own (already-green) artifact does not make you a
        # component: the lookup is at base, so this is still artifact-exists.
        self.repo.write("project/app/tests_demo_delta.py", tests_module_src("delta", marked=0))
        self.repo.commit_all("PR plants its own artifact")
        self.assert_gate(self.gate("feat/demo", "feat/demo--delta"), 1, "rule: artifact-exists")

    def test_self_created_second_stack_artifact_is_swept_as_a_sibling(self):
        # gamma owns only an FE artifact at base. A BE file appearing at gamma's
        # BE artifact path is NOT its own artifact — it is swept like any
        # sibling, so smuggling fresh red in there fails.
        self.repo.branch("feat/demo--gamma", "feat/demo")
        self.implement_gamma(fe_todos=0)
        self.repo.write("project/app/tests_demo_gamma.py", tests_module_src("gamma", marked=1))
        self.repo.commit_all("gamma green on FE but plants a red BE module")
        self.assert_gate(
            self.gate("feat/demo", "feat/demo--gamma"),
            1,
            "project/app/tests_demo_gamma.py",
            "rule: sibling-frozen",
        )

    def test_artifact_deletion_rejected(self):
        self.repo.branch("feat/demo--alpha", "feat/demo")
        self.repo.write("project/app/services/demo_alpha.py", service_stub("alpha", True))
        self.repo.delete("project/app/tests_demo_alpha.py")
        self.repo.commit_all("delete the artifact instead of greening it")
        self.assert_gate(
            self.gate("feat/demo", "feat/demo--alpha"),
            1,
            "project/app/tests_demo_alpha.py",
            "rule: artifact-current",
        )

    def test_artifact_rename_rejected(self):
        self.repo.branch("feat/demo--alpha", "feat/demo")
        self.repo.write("project/app/services/demo_alpha.py", service_stub("alpha", True))
        self.repo.move("project/app/tests_demo_alpha.py", "project/app/tests_demo_alpha2.py")
        self.repo.write("project/app/tests_demo_alpha2.py", tests_module_src("alpha", marked=0))
        self.repo.commit_all("rename the artifact")
        self.assert_gate(
            self.gate("feat/demo", "feat/demo--alpha"),
            1,
            "project/app/tests_demo_alpha.py",
            "rule: artifact-current",
        )

    def test_sibling_be_marker_change_rejected(self):
        self.repo.branch("feat/demo--alpha", "feat/demo")
        self.implement_alpha()
        self.repo.write("project/app/tests_demo_beta.py", tests_module_src("beta", marked=0))
        self.repo.commit_all("alpha done but beta's marker removed")
        self.assert_gate(
            self.gate("feat/demo", "feat/demo--alpha"),
            1,
            "expectedFailure marker count changed in sibling test file "
            "project/app/tests_demo_beta.py (1 -> 0)",
        )

    def test_sibling_fe_todo_tampering_rejected(self):
        self.repo.branch("feat/demo--alpha", "feat/demo")
        self.implement_alpha()
        self.repo.write("frontend/tests/demo_gamma.test.ts", fe_test_src("gamma", todos=1))
        self.repo.commit_all("alpha done but gamma's todo quietly removed")
        self.assert_gate(
            self.gate("feat/demo", "feat/demo--alpha"),
            1,
            "FE todo count changed in sibling test file frontend/tests/demo_gamma.test.ts (2 -> 1)",
            "rule: sibling-frozen",
        )

    def test_new_red_added_anywhere_else_rejected(self):
        self.repo.branch("feat/demo--alpha", "feat/demo")
        self.implement_alpha()
        self.repo.write("project/app/tests_extra.py", tests_module_src("extra", marked=1))
        self.repo.commit_all("alpha done but a new marker appears elsewhere")
        self.assert_gate(
            self.gate("feat/demo", "feat/demo--alpha"),
            1,
            "sibling test file project/app/tests_extra.py (0 -> 1)",
        )

    def test_baseline_marker_may_not_be_touched_by_a_component(self):
        self.repo.branch("feat/demo--alpha", "feat/demo")
        self.implement_alpha()
        self.repo.write("project/app/tests_baseline.py", tests_module_src("baseline", marked=0))
        self.repo.commit_all("alpha done but the permanent baseline was 'cleaned up'")
        self.assert_gate(
            self.gate("feat/demo", "feat/demo--alpha"),
            1,
            "project/app/tests_baseline.py (1 -> 0)",
        )

    def test_protected_path_rejected(self):
        self.repo.branch("feat/demo--alpha", "feat/demo")
        self.implement_alpha()
        self.repo.write("CLAUDE.md", "rewritten by a compliant-looking component PR\n")
        self.repo.commit_all("alpha + protected edit")
        self.assert_gate(
            self.gate("feat/demo", "feat/demo--alpha"),
            1,
            "protected path modified: CLAUDE.md",
            "rule: protected-paths",
        )

    def test_component_may_touch_any_non_protected_file(self):
        # File-map scoping and shared-file rejection are deleted by design:
        # sequential work, human merges, and frozen sibling tests are the
        # mitigations, and this is documented as review-owned.
        self.repo.branch("feat/demo--alpha", "feat/demo")
        self.implement_alpha()
        self.repo.write("project/app/services/demo_beta.py", service_stub("beta", True))
        self.repo.write("project/app/urls_demo.py", "urlpatterns = []\n")
        self.repo.write("README.md", "demo repo, documented\n")
        self.repo.commit_all("alpha plus edits a file map would have blocked")
        self.assert_gate(self.gate("feat/demo", "feat/demo--alpha"), 0, "PASS")

    def test_repeat_component_pr_passes_with_no_red_left_to_clear(self):
        # The fix/undo path on a feature branch: alpha already landed green, and
        # a corrective PR for the same component must still be legal.
        self.repo.checkout("feat/demo")
        self.implement_alpha()
        self.repo.commit_all("alpha landed")
        self.repo.branch("feat/demo--alpha", "feat/demo")
        self.repo.write("project/app/services/demo_alpha.py", service_stub("alpha", True) + "\n")
        self.repo.commit_all("corrective follow-up for alpha")
        self.assert_gate(self.gate("feat/demo", "feat/demo--alpha"), 0, "PASS")

    def test_unmappable_feature_name_rejected(self):
        repo = make_base_repo(Path(tempfile.mkdtemp()))
        repo.branch("feat/demo.v2", "master")
        repo.commit_all("feature with an unmappable name")
        repo.branch("feat/demo.v2--alpha", "feat/demo.v2")
        repo.write("project/app/services/x.py", "VALUE = 1\n")
        repo.commit_all("component under an unmappable feature")
        proc = run_cli(
            repo.root, "--gate", "--base", "feat/demo.v2", "--head", "feat/demo.v2--alpha"
        )
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, out)
        self.assertIn("rule: feature-slug", out)

    def test_local_self_check_shorthand_gates_head(self):
        self.repo.branch("feat/demo--alpha", "feat/demo")
        self.implement_alpha()
        self.repo.commit_all("implement alpha")
        proc = run_cli(self.repo.root, "--gate", "--base", "feat/demo", "--component", "alpha")
        self.assert_gate(proc, 0, "PASS", "component 'alpha'")


# ---------------------------------------------------------------------------
# skeleton PRs: red delta, not absolute markers
# ---------------------------------------------------------------------------


class SkeletonGateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = make_base_repo(Path(tmp.name))
        self.repo.branch("feat/demo--skeleton", "feat/demo")

    def run_gate(self):
        return run_cli(
            self.repo.root, "--gate", "--base", "feat/demo", "--head", "feat/demo--skeleton"
        )

    def assert_gate(self, proc, code, *fragments):
        out = proc.stdout + proc.stderr
        self.assertEqual(
            proc.returncode, code, f"exit {proc.returncode}, expected {code}\n--- output ---\n{out}"
        )
        for fragment in fragments:
            self.assertIn(fragment, out)

    def test_skeleton_adding_be_markers_passes(self):
        self.repo.write("project/app/services/demo_alpha.py", service_stub("alpha"))
        self.repo.write("project/app/tests_demo_alpha.py", tests_module_src("alpha", marked=2))
        self.repo.commit_all("skeleton with BE markers")
        self.assert_gate(self.run_gate(), 0, "PASS")

    def test_skeleton_adding_only_fe_todos_passes(self):
        # An FE-only skeleton has no BE markers at all; the delta spans both
        # stacks, so it is still a legitimate skeleton.
        self.repo.write(
            "frontend/src/demo_gamma.ts", "export const gammaValue = () => 'pending';\n"
        )
        self.repo.write("frontend/tests/demo_gamma.test.ts", fe_test_src("gamma", todos=2))
        self.repo.commit_all("FE-only skeleton")
        self.assert_gate(self.run_gate(), 0, "PASS")

    def test_marker_neutral_skeleton_rejected_despite_the_baseline(self):
        # The whole point of the delta rule: master already carries a permanent
        # marker, so "markers exist" proves nothing. A skeleton that changes no
        # red must fail even though the absolute count is non-zero.
        self.repo.write("project/app/services/demo_alpha.py", service_stub("alpha"))
        self.repo.write("docs/notes.md", "some scaffolding prose\n")
        self.repo.commit_all("stubs and prose, no red")
        self.assert_gate(
            self.run_gate(),
            1,
            "skeleton PR changes no red tests",
            "total 1 at both base and head",
            "rule: skeleton-delta",
        )

    def test_removal_only_skeleton_passes(self):
        # Re-skeletoning mid-feature may legitimately retire a red test; a
        # negative delta is still a delta.
        self.repo.write("project/app/tests_baseline.py", tests_module_src("baseline", marked=0))
        self.repo.commit_all("retire a red test")
        self.assert_gate(self.run_gate(), 0, "PASS")

    def test_skeleton_may_create_unclaimed_files(self):
        # No file map exists any more, so a skeleton may scaffold anything
        # non-protected as long as it moves the red count.
        self.repo.write("project/app/tests_demo_alpha.py", tests_module_src("alpha", marked=1))
        self.repo.write("project/app/serializers_demo.py", "# unclaimed scaffolding\n")
        self.repo.write("frontend/src/whatever.ts", "export const x = 1;\n")
        self.repo.commit_all("skeleton with unclaimed files")
        self.assert_gate(self.run_gate(), 0, "PASS")

    def test_skeleton_protected_path_still_rejected(self):
        self.repo.write("project/app/tests_demo_alpha.py", tests_module_src("alpha", marked=1))
        self.repo.write("CLAUDE.md", "skeleton tries to rewrite the rules\n")
        self.repo.commit_all("skeleton touching protected path")
        self.assert_gate(self.run_gate(), 1, "protected path modified: CLAUDE.md")


# ---------------------------------------------------------------------------
# feature landing: per-file delta (head <= base)
# ---------------------------------------------------------------------------


class FeatureLandingTests(GateRepoTestCase):
    def finish_feature(self, be_marked=0, fe_todos=0):
        self.repo.checkout("feat/demo")
        self.implement_alpha()
        self.implement_beta(be_marked=be_marked, fe_todos=fe_todos)
        self.implement_gamma(fe_todos=fe_todos)
        self.repo.commit_all("feature complete")

    def test_landing_with_no_red_left_passes(self):
        self.finish_feature()
        self.assert_gate(self.gate("master", "feat/demo"), 0, "PASS")

    def test_landing_preserves_the_permanent_baseline(self):
        # The regression this rule exists to prevent: an absolute-zero landing
        # check would demand master's documented KNOWN GAP xfail be deleted, and
        # would block every future landing forever.
        self.finish_feature()
        self.assertEqual(
            cs.count_markers_at("feat/demo", "project/app/tests_baseline.py", cwd=self.repo.root), 1
        )
        self.assert_gate(self.gate("master", "feat/demo"), 0, "PASS")

    def test_landing_surviving_be_marker_rejected_names_file(self):
        self.finish_feature(be_marked=1)
        self.assert_gate(
            self.gate("master", "feat/demo"),
            1,
            "expectedFailure marker count increased in project/app/tests_demo_beta.py (0 -> 1)",
            "rule: landing-delta",
        )

    def test_landing_surviving_fe_todo_rejected_names_file(self):
        self.finish_feature(fe_todos=1)
        self.assert_gate(
            self.gate("master", "feat/demo"),
            1,
            "FE todo count increased in frontend/tests/demo_gamma.test.ts (0 -> 1)",
            "rule: landing-delta",
        )

    def test_landing_may_reduce_red_below_the_baseline(self):
        self.finish_feature()
        self.repo.write("project/app/tests_baseline.py", tests_module_src("baseline", marked=0))
        self.repo.commit_all("also retire the baseline xfail")
        self.assert_gate(self.gate("master", "feat/demo"), 0, "PASS")

    def test_landing_protected_path_rejected(self):
        self.finish_feature()
        self.repo.write("docs/ci.md", "a feature branch rewriting the enforcement docs\n")
        self.repo.commit_all("protected edit on the feature branch")
        self.assert_gate(self.gate("master", "feat/demo"), 1, "protected path modified: docs/ci.md")


# ---------------------------------------------------------------------------
# edges + messages
# ---------------------------------------------------------------------------


class EdgeTests(GateRepoTestCase):
    def test_empty_diff_noop_success(self):
        self.repo.branch("feat/demo--alpha", "feat/demo")
        self.assert_gate(self.gate("feat/demo", "feat/demo--alpha"), 0, "empty diff")

    def test_docs_only_pr_to_master_noop_success(self):
        self.repo.branch("docs/readme-tweak", "master")
        self.repo.write("README.md", "demo repo, improved\n")
        self.repo.commit_all("docs only")
        self.assert_gate(self.gate("master", "docs/readme-tweak"), 0, "normal")

    def test_meta_pr_noop_success(self):
        self.repo.branch("meta/gate-fix", "master")
        self.repo.write("CLAUDE.md", "meta PRs may touch protected paths\n")
        self.repo.commit_all("meta change")
        self.assert_gate(self.gate("master", "meta/gate-fix"), 0, "meta")

    def test_failure_messages_name_file_and_rule(self):
        self.repo.branch("feat/demo--alpha", "feat/demo")
        self.repo.write("project/app/services/demo_alpha.py", service_stub("alpha", True))
        self.repo.write("project/app/tests_demo_alpha.py", tests_module_src("alpha", marked=1))
        self.repo.commit_all("marker left behind")
        proc = self.gate("feat/demo", "feat/demo--alpha")
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, out)
        self.assertIn("project/app/tests_demo_alpha.py", out)
        self.assertIn("rule:", out)

    def test_unresolvable_base_ref_reports_cleanly(self):
        proc = self.gate("feat/nonexistent", "feat/nonexistent--alpha")
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, out)
        self.assertIn("cannot resolve git ref", out)


# ---------------------------------------------------------------------------
# consistency: cross-file couplings pulled into tier 1
# ---------------------------------------------------------------------------


def _job_check_name(jobs, job_id):
    """The status-check context a job reports as: its `name` if set, else its id."""
    return jobs[job_id].get("name", job_id)


class ConsistencyTests(unittest.TestCase):
    def test_workflow_yamls_parse(self):
        for rel in (".github/workflows/ci.yml", ".github/workflows/workflow-gate.yml"):
            with self.subTest(rel):
                parsed = yaml.safe_load((REPO_ROOT / rel).read_text())
                self.assertIn("jobs", parsed)

    def test_ruleset_json_check_names_match_workflow_job_names(self):
        import json

        ci_jobs = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
        gate_jobs = yaml.safe_load((REPO_ROOT / ".github/workflows/workflow-gate.yml").read_text())[
            "jobs"
        ]
        self.assertEqual(_job_check_name(ci_jobs, "ci-ok"), "ci-ok")
        self.assertEqual(_job_check_name(gate_jobs, "workflow-gate"), "workflow-gate")

        for ruleset in ("docs/rulesets/master.json", "docs/rulesets/feat.json"):
            with self.subTest(ruleset):
                data = json.loads((REPO_ROOT / ruleset).read_text())
                contexts = {
                    check["context"]
                    for rule in data["rules"]
                    if rule["type"] == "required_status_checks"
                    for check in rule["parameters"]["required_status_checks"]
                }
                self.assertEqual(contexts, {"ci-ok", "workflow-gate"})
                self.assertEqual(data.get("bypass_actors", []), [], "no-bypass is the point")


if __name__ == "__main__":
    unittest.main()
