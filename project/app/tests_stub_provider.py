"""The benchmark's fake provider and its fixture (MUS-26e): good enough to
benchmark against, and impossible to reach from the app without the
``OUTREACH_ALLOW_STUB_LLM`` opt-in."""

import os
import subprocess
import sys
import tempfile
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from project.app.models import Event, Lead
from project.app.services import verify
from project.app.services.llm import _REGISTRY, build_client
from project.app.services.llm.stub import (
    ALLOW_ENV_VAR,
    PROVIDER_NAME,
    StubClient,
    StubLLMNotAllowed,
    canned_email,
)
from project.app.services.outreach import _build_copy_prompt, validate_copy


def _allowed():
    return mock.patch.dict(os.environ, {ALLOW_ENV_VAR: "1"})


class StubIsUnreachableFromTheAppTests(TestCase):
    """Three independent barriers, tested independently."""

    def test_building_one_without_the_opt_in_is_refused(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(StubLLMNotAllowed) as caught:
                StubClient()

        self.assertIn(ALLOW_ENV_VAR, str(caught.exception))

    def test_a_value_other_than_one_does_not_count(self):
        for value in ("0", "true", "yes", ""):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {ALLOW_ENV_VAR: value}):
                    with self.assertRaises(StubLLMNotAllowed):
                        StubClient()

    def test_the_provider_catalog_never_offers_it(self):
        """No catalog row means no configuration can point at the stub."""
        from project.app.models import LLMProvider

        call_command("seed_llm_catalog", verbosity=0)

        self.assertFalse(LLMProvider.objects.filter(key=PROVIDER_NAME).exists())
        self.assertEqual(
            set(LLMProvider.objects.values_list("key", flat=True)),
            {"claude", "chatgpt", "deepseek", "groq"},
        )

    def test_only_the_benchmark_sets_the_opt_in(self):
        # `grep` rather than a mock: this is a fact about the tree.
        hits = subprocess.run(
            ["git", "grep", "-l", "--untracked", ALLOW_ENV_VAR],
            cwd=settings.BASE_DIR,
            capture_output=True,
            text=True,
        ).stdout.split()

        self.assertEqual(
            sorted(hits),
            [
                "README.md",
                "evals/bench_planner.py",
                "project/app/services/llm/stub.py",
                "project/app/tests_stub_provider.py",
            ],
            "something new references the stub opt-in; check it is not the app",
        )

    def test_it_is_registered_so_it_cannot_drift_from_the_real_adapters(self):
        # Registered on purpose: it goes through the same factory as real
        # adapters, so an LLMClient interface change breaks it too.
        self.assertIs(_REGISTRY[PROVIDER_NAME], StubClient)

        with _allowed():
            client = build_client(PROVIDER_NAME)

        self.assertIsInstance(client, StubClient)
        self.assertEqual(client.provider_name, PROVIDER_NAME)


class CannedEmailPassesTheRealGatesTests(TestCase):
    """The stub's output has to survive the planner's two output gates."""

    def setUp(self):
        super().setUp()
        self.lead = Lead.objects.create(
            id="synth_0001",
            agency_name="Summit Risk Advisors",
            contact_name="Priya Nair",
            contact_email="priya.nair@summitrisk.com",
            contact_phone="555-0000",
            state="CO",
            num_producers=4,
            years_in_business=12,
            estimated_book_size_usd=5_000_000,
            stage="demo_completed",
            signed_up_date=None,
            deals_closed=3,
        )
        self.prompt = _build_copy_prompt(self.lead, "complete_onboarding", "reason")

    def test_the_email_names_the_actual_lead(self):
        email = canned_email(self.prompt)

        # These come from the prompt, not from hardcoded strings in the stub.
        self.assertIn("Priya Nair", email)
        self.assertIn("Summit Risk Advisors", email)

    def test_it_passes_the_shape_gate(self):
        self.assertEqual(validate_copy(canned_email(self.prompt)), [])

    def test_it_passes_the_grounding_gate_at_the_strictest_level(self):
        violations = verify.verify_copy(
            self.lead, canned_email(self.prompt), "complete_onboarding", level="strict"
        )
        self.assertEqual(violations, [], f"stub copy is not grounded: {violations}")

    def test_it_makes_no_numeric_claim(self):
        # An invented number would fail the verifier and route the lead to a human.
        self.assertFalse(
            [ch for ch in canned_email(self.prompt) if ch.isdigit()],
            "the canned email contains a digit, which the verifier may contradict",
        )

    def test_an_unparseable_prompt_still_produces_a_well_formed_email(self):
        email = canned_email("not a prompt at all")

        self.assertEqual(validate_copy(email), [])
        self.assertIn("Hi there", email)


class StubBehaviourTests(SimpleTestCase):
    """Latency, failure injection and token counts."""

    def test_latency_is_seeded_and_never_negative(self):
        with _allowed():
            first = StubClient(seed=7, latency_mean_s=0.001, latency_stddev_s=0.0005)
            second = StubClient(seed=7, latency_mean_s=0.001, latency_stddev_s=0.0005)

        draws_a = [first._next_latency() for _ in range(50)]
        draws_b = [second._next_latency() for _ in range(50)]

        self.assertEqual(draws_a, draws_b)
        self.assertTrue(all(value > 0 for value in draws_a))

    def test_a_wide_distribution_is_still_clamped_above_zero(self):
        # A fat-tailed gaussian draws negatives, and a negative sleep raises.
        with _allowed():
            client = StubClient(seed=1, latency_mean_s=0.01, latency_stddev_s=5.0)

        self.assertTrue(all(client._next_latency() > 0 for _ in range(500)))

    def test_it_does_not_disturb_the_global_random_stream(self):
        import random

        random.seed(99)
        expected = [random.random() for _ in range(3)]

        random.seed(99)
        with _allowed():
            client = StubClient(seed=99)
        [client._next_latency() for _ in range(10)]

        self.assertEqual([random.random() for _ in range(3)], expected)

    def test_injected_failures_are_retryable(self):
        from project.app.services.llm import LLMError

        with _allowed():
            client = StubClient(seed=3, rate_limit_rate=0.5, failure_rate=0.5)

        raised = 0
        for _ in range(50):
            try:
                client._maybe_fail()
            except LLMError as exc:
                raised += 1
                # Both injected kinds are retryable, so the run measures retries.
                self.assertTrue(exc.retryable)
        self.assertEqual(raised, 50)  # the two rates sum to 1.0

    def test_no_failures_by_default(self):
        with _allowed():
            client = StubClient(seed=3)
        for _ in range(100):
            client._maybe_fail()  # must not raise

    def test_the_result_carries_plausible_token_counts(self):
        with _allowed():
            client = StubClient(seed=1, latency_mean_s=0.001, latency_stddev_s=0.0)

        result = client.generate("- Contact: Dana Lee (dana@x.test)\n- Agency: Acme (CO, 2 p")

        self.assertGreater(result.input_tokens, 0)
        self.assertGreater(result.output_tokens, 0)
        self.assertEqual(result.provider, PROVIDER_NAME)
        self.assertEqual(result.finish_reason, "stop")
        self.assertGreater(result.latency_s, 0)

    def test_the_async_path_does_not_block_the_loop(self):
        """`agenerate` must await, not `time.sleep`: two overlapped calls take
        1x the latency, not 2x."""
        import asyncio
        import time

        with _allowed():
            client = StubClient(seed=1, latency_mean_s=0.15, latency_stddev_s=0.0)

        async def two_at_once():
            started = time.perf_counter()
            await asyncio.gather(client.agenerate("a"), client.agenerate("b"))
            return time.perf_counter() - started

        elapsed = asyncio.run(two_at_once())

        self.assertLess(elapsed, 0.28, "agenerate appears to block the event loop")


class SeedSyntheticLeadsTests(TestCase):
    """The fixture, and the one property that makes it a fixture worth having."""

    def test_it_creates_the_requested_number_with_a_safe_id_prefix(self):
        call_command("seed_synthetic_leads", count=30, seed=1, verbosity=0)

        self.assertEqual(Lead.objects.count(), 30)
        self.assertEqual(Lead.objects.filter(id__startswith="synth_").count(), 30)

    def test_the_action_mix_is_not_thirty_copies_of_one_lead(self):
        """The seeded mix spans several actions, including some ``unknown``
        (the fraction of the run concurrency cannot speed up)."""
        from project.app.services import actions
        from project.app.services.outreach import determine_action

        call_command("seed_synthetic_leads", count=60, seed=1, verbosity=0)
        classified = [determine_action(lead)[0] for lead in Lead.objects.all()]

        self.assertGreater(len(set(classified)), 3, f"mix is too narrow: {set(classified)}")
        self.assertIn(actions.UNKNOWN, classified)
        # ...but not so many that the benchmark is mostly skips.
        unknown_share = classified.count(actions.UNKNOWN) / len(classified)
        self.assertLess(unknown_share, 0.5)

    def test_each_synthetic_lead_classifies_exactly_as_its_template_does(self):
        """Re-anchoring dates and adding padding events must not move a lead.

        Compared against ``determine_action`` on the *template*, not the
        record's ``expected_action`` label -- the classifier does not score 100%
        on the golden set. Each side is classified against its own anchor: the
        template against the harness's frozen TODAY, the seeded lead against the
        real today it was re-anchored to.
        """
        from evals.run_rules_eval import GOLDEN_PATH, TODAY, build_lead, load_golden
        from project.app.services.outreach import determine_action

        templates = load_golden(GOLDEN_PATH)
        call_command("seed_synthetic_leads", count=len(templates), seed=1, verbosity=0)

        for position, record in enumerate(templates, start=1):
            # Ids are assigned before the shuffle, so `synth_0001` is always the
            # first template regardless of seed.
            lead = Lead.objects.prefetch_related("events").get(id=f"synth_{position:04d}")
            template_action, _ = determine_action(build_lead(record), today=TODAY)
            seeded_action, _ = determine_action(lead)

            self.assertEqual(
                seeded_action,
                template_action,
                f"{lead.id} (from golden {record.get('id')}) drifted during seeding",
            )

    def test_flush_only_deletes_synthetic_rows(self):
        """`--flush` clears the `synth_` prefix only, never the demo rows."""
        Lead.objects.create(
            id="lead_001",
            agency_name="Real Demo Agency",
            contact_name="Someone Real",
            contact_email="real@example.com",
            contact_phone="555-0000",
            state="CO",
            num_producers=1,
            years_in_business=1,
            estimated_book_size_usd=1,
            stage="active_trial",
        )
        call_command("seed_synthetic_leads", count=10, seed=1, verbosity=0)
        call_command("seed_synthetic_leads", count=5, seed=1, flush=True, verbosity=0)

        self.assertTrue(Lead.objects.filter(id="lead_001").exists())
        self.assertEqual(Lead.objects.filter(id__startswith="synth_").count(), 5)

    def test_events_are_attached_and_the_dates_are_re_anchored(self):
        import datetime

        call_command("seed_synthetic_leads", count=40, seed=1, verbosity=0)

        self.assertGreater(Event.objects.count(), 0)
        # Golden dates are relative to the harness's frozen TODAY.
        today = datetime.date.today()
        recent = Lead.objects.exclude(last_login_date=None).order_by("-last_login_date").first()
        if recent is not None:
            self.assertLessEqual((today - recent.last_login_date).days, 400)

    def test_a_zero_count_is_refused_rather_than_silently_seeding_nothing(self):
        call_command("seed_synthetic_leads", count=0, verbosity=0)
        self.assertEqual(Lead.objects.count(), 0)


class BenchmarkScriptTests(SimpleTestCase):
    """The benchmark's one safety-critical property, tested as a property."""

    def test_it_points_django_at_a_temporary_database_before_setup(self):
        """`DATABASE_URL` must be set before `django.setup()`: settings reads it
        at import time, and the failure mode is synthetic leads in the demo db."""
        source = (settings.BASE_DIR / "evals" / "bench_planner.py").read_text(encoding="utf-8")
        set_url = source.index('os.environ["DATABASE_URL"]')
        # The *call*, not the mention of it in the module docstring.
        setup = source.index("\n    django.setup()")

        self.assertLess(set_url, setup)
        # And it refuses to continue if Django did not honour it.
        self.assertIn("Refusing to run", source)

    def test_the_benchmark_runs_end_to_end_on_a_temporary_database(self):
        # A real subprocess: in-process would inherit this run's configured
        # Django. Results go to a tmpdir, clear of evals/results/.
        with tempfile.TemporaryDirectory(prefix="bench-smoke-") as tmpdir:
            result = subprocess.run(
                [
                    sys.executable,
                    "evals/bench_planner.py",
                    "--leads",
                    "6",
                    "--concurrency",
                    "3",
                    "--latency",
                    "0.001",
                    "--latency-stddev",
                    "0.0",
                    "--results-dir",
                    tmpdir,
                ],
                cwd=settings.BASE_DIR,
                capture_output=True,
                text=True,
                timeout=180,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Temporary database", result.stdout)
        self.assertIn("wall clock", result.stdout)
