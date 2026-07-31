import unittest

from project.app.services.demo_assembly import demo_summary


class DemoAssemblyContractTests(unittest.TestCase):
    @unittest.expectedFailure
    def test_summary_composes_alpha_and_beta(self):
        self.assertEqual(
            demo_summary("  acme   insurance ", 4, 2),
            {"name": "Acme Insurance", "score": 10},
        )
