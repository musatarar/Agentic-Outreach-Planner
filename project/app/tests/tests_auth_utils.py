"""Shared test base class for authenticated API tests (MUS-37).

The name and API of :class:`AuthenticatedAPITestCase` are frozen by contract.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient


class AuthenticatedAPITestCase(TestCase):
    """TestCase whose ``self.client`` is a DRF ``APIClient`` already signed in as an
    allowlisted user; the magic-link flow itself is covered in ``tests_auth.py``."""

    client_class = APIClient

    TEST_EMAIL = "tester@example.com"

    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create(username=self.TEST_EMAIL, email=self.TEST_EMAIL)
        self.user.set_unusable_password()
        self.user.save()
        self.client.force_login(self.user)
