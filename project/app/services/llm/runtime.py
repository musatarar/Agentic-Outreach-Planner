"""Django settings -> the knobs the concurrent planner runs on (MUS-26).

The original ticket said these belong in ``config.toml``. That file no longer
exists: MUS-32 moved provider/model/key resolution into the database, so the
only configuration surface left for *operational* knobs is Django settings read
from the environment -- the ``COPY_VERIFY_LEVEL`` precedent in
``project/settings.py``. Provider selection is a product decision made in the
Settings UI; how hard to retry a 429 is a deployment decision made by whoever
runs the process. Different lifetimes, different homes.

**This module is the only place under ``services/llm/`` that knows Django
exists.** ``retry.py`` deliberately does not: it is imported by the eval
harness and by unit tests that never call ``django.setup()``, and a top-level
``from django.conf import settings`` there would make the whole retry schedule
untestable without a configured project. So the dependency points one way --
this module reads settings and hands the LLM layer plain frozen dataclasses --
and even here the import is function-local, so importing this module is itself
safe without Django.

Validation names the *setting*, not the dataclass field. A misconfigured
deployment reads the traceback, and ``RetryPolicy.multiplier must be at least
1`` does not tell an operator which environment variable to go and fix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .retry import (
    DEFAULT_INITIAL_BACKOFF_S,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_BACKOFF_S,
    DEFAULT_MULTIPLIER,
    RetryPolicy,
)

# Fallbacks used when a settings module doesn't define the knob at all (a custom
# settings module, or a test that deletes one). The four retry values are
# re-exported from retry.py rather than restated, so the schedule has exactly one
# definition; the three below have no prior home and are defined here.
#
# `project/settings.py` states the same numbers again as env-var defaults --
# unavoidable, since settings cannot import app code at settings-load time --
# and `PlannerRuntimeDefaultsTests` pins the two lists together.
DEFAULT_MAX_IN_FLIGHT = 8
DEFAULT_REQUEST_TIMEOUT_S = 60.0
DEFAULT_PER_LEAD_TIMEOUT_S = 150.0

SETTING_MAX_IN_FLIGHT = "OUTREACH_MAX_IN_FLIGHT"
SETTING_MAX_ATTEMPTS = "OUTREACH_MAX_ATTEMPTS"
SETTING_INITIAL_BACKOFF_S = "OUTREACH_INITIAL_BACKOFF_S"
SETTING_MAX_BACKOFF_S = "OUTREACH_MAX_BACKOFF_S"
SETTING_BACKOFF_MULTIPLIER = "OUTREACH_BACKOFF_MULTIPLIER"
SETTING_REQUEST_TIMEOUT_S = "OUTREACH_REQUEST_TIMEOUT_S"
SETTING_PER_LEAD_TIMEOUT_S = "OUTREACH_PER_LEAD_TIMEOUT_S"


@dataclass(frozen=True, slots=True)
class Timeouts:
    """Two nested deadlines around one lead's copy generation.

    ``request_s`` bounds a single HTTP attempt and is handed to the adapter.
    ``per_lead_s`` bounds the *whole* retry loop -- every attempt plus every
    backoff sleep -- and is the one that matters under concurrency: a worker
    holding a semaphore slot is holding 1/N of the run's throughput, so a lead
    that keeps drawing retryable failures has to be given up on, not waited out.

    They are separate numbers rather than one derived from the other because
    ``max_attempts * request_s`` is a wild overestimate (attempts usually fail
    fast) and ``request_s`` alone is an underestimate (it ignores backoff). Both
    are honest bounds on different things; neither substitutes for the other.

    Frozen for the same reason :class:`RetryPolicy` is: a run's deadlines are
    decided once, before the run, and nothing inside it gets to renegotiate.
    """

    request_s: float = DEFAULT_REQUEST_TIMEOUT_S
    per_lead_s: float = DEFAULT_PER_LEAD_TIMEOUT_S

    def __post_init__(self) -> None:
        # Zero is rejected as firmly as negative. `asyncio.timeout(0)` doesn't
        # mean "no deadline", it means "expire immediately" -- so a deployment
        # that set 0 hoping to disable the bound would instead fail every single
        # lead, which is the most confusing possible reading of the value.
        if self.request_s <= 0:
            raise ValueError("Timeouts.request_s must be greater than 0.")
        if self.per_lead_s <= 0:
            raise ValueError("Timeouts.per_lead_s must be greater than 0.")


def _setting(name: str, default: Any) -> Any:
    # Imported inside the function so this module is importable without Django
    # configured -- see the module docstring.
    from django.conf import settings

    return getattr(settings, name, default)


def _as_int(name: str, default: int) -> int:
    value = _setting(name, default)
    # bool is an int subclass; `OUTREACH_MAX_IN_FLIGHT=True` silently becoming a
    # semaphore of 1 is exactly the kind of thing nobody ever finds.
    if isinstance(value, bool) or not isinstance(value, int):
        raise _bad(name, value, "a whole number")
    return value


def _as_float(name: str, default: float) -> float:
    value = _setting(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _bad(name, value, "a number")
    return float(value)


def _bad(name: str, value: Any, expected: str) -> Exception:
    from django.core.exceptions import ImproperlyConfigured

    return ImproperlyConfigured(f"{name} must be {expected}, got {value!r}.")


def _require(condition: bool, name: str, value: Any, requirement: str) -> None:
    if not condition:
        raise _bad(name, value, requirement)


def get_retry_policy() -> RetryPolicy:
    """Build the run's :class:`RetryPolicy` from Django settings."""
    max_attempts = _as_int(SETTING_MAX_ATTEMPTS, DEFAULT_MAX_ATTEMPTS)
    initial_backoff_s = _as_float(SETTING_INITIAL_BACKOFF_S, DEFAULT_INITIAL_BACKOFF_S)
    max_backoff_s = _as_float(SETTING_MAX_BACKOFF_S, DEFAULT_MAX_BACKOFF_S)
    multiplier = _as_float(SETTING_BACKOFF_MULTIPLIER, DEFAULT_MULTIPLIER)

    # Checked here, ahead of RetryPolicy's own __post_init__, purely so the
    # message names the environment variable an operator can act on. The
    # dataclass still validates -- it is constructed by callers that never came
    # through this function -- so this is a better message, not the only guard.
    _require(max_attempts >= 1, SETTING_MAX_ATTEMPTS, max_attempts, "at least 1")
    _require(initial_backoff_s >= 0, SETTING_INITIAL_BACKOFF_S, initial_backoff_s, "non-negative")
    _require(max_backoff_s >= 0, SETTING_MAX_BACKOFF_S, max_backoff_s, "non-negative")
    _require(multiplier >= 1, SETTING_BACKOFF_MULTIPLIER, multiplier, "at least 1")

    return RetryPolicy(
        max_attempts=max_attempts,
        initial_backoff_s=initial_backoff_s,
        max_backoff_s=max_backoff_s,
        multiplier=multiplier,
    )


def get_timeouts() -> Timeouts:
    """Build the run's :class:`Timeouts` from Django settings."""
    request_s = _as_float(SETTING_REQUEST_TIMEOUT_S, DEFAULT_REQUEST_TIMEOUT_S)
    per_lead_s = _as_float(SETTING_PER_LEAD_TIMEOUT_S, DEFAULT_PER_LEAD_TIMEOUT_S)
    _require(request_s > 0, SETTING_REQUEST_TIMEOUT_S, request_s, "greater than 0")
    _require(per_lead_s > 0, SETTING_PER_LEAD_TIMEOUT_S, per_lead_s, "greater than 0")
    return Timeouts(request_s=request_s, per_lead_s=per_lead_s)


def get_max_in_flight() -> int:
    """How many provider calls the planner may have outstanding at once.

    Not folded into :class:`RetryPolicy` or :class:`Timeouts`: it is a property
    of the *run*, not of a single call, and it is the only one of the three the
    provider layer itself never sees -- the planner's semaphore is the sole
    consumer.
    """
    value = _as_int(SETTING_MAX_IN_FLIGHT, DEFAULT_MAX_IN_FLIGHT)
    _require(value >= 1, SETTING_MAX_IN_FLIGHT, value, "at least 1")
    return value


__all__ = [
    "Timeouts",
    "RetryPolicy",
    "DEFAULT_MAX_IN_FLIGHT",
    "DEFAULT_REQUEST_TIMEOUT_S",
    "DEFAULT_PER_LEAD_TIMEOUT_S",
    "get_retry_policy",
    "get_timeouts",
    "get_max_in_flight",
]
