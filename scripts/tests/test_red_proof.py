"""Tier-1 suite for scripts/red_proof.py (MUS-53 §8).

Fixture test modules — one per outcome class — are written into project/app/
temporarily and run through the real subprocess path (`manage.py test` with the
JSON-recording runner), so the stripping, per-test classification, and summary
table are all proven offline.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import red_proof as rp  # noqa: E402

RED_PROOF = REPO_ROOT / "scripts" / "red_proof.py"

FIXTURE_HEADER = "import unittest\n\n\n"

FIXTURES = {
    "assertion_red": (
        FIXTURE_HEADER + "class AssertionRedTests(unittest.TestCase):\n"
        "    @unittest.expectedFailure\n"
        "    def test_contract_mismatch(self):\n"
        "        self.assertEqual('implemented', 'not yet')\n"
    ),
    "nie_red": (
        "import unittest\n\n\n"
        "def stub():\n"
        "    raise NotImplementedError\n\n\n"
        "class NotImplementedRedTests(unittest.TestCase):\n"
        "    @unittest.expectedFailure\n"
        "    def test_calls_stub(self):\n"
        "        self.assertEqual(stub(), 'value')\n"
    ),
    "vacuous": (
        FIXTURE_HEADER + "class VacuousTests(unittest.TestCase):\n"
        "    @unittest.expectedFailure\n"
        "    def test_vacuously_true(self):\n"
        "        self.assertTrue(True)\n"
    ),
    "import_error": (
        "import unittest\n\n"
        "import module_that_does_not_exist_mus53  # noqa: F401\n\n\n"
        "class BrokenScaffoldingTests(unittest.TestCase):\n"
        "    @unittest.expectedFailure\n"
        "    def test_never_collected(self):\n"
        "        self.assertEqual(1, 2)\n"
    ),
    "wrong_error": (
        FIXTURE_HEADER + "class WrongErrorTests(unittest.TestCase):\n"
        "    @unittest.expectedFailure\n"
        "    def test_raises_valueerror(self):\n"
        "        raise ValueError('not a contract failure')\n"
    ),
    "two_reds": (
        FIXTURE_HEADER + "class TwoRedsTests(unittest.TestCase):\n"
        "    @unittest.expectedFailure\n"
        "    def test_red_one(self):\n"
        "        self.assertEqual(1, 2)\n\n"
        "    @unittest.expectedFailure\n"
        "    def test_red_two(self):\n"
        "        self.assertEqual(3, 4)\n"
    ),
}


def contract_for(module_rel):
    return (
        "# Contract — red-proof fixture\n\n"
        "## File map\n\n"
        "```yaml\n"
        "# file-map\n"
        "feature: rpfx\n"
        "components:\n"
        "  fx:\n"
        "    files: []\n"
        f"    tests: {module_rel}\n"
        "shared: []\n"
        "```\n"
    )


class StripMarkersTests(unittest.TestCase):
    def test_strip_removes_both_marker_forms(self):
        src = (
            "import unittest\n"
            "from unittest import expectedFailure\n\n\n"
            "class T(unittest.TestCase):\n"
            "    @unittest.expectedFailure\n"
            "    def test_qualified(self):\n"
            "        self.assertEqual(1, 2)\n\n"
            "    @expectedFailure\n"
            "    def test_bare(self):\n"
            "        self.assertEqual(3, 4)\n\n"
            "    def test_unmarked(self):\n"
            "        self.assertTrue(True)\n"
        )
        stripped, marked = rp.strip_expected_failure_markers(src)
        self.assertNotIn("expectedFailure\n    def", stripped)
        self.assertEqual(
            {(cls, name) for cls, name in marked},
            {("T", "test_qualified"), ("T", "test_bare")},
        )
        compile(stripped, "<stripped>", "exec")  # still valid python


class RedProofSubprocessTests(unittest.TestCase):
    """Run scripts/red_proof.py for real against fixture modules in project/app/."""

    def run_red_proof(self, fixture_key):
        module_name = f"tests_rpfx_{fixture_key}"
        module_rel = f"project/app/{module_name}.py"
        module_path = REPO_ROOT / module_rel
        module_path.write_text(FIXTURES[fixture_key])
        self.addCleanup(module_path.unlink)

        with tempfile.TemporaryDirectory() as tmp:
            contract = Path(tmp) / "rpfx.md"
            contract.write_text(contract_for(module_rel))
            env = dict(os.environ)
            env.setdefault("DJANGO_SECRET_KEY", "red-proof-tests-not-a-secret")
            env.pop("DATABASE_URL", None)  # force the zero-setup sqlite path
            proc = subprocess.run(
                [sys.executable, str(RED_PROOF), "--contract", str(contract)],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                env=env,
            )
        return proc, f"project.app.{module_name}"

    def assert_outcome(self, fixture_key, code, *fragments):
        proc, dotted = self.run_red_proof(fixture_key)
        out = proc.stdout + proc.stderr
        self.assertEqual(
            proc.returncode, code, f"exit {proc.returncode}, expected {code}\n--- output ---\n{out}"
        )
        for fragment in fragments:
            self.assertIn(fragment.format(module=dotted), out)

    def test_assertion_failure_counts_red(self):
        self.assert_outcome("assertion_red", 0, "red-proof: PASS", "{module}")

    def test_notimplementederror_counts_red(self):
        self.assert_outcome("nie_red", 0, "red-proof: PASS")

    def test_vacuous_test_fails_naming_test(self):
        self.assert_outcome("vacuous", 1, "vacuous", "test_vacuously_true")

    def test_import_error_fails_naming_module(self):
        self.assert_outcome("import_error", 1, "broken scaffolding", "{module}")

    def test_unexpected_error_type_fails(self):
        self.assert_outcome("wrong_error", 1, "wrong failure type", "ValueError")

    def test_summary_table_lists_module_red_counts(self):
        proc, dotted = self.run_red_proof("two_reds")
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out)
        table_lines = [ln for ln in out.splitlines() if dotted in ln and "red" in ln]
        self.assertTrue(table_lines, f"no summary row for {dotted} in:\n{out}")
        self.assertTrue(
            any("2" in ln for ln in table_lines), f"expected a red count of 2 in:\n{out}"
        )


if __name__ == "__main__":
    unittest.main()
