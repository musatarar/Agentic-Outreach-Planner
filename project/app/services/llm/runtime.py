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

# Agentic copy step (MUS-29). Off by default: merged agent code is inert until
# a deployment opts in. The agent per-lead budget is separate from
# DEFAULT_PER_LEAD_TIMEOUT_S because an agent lead is several provider calls
# plus tool executions, not one retried call.
DEFAULT_AGENT_ENABLED = False
DEFAULT_AGENT_MAX_STEPS = 6
DEFAULT_AGENT_MAX_TOOL_CALLS = 8
DEFAULT_AGENT_PER_LEAD_TIMEOUT_S = 300.0

# Sanity ceiling on the pool. Not a capacity limit -- it is a typo guard. `>= 1`
# only bounds one end of the range, and `80000` for `8` would have the planner
# open eighty thousand outstanding provider calls, which is a self-inflicted
# outage rather than a fast run. Nothing plausible sits above this; a deployment
# that genuinely wants more has a provider conversation to have first.
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

    ``request_s`` bounds a single HTTP attempt and is handed to the adapter.
    ``per_lead_s`` bounds the *whole* retry loop -- every attempt plus every
    backoff sleep -- and is the one that matters under concurrency: a worker
    holding a semaphore slot is holding 1/N of the run's throughput, so a lead
    that keeps drawing retryable failures has to be given up on, not waited out.

    They are separate numbers rather than one derived from the other because
    ``max_attempts * request_s`` is a wild overestimate (attempts usually fail
    fast) and ``request_s`` alone is an underestimate (it ignores backoff). Both
    are honest bounds on different things; neither substitutes for the other.

    The nesting is **enforced**, not merely described. ``request_s=600`` with
    ``per_lead_s=5`` is two individually-plausible numbers that together give a
    100% failure rate -- every lead dies on the outer deadline before a single
    attempt can finish -- and raising one while forgetting the other is the most
    likely operator error here.

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
        if self.per_lead_s < self.request_s:
            raise ValueError(
                "Timeouts.per_lead_s must be at least Timeouts.request_s -- a "
                "per-lead budget shorter than one attempt fails every lead."
            )


@dataclass(frozen=True, slots=True)
class PlannerRuntime:
    """Everything one planner run needs to know about how hard to push.

    Resolved once, at the top of the run, and passed down. Three separate
    accessors would let a future edit re-read configuration per lead, which is
    exactly what :class:`Timeouts` claims not to happen -- an aggregate makes
    "decided once, before the run" structural rather than aspirational.

    It also gives the boot-time system check (``project/app/checks.py``) a
    single call site that exercises all seven settings.
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
    # and annotated via TYPE_CHECKING so mypy still knows `raise _bad(...)` is a
    # terminal path rather than raising some unknown `Exception`.
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

    Not folded into :class:`RetryPolicy` or :class:`Timeouts`: it is a property
    of the *run*, not of a single call, and it is the only one of the three the
    provider layer itself never sees -- the planner's semaphore is the sole
    consumer.
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

    The planner calls this at the top of a run and carries the result down. The
    boot-time system check calls it to turn a misconfiguration into a
    ``manage.py check`` failure instead of a 500 for whoever clicked "Run
    Outreach Plan".
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
