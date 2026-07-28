"""Tests for magic-link authentication (MUS-37).

Grouped by the thing being protected rather than by module:

* token secrecy -- the raw token must never reach the database;
* single use -- a replayed link loses a race, it does not authenticate twice;
* expiry -- and the deliberate expired/invalid distinction;
* the allowlist -- which must not become an account-enumeration oracle.
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.db import connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from project.app.models import LoginToken
from project.app.services import login_links

ALLOWED = "tester@example.com"
NOT_ALLOWED = "stranger@example.com"


@override_settings(LOGIN_ALLOWED_EMAILS={ALLOWED})
class TokenIssueTests(TestCase):
    """Minting a link: entropy, hashing, expiry, and the URL handed out."""

    def test_raw_token_is_never_persisted(self):
        issued = login_links.issue_login_link(ALLOWED)

        stored = LoginToken.objects.get(pk=issued.login_token.pk)
        self.assertNotIn(issued.raw_token, stored.token_hash)
        self.assertEqual(
            stored.token_hash,
            hashlib.sha256(issued.raw_token.encode("utf-8")).hexdigest(),
        )
        # And no other column smuggles it in either.
        row = LoginToken.objects.filter(pk=stored.pk).values().first()
        self.assertNotIn(issued.raw_token, " ".join(str(v) for v in row.values()))

    def test_token_has_256_bits_of_entropy(self):
        # token_urlsafe(32) is 32 bytes base64url-encoded without padding.
        tokens = {login_links.generate_raw_token() for _ in range(50)}
        self.assertEqual(len(tokens), 50)
        for token in tokens:
            self.assertEqual(len(token), 43)

    def test_token_hash_is_64_hex_chars(self):
        digest = login_links.hash_token("anything")
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, digest.lower())

    @override_settings(LOGIN_TOKEN_TTL_SECONDS=900)
    def test_expiry_comes_from_the_configured_ttl(self):
        before = timezone.now()
        issued = login_links.issue_login_link(ALLOWED)

        delta = issued.login_token.expires_at - before
        self.assertGreater(delta, timedelta(seconds=890))
        self.assertLessEqual(delta, timedelta(seconds=901))
        self.assertEqual(issued.expires_in, 900)

    def test_email_is_normalized_on_the_row(self):
        issued = login_links.issue_login_link("  TESTER@Example.COM ")
        self.assertEqual(issued.login_token.email, ALLOWED)

    def test_audit_fields_are_recorded_and_truncated(self):
        issued = login_links.issue_login_link(ALLOWED, ip="203.0.113.7", user_agent="x" * 400)
        self.assertEqual(issued.login_token.requested_ip, "203.0.113.7")
        self.assertEqual(len(issued.login_token.requested_user_agent), 255)

    @override_settings(LOGIN_LINK_BASE_URL="https://example.test/")
    def test_link_points_at_the_consume_route_with_the_raw_token(self):
        issued = login_links.issue_login_link(ALLOWED)
        self.assertEqual(
            issued.link,
            f"https://example.test/auth/consume?token={issued.raw_token}",
        )

    def test_str_is_readable(self):
        issued = login_links.issue_login_link(ALLOWED)
        self.assertIn(ALLOWED, str(issued.login_token))
        self.assertIn("expires", str(issued.login_token))


@override_settings(LOGIN_ALLOWED_EMAILS={ALLOWED})
class AllowlistTests(TestCase):
    def test_allowlist_is_case_and_whitespace_insensitive(self):
        self.assertTrue(login_links.is_allowed("  TESTER@EXAMPLE.COM  "))
        self.assertTrue(login_links.is_allowed(ALLOWED))
        self.assertFalse(login_links.is_allowed(NOT_ALLOWED))

    def test_discard_token_costs_the_same_work_and_stores_nothing(self):
        digest = login_links.burn_discard_token()
        self.assertEqual(len(digest), 64)
        self.assertEqual(LoginToken.objects.count(), 0)


@override_settings(LOGIN_ALLOWED_EMAILS={ALLOWED})
class DeliveryTests(TestCase):
    """Delivery is stubbed; console mode is the demo path."""

    @override_settings(LOGIN_LINK_DELIVERY="console")
    def test_console_delivery_logs_the_link_and_sends_no_mail(self):
        issued = login_links.issue_login_link(ALLOWED)
        with self.assertLogs("project.app.services.login_links", level="INFO") as logs:
            login_links.deliver_login_link(issued)

        self.assertTrue(any(issued.link in line for line in logs.output))
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(
        LOGIN_LINK_DELIVERY="email",
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    def test_email_delivery_uses_the_django_email_backend(self):
        issued = login_links.issue_login_link(ALLOWED)
        login_links.deliver_login_link(issued)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [ALLOWED])
        self.assertIn(issued.link, mail.outbox[0].body)

    @override_settings(
        LOGIN_LINK_DELIVERY="email",
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    def test_email_delivery_failure_is_swallowed(self):
        # A bounce must not change the HTTP response -- that would re-open the
        # enumeration oracle the identical-response rule exists to close.
        issued = login_links.issue_login_link(ALLOWED)
        with (
            mock.patch.object(login_links, "send_mail", side_effect=OSError("smtp down")),
            self.assertLogs("project.app.services.login_links", level="ERROR") as logs,
        ):
            login_links.deliver_login_link(issued)  # must not raise

        self.assertTrue(any("Failed to email" in line for line in logs.output))
        self.assertEqual(len(mail.outbox), 0)


@override_settings(LOGIN_ALLOWED_EMAILS={ALLOWED})
class TokenConsumeTests(TestCase):
    def test_happy_path_marks_the_token_consumed_once(self):
        issued = login_links.issue_login_link(ALLOWED)

        outcome, token = login_links.consume_login_token(issued.raw_token)

        self.assertEqual(outcome, login_links.ConsumeOutcome.OK)
        self.assertIsNotNone(token)
        self.assertEqual(token.email, ALLOWED)
        token.refresh_from_db()
        self.assertIsNotNone(token.consumed_at)

    def test_replayed_token_is_rejected(self):
        issued = login_links.issue_login_link(ALLOWED)
        login_links.consume_login_token(issued.raw_token)

        outcome, token = login_links.consume_login_token(issued.raw_token)

        self.assertEqual(outcome, login_links.ConsumeOutcome.INVALID)
        self.assertIsNone(token)

    def test_unknown_token_is_invalid(self):
        outcome, token = login_links.consume_login_token("not-a-real-token")
        self.assertEqual(outcome, login_links.ConsumeOutcome.INVALID)
        self.assertIsNone(token)

    def test_expired_token_is_distinguishable_from_invalid(self):
        issued = login_links.issue_login_link(ALLOWED)
        LoginToken.objects.filter(pk=issued.login_token.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

        outcome, token = login_links.consume_login_token(issued.raw_token)

        self.assertEqual(outcome, login_links.ConsumeOutcome.EXPIRED)
        self.assertIsNone(token)

    def test_expired_and_already_consumed_reads_as_invalid(self):
        # Once redeemed, a token stops being "expired" -- there is nothing to
        # re-request, and "invalid" is the honest answer.
        issued = login_links.issue_login_link(ALLOWED)
        login_links.consume_login_token(issued.raw_token)
        LoginToken.objects.filter(pk=issued.login_token.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

        outcome, _ = login_links.consume_login_token(issued.raw_token)

        self.assertEqual(outcome, login_links.ConsumeOutcome.INVALID)

    def test_consume_does_not_touch_other_tokens(self):
        first = login_links.issue_login_link(ALLOWED)
        second = login_links.issue_login_link(ALLOWED)

        login_links.consume_login_token(first.raw_token)

        second.login_token.refresh_from_db()
        self.assertIsNone(second.login_token.consumed_at)


@override_settings(LOGIN_ALLOWED_EMAILS={ALLOWED})
class ConcurrentConsumeTests(TransactionTestCase):
    """The single highest-value test in this module.

    A read-then-write implementation passes every sequential test above and
    still authenticates a replayed link twice under concurrency. Only real
    threads, on real connections, catch that -- hence ``TransactionTestCase``.
    """

    def test_concurrent_consume_yields_exactly_one_success(self):
        issued = login_links.issue_login_link(ALLOWED)

        def redeem(_i):
            try:
                outcome, _token = login_links.consume_login_token(issued.raw_token)
                return outcome
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(redeem, range(8)))

        successes = [o for o in outcomes if o == login_links.ConsumeOutcome.OK]
        self.assertEqual(len(successes), 1, outcomes)
        self.assertEqual(LoginToken.objects.filter(consumed_at__isnull=False).count(), 1)
