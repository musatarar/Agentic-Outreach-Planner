import unittest

from project.app.services.demo_beta import score_lead


class DemoBetaContractTests(unittest.TestCase):
    @unittest.expectedFailure
    def test_score_weights_deals_over_quotes(self):
        self.assertEqual(score_lead(4, 2), 10)
