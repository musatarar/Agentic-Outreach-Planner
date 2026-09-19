"""The subject/body columns: the invariant they hold and the backfill that
brings rows written before them up to it."""

from datetime import date
from io import StringIO

from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase

from project.app.management.commands.backfill_generated_copy_parts import unsplit_rows
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


class SplitEmailTests(TestCase):
    def test_a_composed_draft_splits_back_into_the_pair_it_came_from(self):
        subject, body = split_email(DRAFT)
        self.assertEqual(subject, "Volume pricing")
        self.assertEqual(compose_email(subject, body), DRAFT)

    def test_text_with_no_subject_line_is_all_body(self):
        self.assertEqual(split_email("Hello there."), ("", "Hello there."))

    def test_a_subject_line_with_nothing_under_it_has_no_body(self):
        self.assertEqual(split_email("Subject: Alone"), ("Alone", ""))


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


class UnsplitRowSelectionTests(TestCase):
    """Which rows the backfill claims. Its other half -- a draft with no parts
    -- cannot be written once 0007 is applied, which is the point of 0007; the
    splitting itself is :class:`SplitEmailTests`.
    """

    def test_a_row_carrying_both_parts_is_not_claimed(self):
        subject, body = split_email(DRAFT)
        make_row(suggested_copy=DRAFT, subject=subject, body=body)
        self.assertFalse(unsplit_rows().exists())

    def test_an_edit_with_no_parts_recorded_is_claimed(self):
        row = make_row(suggested_copy="", edited_copy=DRAFT)
        self.assertEqual([item.pk for item in unsplit_rows()], [row.pk])

    def test_a_row_with_nothing_stored_is_not_claimed(self):
        make_row(suggested_copy="", needs_human=True)
        self.assertFalse(unsplit_rows().exists())


class BackfillCommandTests(TestCase):
    """The command over the half that stays reachable: a reviewer's edit, which
    0007 does not constrain because an edit may legitimately have no subject
    line."""

    def run_command(self, *args):
        out = StringIO()
        call_command("backfill_generated_copy_parts", *args, stdout=out)
        return out.getvalue()

    def test_it_splits_an_edit_whose_parts_were_never_recorded(self):
        row = make_row(suggested_copy="", edited_copy=DRAFT)

        self.run_command()

        row.refresh_from_db()
        self.assertEqual(row.edited_subject, "Volume pricing")
        self.assertEqual(compose_email(row.edited_subject, row.edited_body), DRAFT)

    def test_it_splits_every_row_it_finds(self):
        for index in range(5):
            make_row(lead_id=f"lead_9{index:02d}", suggested_copy="", edited_copy=DRAFT)

        self.run_command()

        self.assertFalse(unsplit_rows().exists())

    def test_a_second_run_changes_nothing(self):
        make_row(suggested_copy="", edited_copy=DRAFT)
        self.run_command()

        self.assertIn("split 0 row(s)", self.run_command())

    def test_an_edit_that_splits_to_nothing_does_not_stall_the_walk(self):
        # It still matches the filter the walk reads, so only the pk cursor
        # makes this terminate.
        make_row(lead_id="lead_blank", suggested_copy="", edited_copy="Subject:")
        make_row(suggested_copy="", edited_copy=DRAFT)

        self.run_command()

        self.assertEqual(
            OutreachGeneratedCopy.objects.get(lead_id="lead_900").edited_subject,
            "Volume pricing",
        )

    def test_it_leaves_a_row_that_already_carries_its_parts_alone(self):
        row = make_row(suggested_copy="", edited_copy=DRAFT, edited_body="A body of its own.")

        self.run_command()

        row.refresh_from_db()
        self.assertEqual(row.edited_body, "A body of its own.")

    def test_dry_run_reports_without_writing(self):
        row = make_row(suggested_copy="", edited_copy=DRAFT)

        self.assertIn("1 row(s) would be split", self.run_command("--dry-run"))

        row.refresh_from_db()
        self.assertEqual(row.edited_subject, "")
