"""Tests for magic-link authentication (MUS-37).

Grouped by the thing being protected rather than by module:

* token secrecy -- the raw token must never reach the database;
* single use -- a replayed link loses a race, it does not authenticate twice;
* expiry -- and the deliberate expired/invalid distinction;
* the allowlist -- which must not become an account-enumeration oracle.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.db import OperationalError, connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.parsers import JSONParser
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory, APITestCase
from rest_framework.throttling import SimpleRateThrottle

from project.app import urls as app_urls
from project.app.models import LoginToken
from project.app.services import login_links
from project.app.tests_auth_utils import AuthenticatedAPITestCase
from project.app.throttling import LoginEmailRateThrottle

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

    WORKERS = 8

    def test_concurrent_consume_yields_exactly_one_success(self):
        issued = login_links.issue_login_link(ALLOWED)
        start = threading.Barrier(self.WORKERS)

        def redeem(_i):
            try:
                start.wait(timeout=5)
                # SQLite's shared-cache mode raises "table is locked" rather
                # than waiting, and only under test. Retrying is honest here:
                # the property under test is that no number of attempts, by
                # any number of clients, can redeem the same link twice.
                for _attempt in range(20):
                    try:
                        outcome, _token = login_links.consume_login_token(issued.raw_token)
                        return outcome
                    except OperationalError:
                        time.sleep(0.02)
                raise AssertionError("never got a turn at the database")
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=self.WORKERS) as pool:
            outcomes = list(pool.map(redeem, range(self.WORKERS)))

        successes = [o for o in outcomes if o == login_links.ConsumeOutcome.OK]
        self.assertEqual(len(successes), 1, outcomes)
        self.assertEqual(LoginToken.objects.filter(consumed_at__isnull=False).count(), 1)


# ---------------------------------------------------------------------------
# Endpoints -- POST /api/auth/{request-link,consume,logout}/, GET /api/auth/me/
# ---------------------------------------------------------------------------


class AuthAPITestCase(APITestCase):
    """Endpoint tests, with the throttle history reset between them.

    DRF keeps rate-limit history in the default cache, which outlives an
    individual test. Without this, whichever test happens to run 21st in the
    process gets a 429 instead of whatever it was asserting.
    """

    def setUp(self):
        super().setUp()
        cache.clear()

    def tearDown(self):
        cache.clear()
        super().tearDown()


@override_settings(
    LOGIN_ALLOWED_EMAILS={ALLOWED},
    LOGIN_LINK_DELIVERY="console",
    LOGIN_TOKEN_TTL_SECONDS=900,
    LOGIN_RESEND_COOLDOWN_SECONDS=30,
)
class RequestLinkEndpointTests(AuthAPITestCase):
    URL = "/api/auth/request-link/"

    def test_allowlisted_request_mints_a_token_and_returns_the_pinned_shape(self):
        resp = self.client.post(self.URL, {"email": ALLOWED}, format="json")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            set(resp.data.keys()), {"status", "expires_in", "resend_after", "dev_link"}
        )
        self.assertEqual(resp.data["status"], "sent")
        self.assertEqual(resp.data["expires_in"], 900)
        self.assertEqual(resp.data["resend_after"], 30)
        self.assertEqual(LoginToken.objects.filter(email=ALLOWED).count(), 1)

    def test_non_allowlisted_request_is_also_200_but_mints_nothing(self):
        resp = self.client.post(self.URL, {"email": NOT_ALLOWED}, format="json")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "sent")
        self.assertEqual(LoginToken.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_email_is_normalized_before_the_allowlist_check(self):
        resp = self.client.post(self.URL, {"email": "  TESTER@Example.com "}, format="json")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(LoginToken.objects.filter(email=ALLOWED).count(), 1)

    def test_invalid_email_is_a_400_with_the_pinned_code(self):
        resp = self.client.post(self.URL, {"email": "not-an-email"}, format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["code"], "invalid_email")
        self.assertEqual(resp.data["detail"], "Enter a valid email address.")

    def test_missing_email_is_a_400_invalid_email(self):
        resp = self.client.post(self.URL, {}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["code"], "invalid_email")

    def test_audit_columns_are_populated(self):
        self.client.post(
            self.URL, {"email": ALLOWED}, format="json", HTTP_USER_AGENT="Mozilla/5.0 (test)"
        )
        token = LoginToken.objects.get()
        self.assertEqual(token.requested_ip, "127.0.0.1")
        self.assertEqual(token.requested_user_agent, "Mozilla/5.0 (test)")

    def test_method_not_allowed_uses_the_contract_envelope(self):
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 405)
        self.assertEqual(resp.data["code"], "method_not_allowed")
        self.assertIn("detail", resp.data)


@override_settings(LOGIN_ALLOWED_EMAILS={ALLOWED})
class DevLinkExposureTests(AuthAPITestCase):
    """``dev_link`` leaking in production is a full auth bypass.

    Three conditions, all required: DEBUG, console delivery, allowlisted.
    """

    URL = "/api/auth/request-link/"

    @override_settings(DEBUG=True, LOGIN_LINK_DELIVERY="console")
    def test_dev_link_present_in_debug_console_for_an_allowlisted_email(self):
        resp = self.client.post(self.URL, {"email": ALLOWED}, format="json")

        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.data["dev_link"])
        self.assertIn("/auth/consume?token=", resp.data["dev_link"])

    @override_settings(DEBUG=False, LOGIN_LINK_DELIVERY="console")
    def test_dev_link_absent_when_debug_is_false(self):
        resp = self.client.post(self.URL, {"email": ALLOWED}, format="json")

        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data["dev_link"])
        # The link was still minted -- it just isn't handed back over HTTP.
        self.assertEqual(LoginToken.objects.count(), 1)

    @override_settings(
        DEBUG=True,
        LOGIN_LINK_DELIVERY="email",
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    def test_dev_link_absent_when_delivery_is_email(self):
        resp = self.client.post(self.URL, {"email": ALLOWED}, format="json")

        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data["dev_link"])
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(DEBUG=True, LOGIN_LINK_DELIVERY="console")
    def test_dev_link_absent_for_a_non_allowlisted_email(self):
        resp = self.client.post(self.URL, {"email": NOT_ALLOWED}, format="json")

        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data["dev_link"])


@override_settings(LOGIN_ALLOWED_EMAILS={ALLOWED}, DEBUG=False, LOGIN_LINK_DELIVERY="console")
class EnumerationOracleTests(AuthAPITestCase):
    """Contract section 9.18: the identical-response guarantee, scoped.

    ``dev_link`` and "always return the same response" genuinely conflict, and
    the contract resolves it by scoping the guarantee to ``DEBUG=False`` or
    email delivery -- which is exactly where it matters.
    """

    URL = "/api/auth/request-link/"

    def test_allowlisted_and_unknown_emails_are_byte_identical(self):
        allowed = self.client.post(self.URL, {"email": ALLOWED}, format="json")
        unknown = self.client.post(self.URL, {"email": NOT_ALLOWED}, format="json")

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(
            json.dumps(json.loads(allowed.content), sort_keys=True),
            json.dumps(json.loads(unknown.content), sort_keys=True),
        )
        self.assertEqual(allowed.content, unknown.content)


@override_settings(LOGIN_ALLOWED_EMAILS={ALLOWED}, LOGIN_LINK_DELIVERY="console")
class ConsumeEndpointTests(AuthAPITestCase):
    URL = "/api/auth/consume/"

    def _issue(self, email=ALLOWED):
        return login_links.issue_login_link(email)

    def test_happy_path_establishes_a_session(self):
        issued = self._issue()

        resp = self.client.post(self.URL, {"token": issued.raw_token}, format="json")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(resp.data.keys()), {"authenticated", "email", "session_expires_at"})
        self.assertTrue(resp.data["authenticated"])
        self.assertEqual(resp.data["email"], ALLOWED)
        self.assertTrue(resp.data["session_expires_at"].endswith("Z"))

        me = self.client.get("/api/auth/me/")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.data, {"authenticated": True, "email": ALLOWED})

    def test_first_consume_creates_the_django_user_with_no_usable_password(self):
        issued = self._issue()
        self.client.post(self.URL, {"token": issued.raw_token}, format="json")

        user = get_user_model().objects.get(username=ALLOWED)
        self.assertEqual(user.email, ALLOWED)
        self.assertFalse(user.has_usable_password())

    def test_second_consume_reuses_the_same_user(self):
        for _ in range(2):
            issued = self._issue()
            resp = self.client.post(self.URL, {"token": issued.raw_token}, format="json")
            self.assertEqual(resp.status_code, 200)

        self.assertEqual(get_user_model().objects.filter(username=ALLOWED).count(), 1)

    def test_expired_token_is_400_expired_token(self):
        issued = self._issue()
        LoginToken.objects.filter(pk=issued.login_token.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

        resp = self.client.post(self.URL, {"token": issued.raw_token}, format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["code"], "expired_token")
        self.assertEqual(resp.data["detail"], "This sign-in link has expired. Request a new one.")

    def test_replayed_token_is_400_invalid_token(self):
        issued = self._issue()
        self.client.post(self.URL, {"token": issued.raw_token}, format="json")
        self.client.post("/api/auth/logout/", {}, format="json")

        resp = self.client.post(self.URL, {"token": issued.raw_token}, format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["code"], "invalid_token")
        self.assertEqual(resp.data["detail"], "This sign-in link is not valid.")

    def test_unknown_token_is_400_invalid_token(self):
        resp = self.client.post(self.URL, {"token": "nope"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["code"], "invalid_token")

    def test_missing_token_field_is_400_invalid_token(self):
        resp = self.client.post(self.URL, {}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["code"], "invalid_token")

    def test_token_for_a_de_allowlisted_address_no_longer_works(self):
        # Revoking access must not mean hunting down outstanding links.
        issued = self._issue()
        with override_settings(LOGIN_ALLOWED_EMAILS=set()):
            resp = self.client.post(self.URL, {"token": issued.raw_token}, format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["code"], "invalid_token")

    def test_consume_rotates_the_csrf_token(self):
        # See contract section 9.12: a client holding a pre-consume csrftoken
        # will 403 on its first authenticated POST unless it re-reads the cookie.
        self.client.get("/")
        before = self.client.cookies["csrftoken"].value
        issued = self._issue()

        self.client.post(self.URL, {"token": issued.raw_token}, format="json")

        self.assertNotEqual(self.client.cookies["csrftoken"].value, before)


@override_settings(LOGIN_ALLOWED_EMAILS={ALLOWED})
class MeAndLogoutTests(AuthAPITestCase):
    def _sign_in(self):
        issued = login_links.issue_login_link(ALLOWED)
        resp = self.client.post("/api/auth/consume/", {"token": issued.raw_token}, format="json")
        self.assertEqual(resp.status_code, 200)

    def test_me_is_401_with_the_pinned_code_when_anonymous(self):
        resp = self.client.get("/api/auth/me/")

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.data["code"], "not_authenticated")
        self.assertEqual(resp.data["detail"], "Authentication credentials were not provided.")

    def test_logout_clears_the_session(self):
        self._sign_in()

        resp = self.client.post("/api/auth/logout/", {}, format="json")

        self.assertEqual(resp.status_code, 204)
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 401)

    def test_full_round_trip(self):
        with override_settings(DEBUG=True, LOGIN_LINK_DELIVERY="console"):
            requested = self.client.post(
                "/api/auth/request-link/", {"email": ALLOWED}, format="json"
            )
            token = requested.data["dev_link"].split("token=")[1]
            consumed = self.client.post("/api/auth/consume/", {"token": token}, format="json")

        self.assertEqual(consumed.status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me/").data["email"], ALLOWED)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def scoped_rate(scope, rate):
    """Override one entry in ``DEFAULT_THROTTLE_RATES`` for a single test.

    ``override_settings(REST_FRAMEWORK=...)`` does not reach here:
    ``SimpleRateThrottle.THROTTLE_RATES`` is bound to the settings dict once,
    at class-definition time, so a reloaded ``api_settings`` never propagates
    to it. Patching the dict in place is the honest way to say "this rate, for
    this test".
    """
    return mock.patch.dict(SimpleRateThrottle.THROTTLE_RATES, {scope: rate})


@override_settings(LOGIN_ALLOWED_EMAILS={ALLOWED}, DEBUG=False, LOGIN_LINK_DELIVERY="console")
class RequestLinkRateLimitTests(AuthAPITestCase):
    """Without these caps the endpoint is a free spam relay."""

    URL = "/api/auth/request-link/"

    def _request(self, email=ALLOWED, ip="127.0.0.1"):
        return self.client.post(self.URL, {"email": email}, format="json", REMOTE_ADDR=ip)

    @override_settings(LOGIN_RATE_LIMIT_EMAIL="100/hour")
    @scoped_rate("auth_request_ip", "3/hour")
    def test_per_ip_cap_trips_with_the_pinned_envelope(self):
        for _ in range(3):
            self.assertEqual(self._request().status_code, 200)

        resp = self._request()

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.data["code"], "rate_limited")
        self.assertTrue(resp.data["detail"].startswith("Too many login links requested."))
        self.assertIn("Try again in", resp.data["detail"])
        self.assertIsInstance(resp.data["retry_after"], int)
        self.assertGreater(resp.data["retry_after"], 0)
        self.assertIn("Retry-After", resp.headers)

    @override_settings(LOGIN_RATE_LIMIT_EMAIL="100/hour")
    @scoped_rate("auth_request_ip", "2/hour")
    def test_the_429_does_not_depend_on_the_allowlist(self):
        # Rate limiting runs in DRF's initial(), before the view body, so a
        # throttled unknown address and a throttled known one are identical.
        for _ in range(2):
            self._request(email=NOT_ALLOWED)

        blocked_known = self._request(email=ALLOWED)
        blocked_unknown = self._request(email=NOT_ALLOWED)

        self.assertEqual(blocked_known.status_code, 429)
        self.assertEqual(blocked_unknown.status_code, 429)
        self.assertEqual(blocked_known.data["code"], blocked_unknown.data["code"])
        self.assertEqual(blocked_known.data["detail"], blocked_unknown.data["detail"])

    @override_settings(LOGIN_RATE_LIMIT_EMAIL="3/hour")
    def test_per_email_cap_survives_a_change_of_source_ip(self):
        # The IP cap cannot see a distributed mailbomb aimed at one address.
        for i in range(3):
            self.assertEqual(self._request(ip=f"198.51.100.{i}").status_code, 200)

        resp = self._request(ip="198.51.100.99")

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.data["code"], "rate_limited")

    @override_settings(LOGIN_RATE_LIMIT_EMAIL="2/hour")
    def test_per_email_cap_does_not_bleed_between_addresses(self):
        for _ in range(2):
            self._request(email=ALLOWED)

        self.assertEqual(self._request(email=ALLOWED).status_code, 429)
        self.assertEqual(self._request(email=NOT_ALLOWED).status_code, 200)

    @override_settings(LOGIN_RATE_LIMIT_EMAIL="1/hour")
    def test_the_email_cap_is_case_insensitive(self):
        self.assertEqual(self._request(email="TESTER@EXAMPLE.COM").status_code, 200)
        self.assertEqual(self._request(email="tester@example.com").status_code, 429)

    @override_settings(LOGIN_RATE_LIMIT_EMAIL="1/hour")
    def test_a_body_with_no_email_is_a_400_not_a_429(self):
        # Bucketing every malformed body together would let one client's junk
        # lock out everyone else's sign-in.
        for _ in range(5):
            resp = self.client.post(self.URL, {}, format="json")
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.data["code"], "invalid_email")


@override_settings(LOGIN_ALLOWED_EMAILS={ALLOWED})
class ConsumeRateLimitTests(AuthAPITestCase):
    URL = "/api/auth/consume/"

    @scoped_rate("auth_consume_ip", "3/hour")
    def test_brute_forcing_tokens_is_capped_per_ip(self):
        for _ in range(3):
            self.assertEqual(
                self.client.post(self.URL, {"token": "x"}, format="json").status_code, 400
            )

        resp = self.client.post(self.URL, {"token": "x"}, format="json")

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.data["code"], "rate_limited")
        self.assertTrue(resp.data["detail"].startswith("Too many sign-in attempts."))


class ThrottleCacheKeyTests(TestCase):
    """The address must not sit in the throttle cache in the clear."""

    @staticmethod
    def _cache_key(body):
        raw = APIRequestFactory().post("/api/auth/request-link/", body, format="json")
        return LoginEmailRateThrottle().get_cache_key(Request(raw, parsers=[JSONParser()]), None)

    def test_cache_key_is_a_hash_not_the_address(self):
        key = self._cache_key({"email": ALLOWED})

        self.assertIsNotNone(key)
        self.assertNotIn(ALLOWED, key)
        self.assertIn(hashlib.sha256(ALLOWED.encode("utf-8")).hexdigest(), key)

    def test_cache_key_is_case_normalized_before_hashing(self):
        self.assertEqual(
            self._cache_key({"email": " TESTER@Example.COM "}), self._cache_key({"email": ALLOWED})
        )

    def test_cache_key_is_none_without_an_email(self):
        self.assertIsNone(self._cache_key({}))

    def test_a_non_dict_body_does_not_crash_the_throttle(self):
        self.assertIsNone(self._cache_key(["nope"]))


# ---------------------------------------------------------------------------
# Global authentication -- contract sections 5.1.4, 9.4 and 9.12
# ---------------------------------------------------------------------------


class UnauthenticatedAccessTests(APITestCase):
    """Contract 9.4: **401, not 403.**

    DRF picks 401 vs 403 by asking the first authenticator for an
    ``authenticate_header``; stock ``SessionAuthentication`` returns None, so
    anonymous requests get 403. MUS-38 would then ship 401 handling that never
    fires -- and every one of its route-guard tests would still pass, because
    they mock the 401. `SessionAuthenticationWith401` is the whole fix, and
    these assertions are what keep it in place.
    """

    PREVIOUSLY_PUBLIC = [
        ("get", "/api/leads/"),
        ("get", "/api/outreach/"),
        ("get", "/api/reports/"),
        ("get", "/api/review-queue/"),
        ("get", "/api/review-decisions/"),
        ("get", "/api/llm/catalog/"),
        ("get", "/api/llm/config/"),
        ("post", "/api/outreach/run/"),
        ("post", "/api/review-decisions/"),
        ("post", "/api/llm/config/test/"),
    ]

    def test_every_previously_public_endpoint_is_401_when_anonymous(self):
        for method, url in self.PREVIOUSLY_PUBLIC:
            with self.subTest(url=url):
                resp = getattr(self.client, method)(url)
                self.assertEqual(resp.status_code, 401, f"{method.upper()} {url}")
                self.assertEqual(resp.data["code"], "not_authenticated")

    def test_logout_is_401_when_anonymous(self):
        # One endpoint from MUS-37's own surface.
        resp = self.client.post("/api/auth/logout/", {}, format="json")

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.data["code"], "not_authenticated")

    def test_the_401_carries_a_www_authenticate_header(self):
        # This header is *why* the status is 401 rather than 403.
        resp = self.client.get("/api/leads/")
        self.assertEqual(resp.headers["WWW-Authenticate"], 'Session realm="api"')

    def test_queue_surface_is_401_when_anonymous(self):
        """Contract 9.4 wants this for MUS-39's surface too.

        `/api/queue/` does not exist on this branch -- MUS-39 uncomments it in
        `urls.py`. Asserting it here rather than leaving it to integration is
        the point: a 403 there is invisible until the route guard silently
        stops working.
        """
        resp = self.client.get("/api/queue/")
        if resp.status_code == 404:
            self.skipTest("/api/queue/ arrives with MUS-39; this assertion activates then")
        self.assertEqual(resp.status_code, 401)

    def test_the_allow_any_exemption_list_is_exactly_three_endpoints(self):
        exempt = {
            pattern.name
            for pattern in app_urls.urlpatterns
            if AllowAny in getattr(pattern.callback, "cls", type(None)).permission_classes
        }
        self.assertEqual(exempt, {"auth-request-link", "auth-consume", "auth-me"})

    def test_the_html_shells_stay_public(self):
        # They render an empty #root. @login_required here would break
        # tests_frontend.py and hand users a Django 302 instead of MUS-38's
        # designed sign-in redirect.
        for url in ("/", "/reports/", "/next-actions/", "/settings/"):
            with self.subTest(url=url):
                self.assertEqual(Client().get(url).status_code, 200)


@override_settings(LOGIN_ALLOWED_EMAILS={ALLOWED})
class CsrfAcrossTheLoginBoundaryTests(TestCase):
    """Contract 9.12: ``login()`` rotates the CSRF token.

    A client that captured `csrftoken` before consuming the link and reuses it
    afterwards 403s on its first authenticated POST. That is correct behaviour
    and the frontend must re-read the cookie per request -- this test is what
    stops someone "optimising" it into a module-level constant.
    """

    def test_stale_csrf_token_is_rejected_and_the_fresh_one_is_accepted(self):
        client = Client(enforce_csrf_checks=True)
        client.get("/")  # @ensure_csrf_cookie shell
        stale = client.cookies["csrftoken"].value

        issued = login_links.issue_login_link(ALLOWED)
        consumed = client.post(
            "/api/auth/consume/",
            json.dumps({"token": issued.raw_token}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=stale,
        )
        self.assertEqual(consumed.status_code, 200)

        fresh = client.cookies["csrftoken"].value
        self.assertNotEqual(fresh, stale)

        with_stale = client.post(
            "/api/review-decisions/",
            json.dumps({}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=stale,
        )
        self.assertEqual(with_stale.status_code, 403)
        self.assertEqual(with_stale.json()["code"], "csrf_failed")

        with_fresh = client.post(
            "/api/review-decisions/",
            json.dumps({}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=fresh,
        )
        self.assertNotEqual(with_fresh.status_code, 403)


class AuthenticatedAPITestCaseTests(AuthenticatedAPITestCase):
    """The helper MUS-39 is writing against right now, in parallel.

    Its name and API are frozen by contract 8.2, so it gets its own test
    rather than being only implicitly exercised by the suites that inherit it.
    """

    def test_the_client_is_signed_in(self):
        resp = self.client.get("/api/auth/me/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["email"], self.TEST_EMAIL)

    def test_the_user_has_no_usable_password(self):
        self.assertFalse(self.user.has_usable_password())

    def test_json_format_requests_still_work(self):
        # The reason client_class is APIClient: 49 existing call sites pass
        # format="json", and none of their bodies were allowed to change.
        resp = self.client.post("/api/review-decisions/", {}, format="json")
        self.assertEqual(resp.status_code, 400)
