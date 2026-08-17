"""Django settings -> the knobs the concurrent planner runs on (MUS-26).

The only module under ``services/llm/`` that knows Django exists, and even here
the imports are function-local so importing it stays safe without Django.
Validation messages name the *setting*, not the dataclass field, so an operator
knows which environment variable to fix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .retry import (
    DEFAULT_INITIAL_BACKOFF_S,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_BACKOFF_S,
    DEFAULT_MULTIPLIER,
    RetryPolicy,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from django.core.exceptions import ImproperlyConfigured

# Fallbacks used when a settings module doesn't define the knob at all. The four
# retry values come from retry.py rather than being restated; `project/settings.py`
# necessarily states the same numbers again as env-var defaults, and
# `PlannerRuntimeDefaultsTests` pins the two lists together.
DEFAULT_MAX_IN_FLIGHT = 8
DEFAULT_REQUEST_TIMEOUT_S = 60.0
DEFAULT_PER_LEAD_TIMEOUT_S = 150.0

# Agentic copy step (MUS-29). Off by default. The agent per-lead budget is
# separate from DEFAULT_PER_LEAD_TIMEOUT_S because an agent lead is several
# provider calls plus tool executions, not one retried call.
DEFAULT_AGENT_ENABLED = False
DEFAULT_AGENT_MAX_STEPS = 6
DEFAULT_AGENT_MAX_TOOL_CALLS = 8
DEFAULT_AGENT_PER_LEAD_TIMEOUT_S = 300.0

# Sanity ceiling on the pool -- a typo guard, not a capacity limit: `>= 1` alone
# would let `80000` typed for `8` through.
MAX_IN_FLIGHT_CEILING = 256

SETTING_MAX_IN_FLIGHT = "OUTREACH_MAX_IN_FLIGHT"
SETTING_MAX_ATTEMPTS = "OUTREACH_MAX_ATTEMPTS"
SETTING_INITIAL_BACKOFF_S = "OUTREACH_INITIAL_BACKOFF_S"
SETTING_MAX_BACKOFF_S = "OUTREACH_MAX_BACKOFF_S"
SETTING_BACKOFF_MULTIPLIER = "OUTREACH_BACKOFF_MULTIPLIER"
SETTING_REQUEST_TIMEOUT_S = "OUTREACH_REQUEST_TIMEOUT_S"
SETTING_PER_LEAD_TIMEOUT_S = "OUTREACH_PER_LEAD_TIMEOUT_S"
SETTING_AGENT_ENABLED = "OUTREACH_AGENT_ENABLED"
SETTING_AGENT_MAX_STEPS = "OUTREACH_AGENT_MAX_STEPS"
SETTING_AGENT_MAX_TOOL_CALLS = "OUTREACH_AGENT_MAX_TOOL_CALLS"
SETTING_AGENT_PER_LEAD_TIMEOUT_S = "OUTREACH_AGENT_PER_LEAD_TIMEOUT_S"


@dataclass(frozen=True, slots=True)
class Timeouts:
    """Two nested deadlines around one lead's copy generation.

    ``request_s`` bounds a single HTTP attempt and is handed to the adapter;
    ``per_lead_s`` bounds the *whole* retry loop -- every attempt plus every
    backoff sleep. The nesting is enforced, not merely described: a per-lead
    budget shorter than one attempt fails every lead.
    """

    request_s: float = DEFAULT_REQUEST_TIMEOUT_S
    per_lead_s: float = DEFAULT_PER_LEAD_TIMEOUT_S

    def __post_init__(self) -> None:
        # Zero is rejected as firmly as negative: `asyncio.timeout(0)` means
        # "expire immediately", not "no deadline".
        if self.request_s <= 0:
            raise ValueError("Timeouts.request_s must be greater than 0.")
        if self.per_lead_s <= 0:
            raise ValueError("Timeouts.per_lead_s must be greater than 0.")
        if self.per_lead_s < self.request_s:
            raise ValueError(
                "Timeouts.per_lead_s must be at least Timeouts.request_s -- a "
                "per-lead budget shorter than one attempt fails every lead."
            )


@dataclass(frozen=True, slots=True)
class PlannerRuntime:
    """Everything one planner run needs to know about how hard to push.

    Resolved once, at the top of the run, and passed down -- an aggregate makes
    "decided once, before the run" structural. It also gives the boot-time
    system check (``project/app/checks.py``) one call site covering every knob.
    """

    max_in_flight: int
    retry: RetryPolicy
    timeouts: Timeouts
    # Agentic copy step (MUS-29): gate plus the three budgets bounding one
    # lead's loop — provider calls, tool executions, wall-clock seconds.
    # Defaulted so every existing construction site stays valid.
    agent_enabled: bool = DEFAULT_AGENT_ENABLED
    agent_max_steps: int = DEFAULT_AGENT_MAX_STEPS
    agent_max_tool_calls: int = DEFAULT_AGENT_MAX_TOOL_CALLS
    agent_per_lead_s: float = DEFAULT_AGENT_PER_LEAD_TIMEOUT_S


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


def _as_bool(name: str, default: bool) -> bool:
    # settings.py already folds the env string to a bool; a custom settings
    # module writing "true" (the string) would otherwise be truthy-on-accident.
    value = _setting(name, default)
    if not isinstance(value, bool):
        raise _bad(name, value, "a boolean")
    return value


def _bad(name: str, value: Any, expected: str) -> "ImproperlyConfigured":
    # Imported here, not at module scope, for the same reason `settings` is --
    # and annotated via TYPE_CHECKING so mypy knows `raise _bad(...)` is terminal.
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

    # Checked ahead of RetryPolicy's own __post_init__ purely so the message
    # names the environment variable: a better message, not the only guard.
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
    _require(
        per_lead_s >= request_s,
        SETTING_PER_LEAD_TIMEOUT_S,
        per_lead_s,
        f"at least {SETTING_REQUEST_TIMEOUT_S} ({request_s}) -- a per-lead "
        "budget shorter than one attempt fails every lead",
    )
    return Timeouts(request_s=request_s, per_lead_s=per_lead_s)


def get_max_in_flight() -> int:
    """How many provider calls the planner may have outstanding at once.

    A property of the *run*, not of a single call: the planner's semaphore is
    the sole consumer, and the provider layer never sees it.
    """
    value = _as_int(SETTING_MAX_IN_FLIGHT, DEFAULT_MAX_IN_FLIGHT)
    _require(value >= 1, SETTING_MAX_IN_FLIGHT, value, "at least 1")
    _require(
        value <= MAX_IN_FLIGHT_CEILING,
        SETTING_MAX_IN_FLIGHT,
        value,
        f"at most {MAX_IN_FLIGHT_CEILING}",
    )
    return value


def get_planner_runtime() -> PlannerRuntime:
    """Resolve every knob for one run, once.

    The planner calls this at the top of a run; the boot-time system check calls
    it so a misconfiguration fails ``manage.py check`` rather than a request.
    """
    timeouts = get_timeouts()

    agent_enabled = _as_bool(SETTING_AGENT_ENABLED, DEFAULT_AGENT_ENABLED)
    agent_max_steps = _as_int(SETTING_AGENT_MAX_STEPS, DEFAULT_AGENT_MAX_STEPS)
    agent_max_tool_calls = _as_int(SETTING_AGENT_MAX_TOOL_CALLS, DEFAULT_AGENT_MAX_TOOL_CALLS)
    agent_per_lead_s = _as_float(SETTING_AGENT_PER_LEAD_TIMEOUT_S, DEFAULT_AGENT_PER_LEAD_TIMEOUT_S)
    _require(agent_max_steps >= 1, SETTING_AGENT_MAX_STEPS, agent_max_steps, "at least 1")
    _require(
        agent_max_tool_calls >= 0,
        SETTING_AGENT_MAX_TOOL_CALLS,
        agent_max_tool_calls,
        "non-negative",
    )
    # Same nesting argument as Timeouts: the agent per-lead budget wraps whole
    # provider calls, so a value shorter than one attempt fails every lead.
    _require(
        agent_per_lead_s >= timeouts.request_s,
        SETTING_AGENT_PER_LEAD_TIMEOUT_S,
        agent_per_lead_s,
        f"at least {SETTING_REQUEST_TIMEOUT_S} ({timeouts.request_s}) -- an agent "
        "per-lead budget shorter than one attempt fails every lead",
    )

    return PlannerRuntime(
        max_in_flight=get_max_in_flight(),
        retry=get_retry_policy(),
        timeouts=timeouts,
        agent_enabled=agent_enabled,
        agent_max_steps=agent_max_steps,
        agent_max_tool_calls=agent_max_tool_calls,
        agent_per_lead_s=agent_per_lead_s,
    )


__all__ = [
    "Timeouts",
    "PlannerRuntime",
    "RetryPolicy",
    "DEFAULT_MAX_IN_FLIGHT",
    "DEFAULT_REQUEST_TIMEOUT_S",
    "DEFAULT_PER_LEAD_TIMEOUT_S",
    "DEFAULT_AGENT_ENABLED",
    "DEFAULT_AGENT_MAX_STEPS",
    "DEFAULT_AGENT_MAX_TOOL_CALLS",
    "DEFAULT_AGENT_PER_LEAD_TIMEOUT_S",
    "MAX_IN_FLIGHT_CEILING",
    "get_retry_policy",
    "get_timeouts",
    "get_max_in_flight",
    "get_planner_runtime",
]
