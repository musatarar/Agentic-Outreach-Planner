"""Tests for the shared retry policy (MUS-46).

``sleep`` and ``rand`` are injected, with the rand stubs pinned to the bottom or top of
each range, so the assertions are about the bounds the policy produces.
"""

import asyncio
import unittest

from project.app.services.llm import errors, retry


class _RecordingScope:
    """Stands in for a per-attempt telemetry span, recording enter, result and exit."""

    def __init__(self, attempt, events):
        self.attempt = attempt
        self.events = events

    def __enter__(self):
        self.events.append(("enter", self.attempt, None))
        return lambda result: self.events.append(("result", self.attempt, result))

    def __exit__(self, exc_type, exc, tb):
        self.events.append(("exit", self.attempt, exc_type))
        return False


class _BlindScope:
    """A scope that yields nothing -- the opt-out path, as nullcontext does."""

    def __init__(self, attempt, events):
        self.attempt = attempt
        self.events = events

    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        self.events.append(("exit", self.attempt, exc_type))
        return False


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
        # max_attempts counts TOTAL attempts, not retries.
        self.assertEqual(retry.RetryPolicy(max_attempts=1).max_attempts, 1)
        with self.assertRaises(ValueError):
            retry.RetryPolicy(max_attempts=0)

    def test_nonsensical_policies_are_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            retry.RetryPolicy(initial_backoff_s=-1)
        with self.assertRaises(ValueError):
            retry.RetryPolicy(max_backoff_s=-1)
        with self.assertRaises(ValueError):
            retry.RetryPolicy(multiplier=0.5)  # below 1 shortens each wait


class BackoffTests(unittest.TestCase):
    def test_ceiling_grows_exponentially_and_then_caps(self):
        policy = retry.RetryPolicy(initial_backoff_s=0.5, multiplier=2.0, max_backoff_s=4.0)
        ceilings = [retry.backoff_seconds(n, policy, rand=_top_of_range) for n in range(6)]
        self.assertEqual(ceilings, [0.5, 1.0, 2.0, 4.0, 4.0, 4.0])

    def test_jitter_is_full_not_a_nudge(self):
        # Full jitter: the wait is uniform over [0, ceiling], not ceiling plus a nudge.
        policy = retry.RetryPolicy(initial_backoff_s=1.0, multiplier=2.0, max_backoff_s=30.0)
        self.assertEqual(retry.backoff_seconds(2, policy, rand=_bottom_of_range), 0.0)
        self.assertEqual(retry.backoff_seconds(2, policy, rand=_top_of_range), 4.0)

    def test_retry_after_wins_on_magnitude(self):
        policy = retry.RetryPolicy(initial_backoff_s=0.5, multiplier=2.0, max_backoff_s=60.0)
        # Provider said 30s; the exponential ceiling for attempt 0 is 0.5s.
        self.assertEqual(
            retry.backoff_seconds(0, policy, retry_after=30.0, rand=_bottom_of_range), 30.0
        )

    def test_retry_after_still_gets_jitter_on_top(self):
        # Otherwise every worker handed the same Retry-After wakes in lockstep.
        policy = retry.RetryPolicy(max_backoff_s=60.0)
        spread = retry.backoff_seconds(0, policy, retry_after=30.0, rand=_top_of_range)
        self.assertEqual(spread, 30.0 + retry.RETRY_AFTER_JITTER_FRACTION * 30.0)
        self.assertGreater(spread, 30.0)

    def test_retry_after_is_capped_by_the_policy_at_both_ends_of_the_jitter(self):
        # Both ends: at the bottom of the range the jitter's base is invisible.
        policy = retry.RetryPolicy(max_backoff_s=10.0)
        lowest = retry.backoff_seconds(0, policy, retry_after=300.0, rand=_bottom_of_range)
        highest = retry.backoff_seconds(0, policy, retry_after=300.0, rand=_top_of_range)

        self.assertEqual(lowest, 10.0)
        self.assertEqual(highest, 10.0 + retry.RETRY_AFTER_JITTER_FRACTION * 10.0)
        self.assertLessEqual(
            highest, policy.max_backoff_s * (1 + retry.RETRY_AFTER_JITTER_FRACTION)
        )


class CallWithRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sleep = RecordingSleep()
        self.attempts = 0

    def _operation(self, *outcomes):
        """A coroutine function yielding ``outcomes`` in order; exceptions are raised."""
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
        # Three attempts means two waits — never a sleep after the last one.
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
        operation = self._operation(ValueError("our bug"), "never reached")
        with self.assertRaises(ValueError):
            await self._run(operation)
        self.assertEqual(self.attempts, 1)
        self.assertEqual(self.sleep.calls, [])

    async def test_attempt_scope_opens_and_closes_once_per_attempt(self):
        # Every attempt ends exactly once, including the successful last one.
        events = []
        operation = self._operation(errors.LLMTransientError("503"), "ok")
        await self._run(operation, attempt_scope=lambda n: _RecordingScope(n, events))

        self.assertEqual(
            events,
            [
                ("enter", 0, None),
                ("exit", 0, errors.LLMTransientError),
                ("enter", 1, None),
                # The successful attempt hands its result to the scope before closing.
                ("result", 1, "ok"),
                ("exit", 1, None),
            ],
        )

    async def test_a_scope_that_yields_nothing_still_works(self):
        events = []
        await self._run(self._operation("ok"), attempt_scope=lambda n: _BlindScope(n, events))
        self.assertEqual(events, [("exit", 0, None)])

    async def test_attempt_scope_sees_the_exception_on_a_permanent_failure(self):
        events = []
        operation = self._operation(errors.LLMAuthError("no key"))
        with self.assertRaises(errors.LLMAuthError):
            await self._run(operation, attempt_scope=lambda n: _RecordingScope(n, events))
        self.assertEqual(events[-1], ("exit", 0, errors.LLMAuthError))

    async def test_cancellation_during_backoff_is_not_retried(self):
        # A retry helper that swallows cancellation makes a run un-killable.
        async def cancelling_sleep(seconds):
            raise asyncio.CancelledError

        operation = self._operation(errors.LLMTransientError("503"), "never reached")
        with self.assertRaises(asyncio.CancelledError):
            await retry.acall_with_retry(operation, sleep=cancelling_sleep, rand=_top_of_range)
        self.assertEqual(self.attempts, 1)

    async def test_cancellation_closes_the_attempt_scope(self):
        events = []

        async def cancelled_operation():
            self.attempts += 1
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await retry.acall_with_retry(
                cancelled_operation,
                sleep=self.sleep,
                attempt_scope=lambda n: _RecordingScope(n, events),
            )
        self.assertEqual(events[-1], ("exit", 0, asyncio.CancelledError))


if __name__ == "__main__":
    unittest.main()
