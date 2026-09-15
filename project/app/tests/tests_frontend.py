from django.test import TestCase


class FrontendTestCase(TestCase):
    """Minimal frontend tests."""

    def test_root_redirects_to_the_leads_page(self):
        """`/` is not a React route: it sends you to the book of leads."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/leads/")

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


class AuthShellTests(TestCase):
    """The three SPA shells (MUS-38): deliberately public, and each sets the csrftoken
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
        self.assertContains(response, "Review Inbox")
        self.assertIn("csrftoken", response.cookies)

    def test_shells_are_public(self):
        """No shell redirects an anonymous visitor; 302 here means @login_required crept in."""
        for url in ("/signin", "/auth/consume", "/inbox"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_trailing_slash_variants_are_not_routed(self):
        """The React routes carry no trailing slash; /inbox/ must 404, not silently work."""
        for url in ("/signin/", "/auth/consume/", "/inbox/"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
