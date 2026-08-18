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

    def test_leads_shell_renders(self):
        """The leads table's shell loads, uses the SPA template, and sets the CSRF cookie.

        Load-bearing beyond the usual: /leads/ is where signing in lands you
        (`DEFAULT_DESTINATION` in frontend/src/hooks/authDestination.ts), so a
        missing route here is not one broken page — it is a 404 immediately
        after every magic link, on the one path nobody navigates to by hand.
        """
        response = self.client.get("/leads/")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/leads.html")
        self.assertContains(response, "<title>Leads · Locked In</title>", html=False)
        self.assertIn("csrftoken", response.cookies)

    def test_settings_view_renders(self):
        """Settings page loads, uses the SPA shell template, and sets the CSRF cookie."""
        response = self.client.get("/settings/")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/settings.html")
        self.assertContains(response, "Settings")
        self.assertIn("csrftoken", response.cookies)


class AuthShellTests(TestCase):
    """The four SPA shells (MUS-38): deliberately public, and each sets the csrftoken
    cookie because /signin and /auth/consume POST before any other page has run."""

    def test_signin_shell_renders(self):
        response = self.client.get("/signin")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/signin.html")
        self.assertContains(response, "Sign in")
        self.assertIn("csrftoken", response.cookies)

    def test_auth_consume_shell_renders(self):
        response = self.client.get("/auth/consume")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/auth_consume.html")
        self.assertContains(response, "Signing you in")
        self.assertIn("csrftoken", response.cookies)

    def test_inbox_shell_renders(self):
        response = self.client.get("/inbox")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/inbox.html")
        self.assertContains(response, "Triage Inbox")
        self.assertIn("csrftoken", response.cookies)

    def test_done_shell_renders(self):
        response = self.client.get("/done")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/done.html")
        self.assertContains(response, "Done Today")
        self.assertIn("csrftoken", response.cookies)

    def test_shells_are_public(self):
        """No shell redirects an anonymous visitor; 302 here means @login_required crept in."""
        for url in ("/signin", "/auth/consume", "/inbox", "/done"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_trailing_slash_variants_are_not_routed(self):
        """The React routes carry no trailing slash; /inbox/ must 404, not silently work."""
        for url in ("/signin/", "/auth/consume/", "/inbox/", "/done/"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
