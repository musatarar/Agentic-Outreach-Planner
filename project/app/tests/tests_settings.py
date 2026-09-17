"""The host and CSRF-origin allowlists, read from the environment (see
`.env.example`)."""

import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, SimpleTestCase, TestCase, override_settings

from project import settings as project_settings

CROSS_SCHEME_ORIGIN = "https://demo.example.test"


class EnvListTests(SimpleTestCase):
    """`_env_list` backs both allowlists, so its edges are both allowlists'."""

    def test_it_splits_on_commas_and_strips_each_entry(self):
        with mock.patch.dict(os.environ, {"DJANGO_ALLOWED_HOSTS": " a.test , b.test "}):
            self.assertEqual(
                project_settings._env_list("DJANGO_ALLOWED_HOSTS"), ["a.test", "b.test"]
            )

    def test_blank_entries_and_a_blank_value_read_as_empty(self):
        with mock.patch.dict(os.environ, {"DJANGO_ALLOWED_HOSTS": " , "}):
            self.assertEqual(project_settings._env_list("DJANGO_ALLOWED_HOSTS"), [])

    def test_an_unset_variable_reads_as_empty(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(project_settings._env_list("DJANGO_ALLOWED_HOSTS"), [])


class EnvBoolTests(SimpleTestCase):
    """`_env_bool` decides whether a run may spend, so it never guesses."""

    def _read(self, raw):
        with mock.patch.dict(os.environ, {"ACTIONS_LLM_DRY_RUN": raw}):
            return project_settings._env_bool("ACTIONS_LLM_DRY_RUN", False)

    def test_every_documented_true_spelling_reads_as_true(self):
        for raw in ("1", "true", "True", "TRUE", " yes ", "on"):
            with self.subTest(raw=raw):
                self.assertIs(self._read(raw), True)

    def test_every_documented_false_spelling_reads_as_false(self):
        for raw in ("0", "false", "False", "no", "off"):
            with self.subTest(raw=raw):
                self.assertIs(self._read(raw), False)

    def test_a_blank_or_unset_value_falls_back_to_the_default(self):
        self.assertIs(self._read(""), False)
        self.assertIs(self._read("   "), False)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIs(project_settings._env_bool("ACTIONS_LLM_DRY_RUN", True), True)

    def test_a_value_that_is_neither_is_refused_by_name(self):
        for raw in ("maybe", "2", "tru"):
            with self.subTest(raw=raw):
                with self.assertRaises(ImproperlyConfigured) as caught:
                    self._read(raw)
                self.assertIn("ACTIONS_LLM_DRY_RUN", str(caught.exception))


class CsrfTrustedOriginTests(TestCase):
    """TLS terminating in front of the app makes the browser send an https
    `Origin` while Django sees http: every authenticated POST is a 403 until
    that origin is trusted."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="tester@example.test")

    def _post_logout(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        client.get("/leads/")  # mints the csrftoken cookie
        token = client.cookies["csrftoken"].value
        return client.post(
            "/api/auth/logout/",
            HTTP_ORIGIN=CROSS_SCHEME_ORIGIN,
            HTTP_X_CSRFTOKEN=token,
        )

    def test_a_cross_scheme_origin_is_rejected_when_untrusted(self):
        with override_settings(CSRF_TRUSTED_ORIGINS=[]):
            self.assertEqual(self._post_logout().status_code, 403)

    def test_trusting_the_origin_lets_the_post_through(self):
        with override_settings(CSRF_TRUSTED_ORIGINS=[CROSS_SCHEME_ORIGIN]):
            self.assertEqual(self._post_logout().status_code, 204)

    def test_the_setting_is_read_from_the_environment(self):
        self.assertEqual(
            project_settings.CSRF_TRUSTED_ORIGINS,
            project_settings._env_list("DJANGO_CSRF_TRUSTED_ORIGINS"),
        )
