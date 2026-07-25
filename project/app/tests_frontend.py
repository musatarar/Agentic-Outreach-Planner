from django.test import TestCase


class FrontendTestCase(TestCase):
    """Minimal frontend tests."""

    def test_index_page_returns_200(self):
        """Test that the index page loads successfully."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

    def test_index_page_contains_title(self):
        """Test that the index page contains 'Outreach Planner'."""
        response = self.client.get("/")
        self.assertContains(response, "Outreach Planner")

    def test_reports_page_returns_200_with_title(self):
        """Test that the reports page loads and contains its title."""
        response = self.client.get("/reports/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Outreach Reports")

    def test_next_actions_page_returns_200_with_title(self):
        """Test that the BD Dashboard page loads and contains its title."""
        response = self.client.get("/next-actions/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "BD Dashboard")
