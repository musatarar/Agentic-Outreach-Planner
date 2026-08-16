"""Env-driven settings parsing in project/settings.py.

The numeric helpers are covered in tests_llm_runtime.py alongside the planner
constants they feed; this file covers the list-valued ones, which decide who
Django will answer for (``ALLOWED_HOSTS``) and which origins may POST
(``CSRF_TRUSTED_ORIGINS``). Both are set from the environment by the preview
workflow (.github/workflows/preview.yml) with a hostname that only exists at
runtime, so their parsing has to tolerate whatever that step interpolates.
"""

import os
from unittest import mock

from django.conf import settings as django_settings
from django.test import SimpleTestCase

from project import settings as project_settings


class EnvCsvTests(SimpleTestCase):
    """`_env_csv` turns a comma-separated env var into a clean list."""

    def test_unset_reads_as_empty(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(project_settings._env_csv("DJANGO_ALLOWED_HOSTS"), [])

    def test_blank_reads_as_empty(self):
        # .env.example ships this key with an empty value, and docker-compose
        # passes `${VAR:-}` through for anything the operator never set.
        with mock.patch.dict(os.environ, {"DJANGO_ALLOWED_HOSTS": "   "}, clear=False):
            self.assertEqual(project_settings._env_csv("DJANGO_ALLOWED_HOSTS"), [])

    def test_splits_and_strips(self):
        env = {"DJANGO_ALLOWED_HOSTS": " example.com , www.example.com "}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(
                project_settings._env_csv("DJANGO_ALLOWED_HOSTS"),
                ["example.com", "www.example.com"],
            )

    def test_drops_empty_items(self):
        # A trailing comma is what you get from shell string-building like
        # "$HOST,127.0.0.1,localhost" when $HOST came back empty.
        with mock.patch.dict(os.environ, {"DJANGO_ALLOWED_HOSTS": ",a.com,"}, clear=False):
            self.assertEqual(project_settings._env_csv("DJANGO_ALLOWED_HOSTS"), ["a.com"])


class CsrfTrustedOriginsTests(SimpleTestCase):
    """The setting exists and defaults to empty.

    Django only compares the browser's ``Origin`` against ``scheme://host`` as
    the *server* sees the request. Behind an https tunnel in front of
    `runserver` the scheme never matches, so every POST fails CSRF unless the
    public origin is named here — that is the whole reason this setting is
    wired to the environment.
    """

    def test_defaults_to_empty(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(project_settings._env_csv("DJANGO_CSRF_TRUSTED_ORIGINS"), [])

    def test_is_a_list_of_full_origins(self):
        env = {"DJANGO_CSRF_TRUSTED_ORIGINS": "https://abc-def.trycloudflare.com"}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(
                project_settings._env_csv("DJANGO_CSRF_TRUSTED_ORIGINS"),
                ["https://abc-def.trycloudflare.com"],
            )

    def test_the_settings_module_defines_it(self):
        # Django ships a global default of [], so a missing (or misspelled)
        # assignment in project/settings.py is silent: CSRF just keeps
        # rejecting every tunnelled POST. Assert the module itself defines it.
        self.assertTrue(
            hasattr(project_settings, "CSRF_TRUSTED_ORIGINS"),
            "project/settings.py must set CSRF_TRUSTED_ORIGINS itself",
        )
        self.assertIsInstance(django_settings.CSRF_TRUSTED_ORIGINS, list)
