"""Shared retry policy for provider calls (MUS-46).

One async-only helper decides how a failed LLM call is retried, whichever
provider is configured. Only errors the :mod:`.errors` taxonomy marks retryable
are retried; backoff is the AWS "full jitter" schedule, so concurrent workers
that fail together do not retry in lockstep.
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

# Defaults, used when a caller passes no policy.
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_INITIAL_BACKOFF_S = 0.5
DEFAULT_MAX_BACKOFF_S = 30.0
DEFAULT_MULTIPLIER = 2.0

# Fraction of a provider-supplied Retry-After added as jitter: enough to
# decorrelate a herd of workers, small enough not to move when we come back.
RETRY_AFTER_JITTER_FRACTION = 0.25


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How many times to retry a provider call, and how long to wait.

    ``max_attempts`` counts *total* attempts, not retries: ``1`` means "call it
    once and surface whatever happens".
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

    ``rand`` is injected so tests can assert on the policy's *bounds* rather
    than on a seeded PRNG's output.
    """
    if retry_after is not None:
        # Trust the provider's magnitude, capped at max_backoff_s. The jitter is
        # proportional to the CAPPED base, not the raw header, so the cap stays a
        # real bound (a 300s header must not add 75s on top of a 30s cap).
        base = min(retry_after, policy.max_backoff_s)
        return base + rand(0.0, RETRY_AFTER_JITTER_FRACTION * base)

    # Full jitter: uniform over [0, exponential_ceiling], not "exponential plus a
    # nudge" -- workers failing simultaneously must come back at unrelated times.
    ceiling = min(policy.max_backoff_s, policy.initial_backoff_s * policy.multiplier**attempt)
    return rand(0.0, ceiling)


async def acall_with_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy = DEFAULT_POLICY,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand: Callable[[float, float], float] = random.uniform,
    attempt_scope: Callable[[int], AbstractContextManager[Callable[[T], None] | None]]
    | None = None,
) -> T:
    """Await ``operation()``, retrying retryable :class:`LLMError`s.

    ``operation`` is a zero-argument coroutine function, so the caller closes
    over its own arguments and this helper stays call-shape agnostic. Only
    ``LLMError`` is caught — anything else is our bug, not a provider blip.

    ``attempt_scope(attempt)`` wraps each attempt (0-based) in a context manager
    so every attempt ends exactly once, cancellations included. ``__enter__``
    may yield a callable that receives a successful attempt's result before the
    scope closes; yield ``None`` (as ``nullcontext`` does) to opt out. The scope
    wraps only the provider call, never the backoff sleep.
    """
    last_error: LLMError | None = None
    for attempt in range(policy.max_attempts):
        scope = attempt_scope(attempt) if attempt_scope is not None else nullcontext()
        try:
            with scope as record_result:
                result = await operation()
                if record_result is not None:
                    # Inside the scope on purpose: the span is still open, so a
                    # consumer can set its attributes directly.
                    record_result(result)
                return result
        except LLMError as exc:
            last_error = exc
            if not exc.retryable:
                # Auth failures, bad requests, malformed responses: the same
                # call fails the same way, so surface it having slept zero.
                raise
            if attempt == policy.max_attempts - 1:
                raise
            await sleep(backoff_seconds(attempt, policy, exc.retry_after, rand))

    # Unreachable: the loop either returns or raises on the final attempt. Kept
    # so there is no implicit `return None` path to fall through into.
    raise last_error if last_error is not None else RuntimeError("retry loop exited without result")


__all__ = [
    "RetryPolicy",
    "DEFAULT_POLICY",
    "RETRY_AFTER_JITTER_FRACTION",
    "backoff_seconds",
    "acall_with_retry",
]
