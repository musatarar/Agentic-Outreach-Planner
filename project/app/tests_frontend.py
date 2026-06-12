from django.test import TestCase


class FrontendTestCase(TestCase):
    """Minimal frontend tests."""

    def test_index_page_returns_200(self):
        """Test that the index page loads successfully."""
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)

    def test_index_page_contains_title(self):
        """Test that the index page contains 'Outreach Planner'."""
        response = self.client.get('/')
        self.assertContains(response, 'Outreach Planner')
