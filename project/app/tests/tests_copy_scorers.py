"""Offline unit tests for the deterministic copy checks in ``evals/copy_checks.py`` —
the structural checks the copy eval (MUS-21) applies before the LLM judge."""

import unittest

from evals import copy_checks

# A well-formed email: Subject line, ~90-word body, exactly one CTA sentence.
GOOD_EMAIL = """Subject: Quick idea on your volume pricing

Hi Priya,

Saw you've closed 6 deals and submitted 14 quotes this month — you're clearly
running Sure Lock hard, and you're closing in on that 20-deal mark you
mentioned wanting to hit for volume pricing.

Given that pace, I want to walk you through how our volume tiers kick in so you're
not leaving margin on the table as you scale. It maps cleanly to the close rate
you're already posting.

Would you be open to a quick 15-minute call next week to look at the numbers together?

Best,
Alex"""


class SubjectLineTests(unittest.TestCase):
    def test_present(self):
        ok, subject = copy_checks.check_subject_line(GOOD_EMAIL)
        self.assertTrue(ok)
        self.assertEqual(subject, "Quick idea on your volume pricing")

    def test_absent(self):
        ok, subject = copy_checks.check_subject_line("Hi there,\n\nNo subject here.")
        self.assertFalse(ok)
        self.assertEqual(subject, "")

    def test_case_insensitive_and_leading_space(self):
        ok, subject = copy_checks.check_subject_line("  SUBJECT:  Hello\n\nBody")
        self.assertTrue(ok)
        self.assertEqual(subject, "Hello")


class WordCountTests(unittest.TestCase):
    def test_in_range(self):
        ok, count = copy_checks.check_word_count(GOOD_EMAIL)
        self.assertTrue(ok)
        self.assertTrue(copy_checks.WORD_MIN <= count <= copy_checks.WORD_MAX)

    def test_too_short(self):
        ok, count = copy_checks.check_word_count("Subject: Hi\n\nToo short.")
        self.assertFalse(ok)
        self.assertLess(count, copy_checks.WORD_MIN)

    def test_too_long(self):
        body = "word " * (copy_checks.WORD_MAX + 50)
        ok, count = copy_checks.check_word_count(f"Subject: Long\n\n{body}")
        self.assertFalse(ok)
        self.assertGreater(count, copy_checks.WORD_MAX)

    def test_excludes_subject_line(self):
        # Subject words must not count toward the body length.
        self.assertEqual(copy_checks.count_body_words("Subject: a b c d e\n\none two"), 2)


class PreambleTests(unittest.TestCase):
    def test_clean(self):
        ok, opener = copy_checks.check_no_preamble(GOOD_EMAIL)
        self.assertTrue(ok)
        self.assertEqual(opener, "")

    def test_here_is_preamble(self):
        ok, opener = copy_checks.check_no_preamble("Here is the email:\n\nSubject: X\n\nBody")
        self.assertFalse(ok)
        self.assertEqual(opener, "here is")

    def test_code_fence_preamble(self):
        ok, _ = copy_checks.check_no_preamble("```\nSubject: X\n\nBody\n```")
        self.assertFalse(ok)


class SingleCtaTests(unittest.TestCase):
    def test_exactly_one(self):
        ok, count = copy_checks.check_single_cta(GOOD_EMAIL)
        self.assertTrue(ok)
        self.assertEqual(count, 1)

    def test_zero_cta(self):
        email = "Subject: FYI\n\nJust wanted to share an update. Things are going well."
        ok, count = copy_checks.check_single_cta(email)
        self.assertFalse(ok)
        self.assertEqual(count, 0)

    def test_multiple_cta(self):
        email = (
            "Subject: Two asks\n\n"
            "Can we schedule a call this week? "
            "Also, reply with your availability and let me know a good time."
        )
        ok, count = copy_checks.check_single_cta(email)
        self.assertFalse(ok)
        self.assertGreaterEqual(count, 2)


class RunAllTests(unittest.TestCase):
    def test_good_email_passes_all(self):
        result = copy_checks.run_all(GOOD_EMAIL)
        for name in copy_checks.CHECK_NAMES:
            self.assertTrue(result[name], f"expected check '{name}' to pass on the good email")

    def test_bad_email_fails_all(self):
        bad = "Here is a draft email for you:\n\nThanks for your interest. We are great."
        result = copy_checks.run_all(bad)
        for name in copy_checks.CHECK_NAMES:
            self.assertFalse(result[name], f"expected check '{name}' to fail on the bad email")

    def test_run_all_reports_detail(self):
        result = copy_checks.run_all(GOOD_EMAIL)
        self.assertIn("detail_word_count", result)
        self.assertIn("detail_cta_count", result)
        self.assertEqual(result["detail_cta_count"], 1)
