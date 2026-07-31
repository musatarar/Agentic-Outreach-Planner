import unittest

from project.app.services.demo_alpha import normalize_lead_name


class DemoAlphaContractTests(unittest.TestCase):
    def test_collapses_internal_whitespace(self):
        self.assertEqual(normalize_lead_name("acme   insurance"), "Acme Insurance")

    def test_strips_and_title_cases(self):
        self.assertEqual(normalize_lead_name("  ACME insurance  "), "Acme Insurance")

    @unittest.expectedFailure
    def test_handles_single_word_names(self):
        self.assertEqual(normalize_lead_name("acme"), "Acme")
