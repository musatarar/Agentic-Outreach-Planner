"""The run-scoped provider cooldown gate: one worker's Retry-After holds the fleet.

Clock, sleep and rand are injected as in the retry suite; the fake sleep advances
the fake clock, so waits complete deterministically without real time passing.
"""

import unittest

from project.app.services.llm import cooldown, errors, retry


def _top_of_range(low, high):
    return high


def _bottom_of_range(low, high):
    return low


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class _AdvancingSleep:
    """Records each wait and advances the fake clock by it, so ``wait()`` terminates."""

    def __init__(self, clock):
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, seconds):
        self.calls.append(seconds)
        self.clock.now += seconds


def _gate(max_cooldown_s=30.0, rand=_bottom_of_range):
    clock = _Clock()
    sleep = _AdvancingSleep(clock)
    gate = cooldown.CooldownGate(max_cooldown_s, clock=clock, sleep=sleep, rand=rand)
    return gate, clock, sleep


def rate_limit(retry_after=None):
    return errors.LLMRateLimitError(
        "Rate limit reached", provider="groq", status_code=429, retry_after=retry_after
    )


class CooldownGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_untouched_gate_never_waits(self):
        gate, _, sleep = _gate()
        await gate.wait()
        self.assertEqual(sleep.calls, [])

    async def test_a_reported_retry_after_holds_every_waiter(self):
        gate, _, sleep = _gate()
        gate.observe(rate_limit(retry_after=20.0))
        await gate.wait()
        self.assertEqual(sleep.calls, [20.0])

    async def test_a_rate_limit_with_no_guidance_does_not_move_the_gate(self):
        # Per-worker backoff already covers it; there is no number to propagate.
        gate, _, sleep = _gate()
        gate.observe(rate_limit(retry_after=None))
        await gate.wait()
        self.assertEqual(sleep.calls, [])

    async def test_other_failures_do_not_move_the_gate(self):
        # A 5xx is an instance having a bad moment, not org-wide quota: pausing
        # the whole run for it would trade throughput for nothing.
        gate, _, sleep = _gate()
        gate.observe(errors.LLMTransientError("503", retry_after=20.0))
        await gate.wait()
        self.assertEqual(sleep.calls, [])

    async def test_the_cooldown_is_capped_like_per_attempt_backoff(self):
        # Same argument as backoff_seconds: a hostile header must not park the run.
        gate, _, sleep = _gate(max_cooldown_s=10.0)
        gate.observe(rate_limit(retry_after=300.0))
        await gate.wait()
        self.assertEqual(sleep.calls, [10.0])

    async def test_a_zero_cap_disables_the_gate(self):
        # OUTREACH_MAX_BACKOFF_S=0 is the suites' no-sleep switch; the gate
        # must respect it the same way the backoff schedule does.
        gate, _, sleep = _gate(max_cooldown_s=0.0)
        gate.observe(rate_limit(retry_after=20.0))
        await gate.wait()
        self.assertEqual(sleep.calls, [])

    async def test_a_shorter_report_cannot_pull_an_existing_deadline_in(self):
        gate, _, sleep = _gate()
        gate.observe(rate_limit(retry_after=20.0))
        gate.observe(rate_limit(retry_after=5.0))
        await gate.wait()
        self.assertEqual(sleep.calls, [20.0])

    async def test_a_longer_report_extends_the_deadline(self):
        gate, _, sleep = _gate()
        gate.observe(rate_limit(retry_after=5.0))
        gate.observe(rate_limit(retry_after=20.0))
        await gate.wait()
        self.assertEqual(sleep.calls, [20.0])

    async def test_an_extension_landing_mid_wait_is_honoured(self):
        # A sibling can report while this worker is already asleep; the wake-up
        # re-checks the deadline rather than barging through.
        class _ExtendingSleep(_AdvancingSleep):
            gate = None

            async def __call__(self, seconds):
                await super().__call__(seconds)
                if len(self.calls) == 1:
                    self.gate.observe(rate_limit(retry_after=10.0))

        clock = _Clock()
        sleep = _ExtendingSleep(clock)
        gate = cooldown.CooldownGate(30.0, clock=clock, sleep=sleep, rand=_bottom_of_range)
        sleep.gate = gate

        gate.observe(rate_limit(retry_after=10.0))
        await gate.wait()
        self.assertEqual(sleep.calls, [10.0, 10.0])

    async def test_re_entry_is_staggered_not_lockstep(self):
        # At the top of the jitter range the wait overshoots the deadline, so a
        # herd released together does not fire again in the same instant.
        gate, _, sleep = _gate(rand=_top_of_range)
        gate.observe(rate_limit(retry_after=20.0))
        await gate.wait()
        self.assertEqual(sleep.calls, [20.0 * (1 + retry.RETRY_AFTER_JITTER_FRACTION)])

    def test_a_negative_cap_is_rejected(self):
        with self.assertRaises(ValueError):
            cooldown.CooldownGate(-1.0)


class _RecordingScope:
    """Mirror of the retry suite's scope double, sharing one event list."""

    def __init__(self, attempt, events):
        self.attempt = attempt
        self.events = events

    def __enter__(self):
        self.events.append(("enter", self.attempt))
        return None

    def __exit__(self, exc_type, exc, tb):
        self.events.append(("exit", self.attempt))
        return False


class RetryLoopGateTests(unittest.IsolatedAsyncioTestCase):
    """`acall_with_retry(gate=...)`: wait before every attempt, report failures."""

    async def test_the_wait_sits_outside_the_attempt_scope(self):
        # Fleet coordination must not land in the attempt's span as provider
        # latency, so the order is wait, enter, call, exit -- every attempt.
        events = []

        class _NarratingGate:
            async def wait(self):
                events.append("wait")

            def observe(self, error):
                events.append(("observe", type(error).__name__))

        script = [rate_limit(retry_after=20.0), "ok"]

        async def operation():
            outcome = script.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        async def no_sleep(seconds):
            return None

        result = await retry.acall_with_retry(
            operation,
            policy=retry.RetryPolicy(initial_backoff_s=0.0, max_backoff_s=0.0),
            sleep=no_sleep,
            rand=_bottom_of_range,
            attempt_scope=lambda n: _RecordingScope(n, events),
            gate=_NarratingGate(),
        )

        self.assertEqual(result, "ok")
        self.assertEqual(
            events,
            [
                "wait",
                ("enter", 0),
                ("exit", 0),
                ("observe", "LLMRateLimitError"),
                "wait",
                ("enter", 1),
                ("exit", 1),
            ],
        )

    async def test_the_gate_hears_about_the_final_failed_attempt_too(self):
        # This worker is giving up; the information is still for the others.
        gate, _, sleep = _gate()

        async def always_throttled():
            raise rate_limit(retry_after=20.0)

        with self.assertRaises(errors.LLMRateLimitError):
            await retry.acall_with_retry(
                always_throttled,
                policy=retry.RetryPolicy(max_attempts=1),
                sleep=sleep,
                rand=_bottom_of_range,
                gate=gate,
            )

        await gate.wait()
        self.assertEqual(sleep.calls, [20.0])

    async def test_a_sibling_starting_later_is_held_out_of_the_closed_window(self):
        # The incident this exists for: worker A hears "come back in 20s" and
        # gives up; worker B, sharing the gate, must not spend its own attempts
        # into the same closed window.
        gate, clock, sleep = _gate()

        async def throttled():
            raise rate_limit(retry_after=20.0)

        with self.assertRaises(errors.LLMRateLimitError):
            await retry.acall_with_retry(
                throttled,
                policy=retry.RetryPolicy(max_attempts=1),
                sleep=sleep,
                rand=_bottom_of_range,
                gate=gate,
            )
        self.assertEqual(sleep.calls, [])  # one attempt: no backoff slept, and yet --

        started_at = []

        async def healthy():
            started_at.append(clock.now)
            return "ok"

        result = await retry.acall_with_retry(
            healthy,
            policy=retry.RetryPolicy(max_attempts=1),
            sleep=sleep,
            rand=_bottom_of_range,
            gate=gate,
        )

        self.assertEqual(result, "ok")
        self.assertEqual(started_at, [20.0])

    async def test_no_gate_means_exactly_the_old_behaviour(self):
        # The default is None so every existing caller is untouched.
        async def operation():
            return "ok"

        self.assertEqual(await retry.acall_with_retry(operation), "ok")


if __name__ == "__main__":
    unittest.main()
