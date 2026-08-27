"""Run-scoped cooldown shared by every concurrent provider call in one run.

Rate limits are org-level: once the provider closes the window, every worker's
next attempt is doomed, and independent per-worker retries just burn the run's
whole budget into it. One gate per run propagates the first worker's Retry-After
to the rest: report on failure, wait before every attempt.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable

from .errors import LLMError, LLMRateLimitError
from .retry import RETRY_AFTER_JITTER_FRACTION


class CooldownGate:
    """Holds every worker's next attempt until a reported rate limit clears.

    Only a rate limit carrying the provider's own Retry-After moves the gate: a
    guidance-less 429 already gets per-worker backoff, and a 5xx is an instance
    having a bad moment, not org-wide quota. The cooldown is capped by
    ``max_cooldown_s`` — the same knob that caps per-attempt backoff — so a
    hostile header cannot park the run (same argument as ``backoff_seconds``).
    """

    def __init__(
        self,
        max_cooldown_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if max_cooldown_s < 0:
            raise ValueError("CooldownGate.max_cooldown_s must not be negative.")
        self.max_cooldown_s = max_cooldown_s
        self._clock = clock
        self._sleep = sleep
        self._rand = rand
        # Monotonic instant before which no attempt should start.
        self._resume_at = clock()

    def observe(self, error: LLMError) -> None:
        """Record one failed attempt; a no-op unless it moves the gate."""
        if not isinstance(error, LLMRateLimitError) or error.retry_after is None:
            return
        cooldown = min(error.retry_after, self.max_cooldown_s)
        self._resume_at = max(self._resume_at, self._clock() + cooldown)

    async def wait(self) -> None:
        """Wait out the current cooldown, if any.

        Re-checked after every sleep — a sibling can extend the deadline while
        this worker is already asleep. The jitter overshoots the deadline so a
        herd released together does not fire again in lockstep.
        """
        while True:
            remaining = self._resume_at - self._clock()
            if remaining <= 0:
                return
            await self._sleep(remaining + self._rand(0.0, RETRY_AFTER_JITTER_FRACTION * remaining))


__all__ = ["CooldownGate"]
