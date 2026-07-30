"""Tests for the shared retry policy (MUS-46).

``asyncio.sleep`` and ``random.uniform`` are injected rather than patched
globally, and the stub for ``uniform`` returns the top of its range. That makes
these assertions about the *bounds the policy produces* — which is the actual
contract — instead of about a seeded PRNG's output, which would change silently
under a Python upgrade and send someone hunting a retry bug that isn't there.
"""

import unittest

from project.app.services.llm import errors, retry


def _top_of_range(low, high):
    """Stand-in for random.uniform that always returns the ceiling."""
    return high


def _bottom_of_range(low, high):
    return low


class RecordingSleep:
    """Async sleep that records what it was asked to wait, and never waits."""

    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds):
        self.calls.append(seconds)


class RetryPolicyTests(unittest.TestCase):
    def test_defaults_are_a_usable_policy(self):
        policy = retry.RetryPolicy()
        self.assertEqual(policy.max_attempts, 4)
        self.assertEqual(policy.initial_backoff_s, 0.5)
        self.assertEqual(policy.max_backoff_s, 30.0)
        self.assertEqual(policy.multiplier, 2.0)

    def test_one_attempt_is_legal_zero_is_not(self):
        # max_attempts counts TOTAL attempts, not retries. 1 means "call once,
        # surface whatever happens"; 0 would mean never calling the provider,
        # which is never what anyone meant.
        self.assertEqual(retry.RetryPolicy(max_attempts=1).max_attempts, 1)
        with self.assertRaises(ValueError):
            retry.RetryPolicy(max_attempts=0)

    def test_nonsensical_policies_are_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            retry.RetryPolicy(initial_backoff_s=-1)
        with self.assertRaises(ValueError):
            retry.RetryPolicy(max_backoff_s=-1)
        with self.assertRaises(ValueError):
            # A multiplier below 1 would make each wait SHORTER than the last,
            # which is the opposite of backoff.
            retry.RetryPolicy(multiplier=0.5)


class BackoffTests(unittest.TestCase):
    def test_ceiling_grows_exponentially_and_then_caps(self):
        policy = retry.RetryPolicy(initial_backoff_s=0.5, multiplier=2.0, max_backoff_s=4.0)
        ceilings = [retry.backoff_seconds(n, policy, rand=_top_of_range) for n in range(6)]
        self.assertEqual(ceilings, [0.5, 1.0, 2.0, 4.0, 4.0, 4.0])

    def test_jitter_is_full_not_a_nudge(self):
        # Full jitter means the wait is uniform over [0, ceiling] -- two workers
        # that fail at the same instant must be able to come back at genuinely
        # unrelated times. A schedule of "ceiling plus a small random nudge"
        # would keep them in lockstep, which is the failure this prevents.
        policy = retry.RetryPolicy(initial_backoff_s=1.0, multiplier=2.0, max_backoff_s=30.0)
        self.assertEqual(retry.backoff_seconds(2, policy, rand=_bottom_of_range), 0.0)
        self.assertEqual(retry.backoff_seconds(2, policy, rand=_top_of_range), 4.0)

    def test_retry_after_wins_on_magnitude(self):
        policy = retry.RetryPolicy(initial_backoff_s=0.5, multiplier=2.0, max_backoff_s=60.0)
        # Provider said 30s; the exponential ceiling for attempt 0 is 0.5s.
        # The header is real information about when the window reopens.
        self.assertEqual(
            retry.backoff_seconds(0, policy, retry_after=30.0, rand=_bottom_of_range), 30.0
        )

    def test_retry_after_still_gets_jitter_on_top(self):
        # The failure mode this exists for: eight workers all receive
        # `Retry-After: 30`, all sleep exactly thirty seconds, all wake in the
        # same millisecond and re-throttle each other. Observed on Groq's free
        # tier, which is this repo's default provider.
        policy = retry.RetryPolicy(max_backoff_s=60.0)
        spread = retry.backoff_seconds(0, policy, retry_after=30.0, rand=_top_of_range)
        self.assertEqual(spread, 30.0 + retry.RETRY_AFTER_JITTER_FRACTION * 30.0)
        self.assertGreater(spread, 30.0)

    def test_retry_after_is_capped_by_the_policy(self):
        policy = retry.RetryPolicy(max_backoff_s=10.0)
        base = retry.backoff_seconds(0, policy, retry_after=45.0, rand=_bottom_of_range)
        self.assertEqual(base, 10.0)


class CallWithRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sleep = RecordingSleep()
        self.attempts = 0

    def _operation(self, *outcomes):
        """A coroutine function yielding ``outcomes`` in order.

        An exception outcome is raised; anything else is returned.
        """
        queue = list(outcomes)

        async def operation():
            self.attempts += 1
            outcome = queue.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        return operation

    async def _run(self, operation, **kwargs):
        kwargs.setdefault("sleep", self.sleep)
        kwargs.setdefault("rand", _top_of_range)
        return await retry.acall_with_retry(operation, **kwargs)

    async def test_success_on_the_first_attempt_never_sleeps(self):
        result = await self._run(self._operation("ok"))
        self.assertEqual(result, "ok")
        self.assertEqual(self.attempts, 1)
        self.assertEqual(self.sleep.calls, [])

    async def test_retryable_failures_are_retried_until_success(self):
        operation = self._operation(
            errors.LLMRateLimitError("429"),
            errors.LLMTransientError("503"),
            "ok",
        )
        policy = retry.RetryPolicy(initial_backoff_s=1.0, multiplier=2.0, max_backoff_s=30.0)
        result = await self._run(operation, policy=policy)

        self.assertEqual(result, "ok")
        self.assertEqual(self.attempts, 3)
        # Two sleeps, at the ceilings for attempts 0 and 1.
        self.assertEqual(self.sleep.calls, [1.0, 2.0])

    async def test_non_retryable_raises_on_attempt_one_with_no_sleep(self):
        # The whole point of the taxonomy at the call site. A missing API key
        # used to cost six attempts and ~40 seconds to produce the message it
        # already had.
        operation = self._operation(errors.LLMAuthError("no key"), "never reached")
        with self.assertRaises(errors.LLMAuthError):
            await self._run(operation)
        self.assertEqual(self.attempts, 1)
        self.assertEqual(self.sleep.calls, [])

    async def test_max_attempts_is_a_total_not_a_retry_count(self):
        operation = self._operation(*[errors.LLMTransientError("503") for _ in range(10)])
        with self.assertRaises(errors.LLMTransientError):
            await self._run(operation, policy=retry.RetryPolicy(max_attempts=3))
        self.assertEqual(self.attempts, 3)
        # Three attempts means two waits -- never a sleep after the last one,
        # which would delay the failure without any chance of changing it.
        self.assertEqual(len(self.sleep.calls), 2)

    async def test_a_single_attempt_policy_does_not_retry(self):
        operation = self._operation(errors.LLMTransientError("503"), "never reached")
        with self.assertRaises(errors.LLMTransientError):
            await self._run(operation, policy=retry.RetryPolicy(max_attempts=1))
        self.assertEqual(self.attempts, 1)
        self.assertEqual(self.sleep.calls, [])

    async def test_provider_retry_after_is_honoured_between_attempts(self):
        rate_limited = errors.LLMRateLimitError("429", retry_after=20.0)
        operation = self._operation(rate_limited, "ok")
        await self._run(operation, policy=retry.RetryPolicy(max_backoff_s=60.0))
        self.assertEqual(self.sleep.calls, [20.0 + retry.RETRY_AFTER_JITTER_FRACTION * 20.0])

    async def test_non_llm_exceptions_are_not_retried(self):
        # A bug on our side is not a provider blip. Retrying it would turn one
        # stack trace into the same stack trace, four times as slowly.
        operation = self._operation(ValueError("our bug"), "never reached")
        with self.assertRaises(ValueError):
            await self._run(operation)
        self.assertEqual(self.attempts, 1)
        self.assertEqual(self.sleep.calls, [])

    async def test_on_attempt_sees_every_attempt_and_its_outcome(self):
        # The hook MUS-25 opens a per-HTTP-attempt span from, so this module
        # never has to import anything telemetry-shaped.
        seen = []
        operation = self._operation(errors.LLMTransientError("503"), "ok")
        await self._run(operation, on_attempt=lambda n, exc: seen.append((n, type(exc))))

        self.assertEqual(
            seen,
            [
                (0, type(None)),
                (0, errors.LLMTransientError),
                (1, type(None)),
            ],
        )


if __name__ == "__main__":
    unittest.main()
