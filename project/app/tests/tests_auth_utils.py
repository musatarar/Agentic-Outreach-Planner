"""Shared test base class for authenticated API tests.

The name and API of :class:`AuthenticatedAPITestCase` are frozen by contract.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from project.app.services.tenancy import add_member
from project.app.tests.tenancy_utils import default_tenant


class AuthenticatedAPITestCase(TestCase):
    """TestCase whose ``self.client`` is a DRF ``APIClient`` already signed in as an
    allowlisted user; the magic-link flow itself is covered in ``tests_auth.py``.

    The user is also enrolled in the suite's default workspace, so the scoped
    endpoints answer with data rather than 403 ``no_tenant``."""

    client_class = APIClient

    TEST_EMAIL = "tester@example.com"

    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create(username=self.TEST_EMAIL, email=self.TEST_EMAIL)
        self.user.set_unusable_password()
        self.user.save()
        self.tenant = default_tenant()
        add_member(self.tenant, self.TEST_EMAIL)
        self.client.force_login(self.user)
