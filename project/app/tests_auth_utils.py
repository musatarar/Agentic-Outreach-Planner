"""Shared test base class for authenticated API tests (MUS-37).

Turning on a global ``IsAuthenticated`` breaks every existing ``self.client``
call in the suite. This is the smallest possible migration for them: change
the base class, change nothing else.

**The name and API of :class:`AuthenticatedAPITestCase` are frozen** by
contract MUS-35 section 8.2, because MUS-39 is writing tests against it in
parallel, before this branch merges.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient


class AuthenticatedAPITestCase(TestCase):
    """TestCase whose ``self.client`` is already signed in as an allowlisted user.

    ``force_login`` rather than a real request through
    ``/api/auth/request-link/`` + ``/api/auth/consume/``: these tests are
    about the endpoint under test, not about sign-in, and the magic-link flow
    itself is covered end-to-end in ``tests_auth.py``.

    The client is DRF's ``APIClient`` (as ``rest_framework.test.APITestCase``
    installs) so that the ``format="json"`` calls in the existing suite keep
    working unchanged -- which is the entire point of this class.
    """

    client_class = APIClient

    TEST_EMAIL = "tester@example.com"

    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create(username=self.TEST_EMAIL, email=self.TEST_EMAIL)
        self.user.set_unusable_password()
        self.user.save()
        self.client.force_login(self.user)
