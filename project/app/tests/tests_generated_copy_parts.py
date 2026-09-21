"""The subject/body columns: the composer, its inverse, and the invariant the
schema holds over both."""

from datetime import date

from django.db import IntegrityError, transaction
from django.test import TestCase

from project.app.models import Lead, OutreachGeneratedCopy
from project.app.services.outreach import compose_email, split_email

DRAFT = compose_email("Volume pricing", "Hi Priya,\n\nWorth a look.\n\nBest,\nDana")


def make_lead(lead_id="lead_900"):
    return Lead.objects.create(
        id=lead_id,
        agency_name="Summit Risk Advisors",
        contact_name="Priya",
        contact_email="priya@example.com",
        contact_phone="555-0100",
        state="CA",
        num_producers=3,
        years_in_business=5,
        estimated_book_size_usd=1_400_000,
        stage="active_trial",
        signed_up_date=date(2026, 1, 1),
    )


def make_row(**overrides):
    defaults = dict(
        lead=make_lead(overrides.pop("lead_id", "lead_900")),
        priority=2,
        action_type="power_user_reward",
        reason="A power user worth rewarding.",
    )
    defaults.update(overrides)
    return OutreachGeneratedCopy.objects.create(**defaults)


class ComposeEmailTests(TestCase):
    """The literals here are asserted in frontend/tests/draft-split.test.ts too,
    against its clipboard-only twin of this composer."""

    def test_a_pair_composes_to_the_draft_that_is_stored(self):
        self.assertEqual(
            compose_email("Six closed deals", "You closed 6 deals this quarter."),
            "Subject: Six closed deals\n\nYou closed 6 deals this quarter.",
        )

    def test_composing_flattens_a_multi_line_subject(self):
        self.assertEqual(
            compose_email("Two\n\nlines", "The body."),
            "Subject: Two lines\n\nThe body.",
        )


class SplitEmailTests(TestCase):
    def test_a_composed_draft_splits_back_into_the_pair_it_came_from(self):
        subject, body = split_email(DRAFT)
        self.assertEqual(subject, "Volume pricing")
        self.assertEqual(compose_email(subject, body), DRAFT)

    def test_text_with_no_subject_line_is_all_body(self):
        self.assertEqual(split_email("Hello there."), ("", "Hello there."))

    def test_a_subject_line_with_nothing_under_it_has_no_body(self):
        self.assertEqual(split_email("Subject: Alone"), ("Alone", ""))

    def test_a_subject_with_a_blank_line_in_it_still_round_trips(self):
        # Unflattened, the subject's own blank line would become the separator.
        draft = compose_email("Two\n\nlines", "The body.")
        self.assertEqual(split_email(draft), ("Two lines", "The body."))


class PartsPresentConstraintTests(TestCase):
    def test_a_row_with_no_copy_needs_no_parts(self):
        row = make_row(suggested_copy="", needs_human=True)
        self.assertEqual(row.subject, "")

    def test_a_draft_with_no_parts_behind_it_is_refused(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_row(suggested_copy=DRAFT)

    def test_a_draft_missing_only_its_body_is_refused(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_row(suggested_copy=DRAFT, subject="Volume pricing")

    def test_a_draft_carrying_both_parts_is_stored(self):
        subject, body = split_email(DRAFT)
        row = make_row(suggested_copy=DRAFT, subject=subject, body=body)
        self.assertEqual(row.suggested_copy, compose_email(row.subject, row.body))
