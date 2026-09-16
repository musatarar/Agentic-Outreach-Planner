"""The fake provider: good enough to test against, and impossible to reach from
the app without the ``OUTREACH_ALLOW_STUB_LLM`` opt-in."""

import os
from unittest import mock

from django.test import SimpleTestCase, TestCase

from project.app.models import Lead
from project.app.services import verify
from project.app.services.llm import _REGISTRY, _build_client, build_client, get_llm_client
from project.app.services.llm.stub import (
    ALLOW_ENV_VAR,
    PROVIDER_NAME,
    StubClient,
    StubLLMNotAllowed,
    canned_email,
)
from project.app.services.outreach import _build_copy_prompt, validate_copy
from project.app.tests.tenancy_utils import default_tenant


def _allowed():
    return mock.patch.dict(os.environ, {ALLOW_ENV_VAR: "1"})


class StubIsUnreachableFromTheAppTests(TestCase):
    """The opt-in is the barrier, and nothing in the app trips it."""

    def setUp(self):
        super().setUp()
        # The factory caches per (provider, model, key); a client another test
        # built for the same tuple would mask the refusal.
        _build_client.cache_clear()
        self.addCleanup(_build_client.cache_clear)

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

    def test_naming_it_as_the_configured_provider_is_still_refused(self):
        """Selection is environment-only, so ``LLM_PROVIDER=stub`` is a thing an
        operator can type. It buys a refusal, not a fake provider."""
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": PROVIDER_NAME}, clear=True):
            with self.assertRaises(StubLLMNotAllowed) as caught:
                get_llm_client()

        self.assertIn(ALLOW_ENV_VAR, str(caught.exception))

    def test_the_opt_in_alone_is_not_something_the_app_sets(self):
        """With the opt-in present the same selection builds one -- which is why
        the opt-in, not the spelling of LLM_PROVIDER, is the barrier that counts."""
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": PROVIDER_NAME, ALLOW_ENV_VAR: "1"}):
            self.assertIsInstance(get_llm_client(), StubClient)

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
            tenant=default_tenant(),
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
