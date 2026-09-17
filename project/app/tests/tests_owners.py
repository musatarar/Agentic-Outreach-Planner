"""Which user a command works for, and how that user comes into being."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from project.app.services import owners


class OwnerEmailTests(TestCase):
    @override_settings(LOGIN_ALLOWED_EMAILS=set())
    def test_the_demo_owner_is_the_last_resort(self):
        self.assertEqual(owners.owner_email(), owners.DEFAULT_OWNER_EMAIL)

    @override_settings(LOGIN_ALLOWED_EMAILS={"zoe@lockedin.example", "ae@lockedin.example"})
    def test_the_first_address_allowed_to_sign_in_wins_over_the_demo_owner(self):
        self.assertEqual(owners.owner_email(), "ae@lockedin.example")

    @override_settings(LOGIN_ALLOWED_EMAILS={"ae@lockedin.example"})
    def test_an_explicit_email_wins_over_everything_and_is_normalized(self):
        self.assertEqual(owners.owner_email("  BD@Lockedin.Example "), "bd@lockedin.example")


class ResolveOwnerTests(TestCase):
    def test_the_owner_is_created_on_first_use_with_no_usable_password(self):
        owner = owners.resolve_owner("bd@lockedin.example")

        self.assertEqual(owner.username, "bd@lockedin.example")
        self.assertEqual(owner.email, "bd@lockedin.example")
        self.assertFalse(owner.has_usable_password())

    def test_resolving_twice_returns_the_same_user(self):
        first = owners.resolve_owner("bd@lockedin.example")
        second = owners.resolve_owner("bd@lockedin.example")

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(get_user_model().objects.count(), 1)
