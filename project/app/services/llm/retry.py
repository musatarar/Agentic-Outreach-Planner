"""Shared retry policy for provider calls (MUS-46).

One place decides how a failed LLM call is retried, for every provider and
every caller. Before this module the only retry logic in the repo was a private
helper inside ``evals/copy_eval.py`` that retried *any* exception six times —
so a missing ``GROQ_API_KEY`` cost about forty seconds of sleeping to fail with
exactly the message it already had on the first attempt.

Two decisions carry most of the value here.

**Retry only what says it is retryable.** The taxonomy in
:mod:`project.app.services.llm.errors` already answers that question per
failure class, so this module never inspects a status code or a message. A
non-retryable error propagates from attempt one having slept zero seconds.

**Full jitter, including on top of Retry-After.** The backoff is
``random.uniform(0, min(cap, initial * multiplier ** n))`` — the AWS
"full jitter" schedule — rather than an exponential sequence with a small
random nudge. The distinction only matters under concurrency, which is exactly
the regime the planner is heading for: with a fixed schedule, N workers that
fail at the same moment retry at the same moment, forever, in lockstep.

The same argument applies to a provider-supplied ``Retry-After``, and that case
is easier to get wrong because honouring the header *feels* like the careful
thing to do. Eight workers that each receive ``Retry-After: 30`` and sleep
exactly thirty seconds wake in the same millisecond and re-throttle each other
immediately. So the header wins on magnitude — it is real information about
when the window reopens — but a proportional jitter is added on top to spread
the herd. This is not hypothetical: it is the observed behaviour of Groq's free
tier, which is this repo's default provider.

The helper is async-only. Every caller that retries is already in a coroutine
(the eval harness, and the concurrent planner that follows), and a sync twin
would be a second copy of the schedule to keep in step for no current user.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import TypeVar

from .errors import LLMError

T = TypeVar("T")

# Defaults, used when a caller passes no policy. MUS-26 makes these
# configurable via config.toml; until then this dataclass is the single
# definition and callers override per call site.
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_INITIAL_BACKOFF_S = 0.5
DEFAULT_MAX_BACKOFF_S = 30.0
DEFAULT_MULTIPLIER = 2.0

# Fraction of a provider-supplied Retry-After added as jitter. Small on purpose:
# the header is real information about when the rate-limit window reopens, so
# the jitter only needs to be large enough to decorrelate a herd of workers, not
# large enough to meaningfully change when we come back.
RETRY_AFTER_JITTER_FRACTION = 0.25


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How many times to retry a provider call, and how long to wait.

    ``max_attempts`` counts *total* attempts, not retries: ``1`` means "call it
    once and surface whatever happens". Off-by-one here is the classic way to
    quietly triple a rate-limited run's wall clock, so the name says which it is
    and ``__post_init__`` refuses ``0``.
    """

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    initial_backoff_s: float = DEFAULT_INITIAL_BACKOFF_S
    max_backoff_s: float = DEFAULT_MAX_BACKOFF_S
    multiplier: float = DEFAULT_MULTIPLIER

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("RetryPolicy.max_attempts must be at least 1 (one attempt, no retry).")
        if self.initial_backoff_s < 0 or self.max_backoff_s < 0:
            raise ValueError("RetryPolicy backoff values must not be negative.")
        if self.multiplier < 1:
            raise ValueError("RetryPolicy.multiplier must be at least 1 (backoff must not shrink).")


DEFAULT_POLICY = RetryPolicy()


def backoff_seconds(
    attempt: int,
    policy: RetryPolicy = DEFAULT_POLICY,
    retry_after: float | None = None,
    rand: Callable[[float, float], float] = random.uniform,
) -> float:
    """Seconds to sleep before attempt ``attempt + 1``. ``attempt`` is 0-based.

    ``rand`` is injected so the schedule is testable as a schedule — with a
    stub returning the top of the range, the assertion is about the *bounds* the
    policy produces rather than about a seeded PRNG's output, which would
    silently change under a Python upgrade.
    """
    if retry_after is not None:
        # The provider told us when its window reopens. Trust the magnitude,
        # capped so a hostile or confused header can't park a worker, and add
        # proportional jitter so a herd that all got the same value doesn't wake
        # together and immediately re-throttle each other.
        #
        # The jitter is proportional to the CAPPED base, not to the raw header.
        # Taking a fraction of the raw value would let a 300s header add 75s of
        # jitter to a 30s cap, so max_backoff_s would not actually be a bound --
        # and a bound that only holds for some inputs is worse than none,
        # because it is the one everybody reasons with.
        base = min(retry_after, policy.max_backoff_s)
        return base + rand(0.0, RETRY_AFTER_JITTER_FRACTION * base)

    # Full jitter: uniform over [0, exponential_ceiling]. Not "exponential plus
    # a nudge" -- the whole point is that two workers failing simultaneously
    # come back at genuinely unrelated times.
    ceiling = min(policy.max_backoff_s, policy.initial_backoff_s * policy.multiplier**attempt)
    return rand(0.0, ceiling)


async def acall_with_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy = DEFAULT_POLICY,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand: Callable[[float, float], float] = random.uniform,
    attempt_scope: Callable[[int], AbstractContextManager[object]] | None = None,
) -> T:
    """Await ``operation()``, retrying retryable :class:`LLMError`s.

    ``operation`` is a zero-argument coroutine function so this helper never
    needs to know the shape of the call it is retrying — the caller closes over
    its own arguments. That is what lets one implementation cover both the
    planner's copy generation and the eval's judge.

    Only ``LLMError`` is caught. Anything else is a bug on our side, not a
    provider blip, and retrying it would turn a stack trace into a stack trace
    delivered four times as slowly.

    ``attempt_scope(attempt)`` returns a context manager wrapped around each
    attempt, 0-based. It is a *scope* rather than a pair of callbacks because
    the thing it exists for — MUS-25 opening one span per HTTP attempt — needs
    every attempt to end exactly once, including the successful last one and
    including a cancellation. Two callbacks would have to enumerate those cases;
    ``__exit__`` gets them for free, and receives the exception info a span needs
    to set its status. This module still imports nothing telemetry-shaped.

    Note the scope wraps only the provider call, never the backoff sleep, so an
    attempt's duration stays the attempt's duration.
    """
    last_error: LLMError | None = None
    for attempt in range(policy.max_attempts):
        scope = attempt_scope(attempt) if attempt_scope is not None else nullcontext()
        try:
            with scope:
                return await operation()
        except LLMError as exc:
            last_error = exc
            if not exc.retryable:
                # Auth failures, bad requests, malformed responses: the same
                # call will fail the same way. Surface it now, at attempt one,
                # rather than after the full budget of sleeps.
                raise
            if attempt == policy.max_attempts - 1:
                raise
            await sleep(backoff_seconds(attempt, policy, exc.retry_after, rand))

    # Unreachable: the loop either returns, or raises on the final attempt.
    # Kept so the function has no implicit `return None` path for a type
    # checker or a future edit to fall through into.
    raise last_error if last_error is not None else RuntimeError("retry loop exited without result")


__all__ = [
    "RetryPolicy",
    "DEFAULT_POLICY",
    "RETRY_AFTER_JITTER_FRACTION",
    "backoff_seconds",
    "acall_with_retry",
]
