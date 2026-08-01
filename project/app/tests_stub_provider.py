"""The benchmark's fake provider and its fixture (MUS-26e).

Two things need pinning and they pull in opposite directions.

The stub has to be **good enough to benchmark against**: its canned email must
pass the same two output gates the real thing does, or the benchmark times a
branch the demo never takes and reports a number about nothing.

And it has to be **impossible to reach from the app**. A fake provider that can
be selected in production is a way to ship an application that silently stops
calling an LLM, which is the worst possible bug for this product: every symptom
would be "the copy got a bit generic". The opt-in is ``OUTREACH_ALLOW_STUB_LLM``,
and the allowlist test below enumerates every file permitted to mention it --
including this one.
"""

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

        # The message has to tell whoever hit it what to do, and -- more
        # importantly -- that seeing it from the app means something is wrong.
        self.assertIn(ALLOW_ENV_VAR, str(caught.exception))

    def test_a_value_other_than_one_does_not_count(self):
        for value in ("0", "true", "yes", ""):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {ALLOW_ENV_VAR: value}):
                    with self.assertRaises(StubLLMNotAllowed):
                        StubClient()

    def test_the_provider_catalog_never_offers_it(self):
        """The structural barrier, and the one that actually matters.

        ``get_llm_client()`` resolves its provider from an ``LLMConfiguration``
        row whose ``provider`` is a foreign key to ``LLMProvider``. If the
        catalog has no "stub" row, no configuration can point at one and the
        Settings UI -- which lists providers from that same table -- can never
        show it. A key cannot be satisfied by wishing, which is why this is a
        stronger guard than the ``LLM_CONFIG_PATH`` file check it replaces.
        """
        from project.app.models import LLMProvider

        call_command("seed_llm_catalog", verbosity=0)

        self.assertFalse(LLMProvider.objects.filter(key=PROVIDER_NAME).exists())
        self.assertEqual(
            set(LLMProvider.objects.values_list("key", flat=True)),
            {"claude", "chatgpt", "deepseek", "groq"},
        )

    def test_only_the_benchmark_sets_the_opt_in(self):
        # The gate is worth nothing if something else in the repo turns it on.
        # `grep` rather than a mock, because this is a fact about the tree.
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
        # Registered on purpose: `build_client("stub")` goes through the same
        # factory as every real adapter, so a change to the LLMClient interface
        # breaks this the same way it would break Groq.
        self.assertIs(_REGISTRY[PROVIDER_NAME], StubClient)

        with _allowed():
            client = build_client(PROVIDER_NAME)

        self.assertIsInstance(client, StubClient)
        self.assertEqual(client.provider_name, PROVIDER_NAME)


class CannedEmailPassesTheRealGatesTests(TestCase):
    """The stub's output has to survive the planner's two output gates.

    Otherwise every benchmark lead takes the failure branch and the run
    measures a code path the product never takes -- which would be a benchmark
    that is not merely imprecise but about something else entirely.
    """

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

        # Found in the prompt, not hardcoded: proving the stub read a real
        # prompt is what proves the prompt-building phase ran at all.
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
        # The specific way a fake email goes wrong. "your 47 closed deals" would
        # be caught by the verifier and route every benchmark lead to a human --
        # turning the benchmark into a measurement of the failure path.
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
        # A gaussian around 1.87 with a fat spread has a negative tail, and a
        # negative sleep is a TypeError, not a fast call.
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
                # Both injected kinds are retryable on purpose: a benchmark run
                # with failures switched on should measure the *retry path*, not
                # degenerate into a run of dead leads.
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

        # MUS-25's token metric needs a number here, not a None.
        self.assertGreater(result.input_tokens, 0)
        self.assertGreater(result.output_tokens, 0)
        self.assertEqual(result.provider, PROVIDER_NAME)
        self.assertEqual(result.finish_reason, "stop")
        self.assertGreater(result.latency_s, 0)

    def test_the_async_path_does_not_block_the_loop(self):
        """The single most likely way for this file to quietly lie.

        A stub calling `time.sleep` inside `agenerate` would serialize the pool
        while looking correct, so every benchmark number would be a measurement
        of one worker. Asserted by overlapping two calls: with a blocking sleep
        the total is 2x, with a real await it is 1x.
        """
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
        """Two hundred identical leads would be a worse benchmark than twelve
        real ones -- they would all take the same branch.

        In particular the run has to include leads that classify as ``unknown``
        and skip generation entirely, because that fraction is exactly the part
        of the run concurrency cannot speed up.
        """
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
        """The property that makes the golden set worth using as a template.

        Compared against what ``determine_action`` says about the *template*,
        not against the record's ``expected_action`` label. The two are not the
        same thing -- the classifier does not score 100% on the golden set, which
        is precisely why the rules eval exists -- and a test that conflated them
        would fail for reasons that have nothing to do with this fixture.

        What it isolates is whether *seeding* changed anything: the dates are
        re-anchored on today and three padding events are added, and neither may
        move a lead. It is also the guard on the padding specifically. Make one
        an ``email_sent`` with ``outcome: no_reply``, or give it notes with a
        stall phrase, and leads start flipping into ``follow_up_after_hold`` --
        quietly corrupting the distribution this fixture exists to reproduce.

        Each side is classified against its own anchor: the template against the
        golden harness's frozen TODAY (exactly as the rules eval scores it), the
        seeded lead against the real today it was re-anchored to. Classifying
        both against the real today reads the template's dates through a
        widening gap and starts failing of its own accord some number of days
        after the golden file was written, with seeding blameless.
        """
        from evals.run_rules_eval import GOLDEN_PATH, TODAY, build_lead, load_golden
        from project.app.services.outreach import determine_action

        templates = load_golden(GOLDEN_PATH)
        call_command("seed_synthetic_leads", count=len(templates), seed=1, verbosity=0)

        for position, record in enumerate(templates, start=1):
            # Ids are assigned before the shuffle, so `synth_0001` is always the
            # first template -- the seed decides which *row* carries it, not
            # which template it came from.
            lead = Lead.objects.prefetch_related("events").get(id=f"synth_{position:04d}")
            template_action, _ = determine_action(build_lead(record), today=TODAY)
            seeded_action, _ = determine_action(lead)

            self.assertEqual(
                seeded_action,
                template_action,
                f"{lead.id} (from golden {record.get('id')}) drifted during seeding",
            )

    def test_flush_only_deletes_synthetic_rows(self):
        """The reason the id prefix exists.

        `--flush` runs against whatever database it is pointed at, and the demo
        pipeline is the DEMO.md contract. Emptying the table would be a much
        more convenient implementation and a much worse one.
        """
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
        # Golden dates are relative to that harness's frozen TODAY; re-anchoring
        # them on the real today is what preserves the classification each
        # template was labelled with.
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
        """`DATABASE_URL` must be set before `django.setup()`, not after.

        `project/settings.py` reads it at import time, so an assignment
        afterwards is decoration -- and the failure mode is 200 synthetic leads
        in the demo database, which is the DEMO.md contract.
        """
        source = (settings.BASE_DIR / "evals" / "bench_planner.py").read_text(encoding="utf-8")
        set_url = source.index('os.environ["DATABASE_URL"]')
        # The *call*, not the mention of it in the module docstring.
        setup = source.index("\n    django.setup()")

        self.assertLess(set_url, setup)
        # And it refuses to continue if Django did not honour it.
        self.assertIn("Refusing to run", source)

    def test_the_benchmark_runs_end_to_end_on_a_temporary_database(self):
        # A real subprocess: the point is the bootstrap sequence, and importing
        # the module in-process would inherit this test run's already-configured
        # Django. Tiny and fast -- 6 leads at 1ms of simulated latency. The
        # result JSON goes to a temporary directory so this smoke artifact can
        # never end up in evals/results/ beside the committed 200-lead numbers.
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
