"""The generated branch dropdown in the demo tunnel workflow.

`scripts/refresh_demo_tunnel_branches.py` rewrites one marked block of
`.github/workflows/demo-tunnel.yml`; a workflow runs it and opens a PR.
"""

import sys
import textwrap
from pathlib import Path

import yaml
from django.conf import settings
from django.test import SimpleTestCase

sys.path.insert(0, str(Path(settings.BASE_DIR) / "scripts"))

import refresh_demo_tunnel_branches as refresh  # noqa: E402

WORKFLOW = Path(settings.BASE_DIR) / ".github" / "workflows" / "demo-tunnel.yml"

TEMPLATE = """      branch:
        type: choice
        default: master
        # <<< generated
        options:
          - master
        # >>> generated
      login_email:
"""


class RenderTests(SimpleTestCase):
    def test_master_leads_and_the_rest_keep_their_order(self):
        rendered = refresh.render_options(["feat/b", "master", "feat/a"], limit=10)

        self.assertEqual(rendered, ["master", "feat/b", "feat/a"])

    def test_the_list_is_capped_and_the_cut_is_reported(self):
        """An unbounded dropdown is unusable, and GitHub's own ceiling on choice
        options is undocumented -- so the cap is ours, and never silent."""
        branches = ["master"] + [f"feat/{i}" for i in range(30)]

        rendered = refresh.render_options(branches, limit=5)

        self.assertEqual(len(rendered), 5)
        self.assertEqual(rendered[0], "master")
        self.assertEqual(refresh.dropped(branches, limit=5), 26)

    def test_master_is_added_when_the_input_somehow_lacks_it(self):
        self.assertEqual(refresh.render_options(["feat/a"], limit=10)[0], "master")

    def test_a_branch_name_that_could_break_the_yaml_is_quoted(self):
        """Branch names are free to look like YAML syntax (`*odd`, `a: b`)."""
        rendered = refresh.rewrite(TEMPLATE, ["master", "feat/*odd: name"], limit=10)

        parsed = yaml.safe_load(textwrap.dedent(rendered))
        self.assertIn("feat/*odd: name", parsed["branch"]["options"])


class RewriteTests(SimpleTestCase):
    def test_only_the_marked_block_is_replaced(self):
        rewritten = refresh.rewrite(TEMPLATE, ["master", "feat/a"], limit=10)

        self.assertIn("        default: master\n", rewritten)
        self.assertIn("      login_email:\n", rewritten)
        self.assertIn("          - feat/a\n", rewritten)

    def test_rewriting_twice_changes_nothing_the_second_time(self):
        once = refresh.rewrite(TEMPLATE, ["master", "feat/a"], limit=10)

        self.assertEqual(refresh.rewrite(once, ["master", "feat/a"], limit=10), once)

    def test_missing_markers_fail_loudly(self):
        """Silently writing nothing would leave a stale dropdown looking fresh."""
        with self.assertRaises(refresh.MarkersNotFound):
            refresh.rewrite("no markers here\n", ["master"], limit=10)


class WorkflowFileTests(SimpleTestCase):
    def test_the_workflow_carries_the_markers_the_script_needs(self):
        text = WORKFLOW.read_text()

        self.assertIn(refresh.BEGIN, text)
        self.assertIn(refresh.END, text)

    def test_the_committed_dropdown_is_still_valid_yaml_and_offers_master(self):
        workflow = yaml.safe_load(WORKFLOW.read_text())

        options = workflow[True]["workflow_dispatch"]["inputs"]["branch"]["options"]
        self.assertIn("master", options)
        self.assertEqual(
            workflow[True]["workflow_dispatch"]["inputs"]["branch"]["default"], "master"
        )
