"""Tier-1 unit suite for scripts/check_scope.py (MUS-53).

Every gate verdict is proven here against throwaway git repositories built in
tmpdirs — no GitHub, no network. The ConsistencyTests at the bottom pull the
cross-file couplings (workflow job names <-> ruleset required-check names,
TEMPLATE.md <-> contract lint) into CI facts.
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
            f"    def test_{component}_contract_{i}(self):",
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


def demo_components():
    return {
        "alpha": {
            "files": ["project/app/services/demo_alpha.py"],
            "tests": ["project/app/tests_demo_alpha.py"],
        },
        "beta": {
            "files": ["project/app/services/demo_beta.py"],
            "tests": ["project/app/tests_demo_beta.py"],
        },
        "assembly": {
            "files": ["project/app/services/demo_assembly.py"],
            "tests": ["project/app/tests_demo_assembly.py"],
        },
    }


def contract_md(components=None, shared=(), include_sections=True, feature="demo"):
    data = {"feature": feature, "components": components or demo_components()}
    data["shared"] = list(shared)
    ymap = yaml.safe_dump(data, sort_keys=False).rstrip()
    parts = [f"# Contract — {feature}", ""]
    if include_sections:
        parts += [
            "## Interfaces",
            "Each component exposes the functions named in its stub file.",
            "",
            "## Data shapes",
            "Plain dicts as described per interface.",
            "",
            "## Error contract",
            "Unimplemented stubs raise NotImplementedError.",
            "",
        ]
    parts += ["## File map", "", "```yaml", "# file-map", ymap, "```", ""]
    return "\n".join(parts)


def make_feature_repo(root: Path, contract=None) -> Repo:
    """master + feat/demo carrying a contract and a marked skeleton."""
    repo = Repo(root)
    repo.write("README.md", "demo repo\n")
    repo.write("CLAUDE.md", "workflow doc\n")
    repo.write("project/app/tests.py", "# base app tests\n")
    repo.commit_all("base")
    repo.branch("feat/demo", "master")
    repo.write("docs/contracts/demo.md", contract if contract is not None else contract_md())
    for comp in ("alpha", "beta", "assembly"):
        repo.write(f"project/app/services/demo_{comp}.py", service_stub(comp))
        n = 2 if comp == "alpha" else 1
        repo.write(f"project/app/tests_demo_{comp}.py", tests_module_src(comp, marked=n))
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
        """Correct mini-PR state: alpha implemented, its markers at zero."""
        self.repo.write("project/app/services/demo_alpha.py", service_stub("alpha", True))
        self.repo.write("project/app/tests_demo_alpha.py", tests_module_src("alpha", marked=0))


# ---------------------------------------------------------------------------
# file-map parsing
# ---------------------------------------------------------------------------


class FileMapParsingTests(unittest.TestCase):
    def test_parse_file_map_happy_path(self):
        fmap = cs.parse_file_map(contract_md())
        self.assertEqual(set(fmap.components), {"alpha", "beta", "assembly"})
        self.assertEqual(fmap.components["alpha"]["files"], ["project/app/services/demo_alpha.py"])
        self.assertEqual(fmap.components["alpha"]["tests"], ["project/app/tests_demo_alpha.py"])
        self.assertEqual(fmap.shared, [])

    def test_parse_file_map_normalizes_scalar_tests(self):
        md = contract_md(
            components={
                "engine": {"files": ["project/app/services/e.py"], "tests": "project/app/tests_e.py"},
                "assembly": {"files": [], "tests": "project/app/tests_asm.py"},
            }
        )
        fmap = cs.parse_file_map(md)
        self.assertEqual(fmap.components["engine"]["tests"], ["project/app/tests_e.py"])

    def test_parse_missing_file_map_block(self):
        with self.assertRaises(cs.FileMapError) as ctx:
            cs.parse_file_map("# Contract\n\nno fenced map here\n")
        self.assertIn("file-map", str(ctx.exception))

    def test_parse_invalid_yaml_names_line(self):
        bad = "# Contract\n\n```yaml\n# file-map\ncomponents:\n  alpha: [unclosed\n```\n"
        with self.assertRaises(cs.FileMapError) as ctx:
            cs.parse_file_map(bad)
        self.assertIn("line", str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# contract lint (--validate-contract)
# ---------------------------------------------------------------------------


class ContractLintTests(unittest.TestCase):
    def test_lint_valid_contract_passes(self):
        self.assertEqual(cs.validate_contract_text(contract_md()), [])

    def test_lint_template_example_passes(self):
        # C1<->C2 coupling lock: the committed TEMPLATE's example map must lint
        # clean forever, else doc/format drift breaks CI.
        proc = run_cli(REPO_ROOT, "--validate-contract", "docs/contracts/TEMPLATE.md")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_lint_file_owned_by_two_components_rejected(self):
        comps = demo_components()
        comps["beta"]["files"].append("project/app/services/demo_alpha.py")
        errors = "\n".join(cs.validate_contract_text(contract_md(comps)))
        self.assertIn("exactly one component", errors)
        self.assertIn("project/app/services/demo_alpha.py", errors)

    def test_lint_shared_overlaps_owned_rejected(self):
        errors = "\n".join(
            cs.validate_contract_text(contract_md(shared=["project/app/services/demo_alpha.py"]))
        )
        self.assertIn("shared", errors)
        self.assertIn("project/app/services/demo_alpha.py", errors)

    def test_lint_missing_assembly_component_rejected(self):
        comps = demo_components()
        del comps["assembly"]
        errors = "\n".join(cs.validate_contract_text(contract_md(comps)))
        self.assertIn("assembly", errors)

    def test_lint_missing_required_section_rejected(self):
        errors = "\n".join(cs.validate_contract_text(contract_md(include_sections=False)))
        self.assertIn("section", errors.lower())

    def test_lint_protected_path_claimed_by_component_rejected(self):
        comps = demo_components()
        comps["alpha"]["files"].append("scripts/check_scope.py")
        errors = "\n".join(cs.validate_contract_text(contract_md(comps)))
        self.assertIn("protected", errors)
        self.assertIn("scripts/check_scope.py", errors)

    def test_lint_protected_path_in_shared_rejected(self):
        errors = "\n".join(cs.validate_contract_text(contract_md(shared=["docs/ci.md"])))
        self.assertIn("protected", errors)
        self.assertIn("docs/ci.md", errors)


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


class ClassificationTests(unittest.TestCase):
    def test_classify_skeleton_pr(self):
        c = cs.classify("feat/demo", "feat/demo--skeleton")
        self.assertEqual((c.pr_class, c.feature), ("skeleton", "demo"))

    def test_classify_mini_pr_extracts_component(self):
        c = cs.classify("feat/demo", "feat/demo--scope_engine")
        self.assertEqual((c.pr_class, c.feature, c.component), ("mini", "demo", "scope_engine"))

    def test_classify_contract_change_pr(self):
        c = cs.classify("feat/demo", "feat/demo--contract-v2")
        self.assertEqual((c.pr_class, c.feature), ("contract", "demo"))

    def test_classify_feature_landing(self):
        c = cs.classify("master", "feat/demo")
        self.assertEqual((c.pr_class, c.feature), ("landing", "demo"))

    def test_classify_meta_pr(self):
        self.assertEqual(cs.classify("master", "meta/mus-53-bootstrap").pr_class, "meta")

    def test_classify_normal_pr_noop(self):
        self.assertEqual(cs.classify("master", "musansht/mus-99-bugfix").pr_class, "normal")
        self.assertEqual(cs.classify("master", "revert-12-something").pr_class, "normal")

    def test_classify_malformed_head_under_feat_base_fails(self):
        for head in ("fix-typo", "feat/other--alpha", "feat/demo-alpha", "feat/demo--"):
            with self.assertRaises(cs.GateError, msg=head) as ctx:
                cs.classify("feat/demo", head)
            self.assertIn("workflow naming", str(ctx.exception))

    def test_classify_stacked_base_rejected(self):
        with self.assertRaises(cs.GateError) as ctx:
            cs.classify("feat/demo--alpha", "feat/demo--alpha-fix")
        self.assertIn("stacked", str(ctx.exception).lower())

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
            self.assertIn("class=mini", written)
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
# mini-PR enforcement
# ---------------------------------------------------------------------------


class MiniPrGateTests(GateRepoTestCase):
    def test_mini_diff_within_map_passes(self):
        self.repo.branch("feat/demo--alpha", "feat/demo")
        self.implement_alpha()
        self.repo.commit_all("implement alpha")
        self.assert_gate(self.gate("feat/demo", "feat/demo--alpha"), 0, "PASS")

    def test_mini_diff_outside_map_rejected_names_file(self):
        self.repo.branch("feat/demo--alpha", "feat/demo")
        self.implement_alpha()
        self.repo.write("project/app/services/demo_beta.py", service_stub("beta", True))
        self.repo.commit_all("alpha + stray beta edit")
        self.assert_gate(
            self.gate("feat/demo", "feat/demo--alpha"),
            1,
            "diff outside component file map: project/app/services/demo_beta.py",
        )

    def test_mini_touches_shared_file_rejected(self):
        repo = make_feature_repo(
            Path(tempfile.mkdtemp()), contract=contract_md(shared=["project/app/urls_demo.py"])
        )
        repo.branch("feat/demo--alpha", "feat/demo")
        repo.write("project/app/services/demo_alpha.py", service_stub("alpha", True))
        repo.write("project/app/tests_demo_alpha.py", tests_module_src("alpha", marked=0))
        repo.write("project/app/urls_demo.py", "urlpatterns = []\n")
        repo.commit_all("alpha + shared edit")
        proc = run_cli(repo.root, "--gate", "--base", "feat/demo", "--head", "feat/demo--alpha")
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, out)
        self.assertIn("shared file", out)
        self.assertIn("project/app/urls_demo.py", out)

    def test_mini_touches_protected_path_rejected(self):
        self.repo.branch("feat/demo--alpha", "feat/demo")
        self.implement_alpha()
        self.repo.write("CLAUDE.md", "rewritten by a compliant-looking mini PR\n")
        self.repo.commit_all("alpha + protected edit")
        self.assert_gate(
            self.gate("feat/demo", "feat/demo--alpha"), 1, "protected path", "CLAUDE.md"
        )

    def test_mini_component_not_declared_rejected(self):
        self.repo.branch("feat/demo--gamma", "feat/demo")
        self.repo.write("project/app/services/demo_gamma.py", service_stub("gamma", True))
        self.repo.commit_all("undeclared component")
        self.assert_gate(
            self.gate("feat/demo", "feat/demo--gamma"), 1, "not declared in the contract file map"
        )

    def test_mini_contract_missing_rejected(self):
        repo = Repo(Path(tempfile.mkdtemp()))
        repo.write("README.md", "x\n")
        repo.commit_all("base")
        repo.branch("feat/nodemo", "master")
        repo.write("project/app/services/x.py", "VALUE = 1\n")
        repo.commit_all("feature without contract")
        repo.branch("feat/nodemo--alpha", "feat/nodemo")
        repo.write("project/app/services/x.py", "VALUE = 2\n")
        repo.commit_all("mini without contract")
        proc = run_cli(repo.root, "--gate", "--base", "feat/nodemo", "--head", "feat/nodemo--alpha")
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, out)
        self.assertIn("contract missing: docs/contracts/nodemo.md", out)

    def test_mini_test_module_rename_rejected_file_map_out_of_date(self):
        self.repo.branch("feat/demo--alpha", "feat/demo")
        self.repo.write("project/app/services/demo_alpha.py", service_stub("alpha", True))
        self.repo.move("project/app/tests_demo_alpha.py", "project/app/tests_demo_alpha2.py")
        self.repo.write("project/app/tests_demo_alpha2.py", tests_module_src("alpha", marked=0))
        self.repo.commit_all("rename test module")
        self.assert_gate(
            self.gate("feat/demo", "feat/demo--alpha"),
            1,
            "file map out of date",
            "project/app/tests_demo_alpha.py",
            "contract-change",
        )

    def test_mini_test_module_delete_rejected_file_map_out_of_date(self):
        self.repo.branch("feat/demo--alpha", "feat/demo")
        self.implement_alpha()
        self.repo.delete("project/app/tests_demo_alpha.py")
        self.repo.commit_all("delete test module")
        self.assert_gate(
            self.gate("feat/demo", "feat/demo--alpha"),
            1,
            "file map out of date",
            "project/app/tests_demo_alpha.py",
        )


# ---------------------------------------------------------------------------
# marker accounting (AST)
# ---------------------------------------------------------------------------


class MarkerAccountingTests(GateRepoTestCase):
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

    def test_mini_own_module_zero_after_passes(self):
        self.repo.branch("feat/demo--alpha", "feat/demo")
        self.implement_alpha()
        self.repo.commit_all("markers to zero")
        proc = self.gate("feat/demo", "feat/demo--alpha")
        self.assert_gate(proc, 0, "PASS")
        self.assertIn("project.app.tests_demo_alpha", proc.stdout + proc.stderr)

    def test_mini_own_module_marker_remaining_rejected(self):
        self.repo.branch("feat/demo--alpha", "feat/demo")
        self.repo.write("project/app/services/demo_alpha.py", service_stub("alpha", True))
        self.repo.write("project/app/tests_demo_alpha.py", tests_module_src("alpha", marked=1))
        self.repo.commit_all("one marker left")
        self.assert_gate(
            self.gate("feat/demo", "feat/demo--alpha"),
            1,
            "1 expectedFailure marker(s) remaining in project/app/tests_demo_alpha.py",
        )

    def test_mini_sibling_marker_removed_rejected(self):
        self.repo.branch("feat/demo--alpha", "feat/demo")
        self.implement_alpha()
        self.repo.write("project/app/tests_demo_beta.py", tests_module_src("beta", marked=0))
        self.repo.commit_all("alpha done but beta marker removed")
        self.assert_gate(
            self.gate("feat/demo", "feat/demo--alpha"),
            1,
            "marker count changed in sibling module project/app/tests_demo_beta.py",
        )

    def test_mini_marker_added_anywhere_rejected(self):
        self.repo.branch("feat/demo--alpha", "feat/demo")
        self.implement_alpha()
        self.repo.write("project/app/tests_extra.py", tests_module_src("extra", marked=1))
        self.repo.commit_all("alpha done but a new marker appears elsewhere")
        self.assert_gate(
            self.gate("feat/demo", "feat/demo--alpha"),
            1,
            "marker count changed in sibling module project/app/tests_extra.py",
        )


# ---------------------------------------------------------------------------
# skeleton exemption
# ---------------------------------------------------------------------------


class SkeletonGateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Repo(Path(tmp.name))
        self.repo.write("README.md", "x\n")
        self.repo.write("CLAUDE.md", "doc\n")
        self.repo.commit_all("base")
        self.repo.branch("feat/demo", "master")
        self.repo.commit_all("feature branch start")

    def skeleton_payload(self):
        self.repo.write("docs/contracts/demo.md", contract_md())
        for comp in ("alpha", "beta", "assembly"):
            self.repo.write(f"project/app/services/demo_{comp}.py", service_stub(comp))
            self.repo.write(f"project/app/tests_demo_{comp}.py", tests_module_src(comp, marked=1))

    def run_gate(self):
        return run_cli(
            self.repo.root, "--gate", "--base", "feat/demo", "--head", "feat/demo--skeleton"
        )

    def test_skeleton_may_add_markers(self):
        self.repo.branch("feat/demo--skeleton", "feat/demo")
        self.skeleton_payload()
        self.repo.commit_all("skeleton with markers")
        proc = self.run_gate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_skeleton_scope_check_skipped_contract_still_required(self):
        self.repo.branch("feat/demo--skeleton", "feat/demo")
        self.skeleton_payload()
        # Files no component claims: fine for a skeleton (scope check skipped).
        self.repo.write("project/app/serializers_demo.py", "# unclaimed scaffolding\n")
        self.repo.commit_all("skeleton with unclaimed file")
        proc = self.run_gate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        # ... but a skeleton with no contract at all is rejected.
        self.repo.git("rm", "-q", "docs/contracts/demo.md")
        self.repo.commit_all("drop the contract")
        proc = self.run_gate()
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, out)
        self.assertIn("contract missing: docs/contracts/demo.md", out)

    def test_skeleton_protected_path_still_rejected(self):
        self.repo.branch("feat/demo--skeleton", "feat/demo")
        self.skeleton_payload()
        self.repo.write("CLAUDE.md", "skeleton tries to rewrite the rules\n")
        self.repo.commit_all("skeleton touching protected path")
        proc = self.run_gate()
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, out)
        self.assertIn("protected path", out)
        self.assertIn("CLAUDE.md", out)


# ---------------------------------------------------------------------------
# feature landing
# ---------------------------------------------------------------------------


class FeatureLandingTests(GateRepoTestCase):
    def finish_feature(self, leave_assembly_marker=False):
        self.repo.checkout("feat/demo")
        for comp in ("alpha", "beta", "assembly"):
            self.repo.write(f"project/app/services/demo_{comp}.py", service_stub(comp, True))
            marked = 1 if (comp == "assembly" and leave_assembly_marker) else 0
            self.repo.write(f"project/app/tests_demo_{comp}.py", tests_module_src(comp, marked))
        self.repo.commit_all("feature complete")

    def test_landing_zero_markers_passes(self):
        self.finish_feature()
        self.assert_gate(self.gate("master", "feat/demo"), 0, "PASS")

    def test_landing_surviving_marker_rejected_names_module(self):
        self.finish_feature(leave_assembly_marker=True)
        self.assert_gate(
            self.gate("master", "feat/demo"),
            1,
            "markers remain across feature test modules: project/app/tests_demo_assembly.py",
        )


# ---------------------------------------------------------------------------
# contract-change PRs
# ---------------------------------------------------------------------------


class ContractChangeTests(GateRepoTestCase):
    def test_contract_change_shape_allowed(self):
        self.repo.branch("feat/demo--contract-v2", "feat/demo")
        self.repo.write("docs/contracts/demo.md", contract_md() + "\nRevised interface note.\n")
        self.repo.write(
            "project/app/tests_demo_alpha.py", tests_module_src("alpha", marked=2, plain_tests=1)
        )
        self.repo.commit_all("contract revision + updated expectations")
        self.assert_gate(self.gate("feat/demo", "feat/demo--contract-v2"), 0, "PASS")

    def test_contract_change_marker_add_with_contract_diff_allowed(self):
        self.repo.branch("feat/demo--contract-v2", "feat/demo")
        self.repo.write("docs/contracts/demo.md", contract_md() + "\nNew alpha surface.\n")
        self.repo.write("project/app/tests_demo_alpha.py", tests_module_src("alpha", marked=3))
        self.repo.commit_all("contract grows, marker added")
        self.assert_gate(self.gate("feat/demo", "feat/demo--contract-v2"), 0, "PASS")

    def test_contract_change_marker_add_without_contract_diff_rejected(self):
        self.repo.branch("feat/demo--contract-v2", "feat/demo")
        self.repo.write("project/app/tests_demo_alpha.py", tests_module_src("alpha", marked=3))
        self.repo.commit_all("marker added with no contract change")
        self.assert_gate(
            self.gate("feat/demo", "feat/demo--contract-v2"), 1, "paired contract diff"
        )

    def test_contract_change_touching_implementation_rejected(self):
        self.repo.branch("feat/demo--contract-v2", "feat/demo")
        self.repo.write("docs/contracts/demo.md", contract_md() + "\nRevision.\n")
        self.repo.write("project/app/services/demo_alpha.py", service_stub("alpha", True))
        self.repo.commit_all("contract change smuggling implementation")
        self.assert_gate(
            self.gate("feat/demo", "feat/demo--contract-v2"),
            1,
            "may only touch",
            "project/app/services/demo_alpha.py",
        )


# ---------------------------------------------------------------------------
# edges + messages
# ---------------------------------------------------------------------------


class EdgeTests(GateRepoTestCase):
    def test_empty_diff_noop_success(self):
        self.repo.branch("feat/demo--alpha", "feat/demo")
        proc = self.gate("feat/demo", "feat/demo--alpha")
        self.assert_gate(proc, 0, "empty diff")

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
        self.implement_alpha()
        self.repo.write("project/app/services/demo_beta.py", service_stub("beta", True))
        self.repo.commit_all("out-of-map edit")
        proc = self.gate("feat/demo", "feat/demo--alpha")
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, out)
        self.assertIn("project/app/services/demo_beta.py", out)
        self.assertIn("rule:", out)


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
        gate_jobs = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/workflow-gate.yml").read_text()
        )["jobs"]
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
