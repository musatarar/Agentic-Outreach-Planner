"""A fake provider, for benchmarking the planner without calling anyone (MUS-26e).

Sleeps for a configurable, seeded duration and returns a canned, grounded email
with plausible token counts, so a benchmark measures the pool rather than a free
tier's throttle -- and passes the planner's shape and grounding gates instead of
timing a failure path. It cannot be selected by accident: ``seed_llm_catalog``
never creates a "stub" provider row for a configuration to point at, and
``__init__`` raises unless ``OUTREACH_ALLOW_STUB_LLM=1`` (only
``evals/bench_planner.py`` sets it, and it injects the client directly). It is
registered in ``_REGISTRY`` anyway, so it cannot drift from the real adapters'
interface.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import time
from collections.abc import Sequence

from .base import FINISH_STOP, FINISH_TOOL_CALLS, LLMClient, LLMResult
from .chat_types import Message, ToolCallRequest, ToolSpec
from .errors import LLMRateLimitError, LLMTransientError

# The gate. An environment variable rather than a Django setting, which would
# advertise itself in `.env.example` and the README configuration table.
ALLOW_ENV_VAR = "OUTREACH_ALLOW_STUB_LLM"

# Groq's measured median in the committed copy-eval table (README: "Latency
# (med) 1.87s"), so `--concurrency 1` reports a realistic number.
DEFAULT_LATENCY_MEAN_S = 1.87
DEFAULT_LATENCY_STDDEV_S = 0.45

# Floor: the gaussian has a thin negative tail, and a negative sleep is an error.
MIN_LATENCY_S = 0.01

PROVIDER_NAME = "stub"
DEFAULT_MODEL = "stub-1"

# Pulled out of the prompt so the canned email is about the actual lead --
# interpolating a name it had to *find* proves the prompt-building phase ran.
_CONTACT_RE = re.compile(r"^- Contact: ([^(\n]+?)\s*\(", re.MULTILINE)
_AGENCY_RE = re.compile(r"^- Agency: ([^(\n]+?)\s*\(", re.MULTILINE)


class StubLLMNotAllowed(RuntimeError):
    """Raised when the stub provider is built without the explicit opt-in."""


class StubClient(LLMClient):
    """An ``LLMClient`` that sleeps and returns a canned, grounded email.

    ``latency_mean_s`` / ``latency_stddev_s`` shape a gaussian per call.
    ``rate_limit_rate`` and ``failure_rate`` are probabilities in ``[0, 1]`` of
    raising a retryable :class:`LLMRateLimitError` / :class:`LLMTransientError`
    -- retryable on purpose, so switching them on measures the retry path.
    ``seed`` makes all of it reproducible.
    """

    provider_name = PROVIDER_NAME

    def __init__(
        self,
        model=DEFAULT_MODEL,
        default_max_tokens=500,
        api_key=None,
        latency_mean_s=DEFAULT_LATENCY_MEAN_S,
        latency_stddev_s=DEFAULT_LATENCY_STDDEV_S,
        rate_limit_rate=0.0,
        failure_rate=0.0,
        seed=None,
    ):
        if os.environ.get(ALLOW_ENV_VAR) != "1":
            raise StubLLMNotAllowed(
                f"The stub LLM provider is for benchmarking only. Set {ALLOW_ENV_VAR}=1 "
                "to build one (evals/bench_planner.py does). If you are seeing this "
                "from the app, something has selected provider 'stub' -- which should "
                "not be reachable, since seed_llm_catalog never creates that provider "
                "row."
            )
        super().__init__(model=model, default_max_tokens=default_max_tokens, api_key=api_key)
        self.latency_mean_s = latency_mean_s
        self.latency_stddev_s = latency_stddev_s
        self.rate_limit_rate = rate_limit_rate
        self.failure_rate = failure_rate
        # Its own Random, not the module-global stream. Under concurrency the
        # *order* of draws still follows task scheduling, so a seed reproduces
        # the distribution, not the sequence.
        self._random = random.Random(seed)
        self.calls = 0

    # -- the two call paths -------------------------------------------------

    def generate(self, prompt, max_tokens=None, timeout=None) -> LLMResult:
        latency_s = self._next_latency()
        self._maybe_fail()
        time.sleep(latency_s)
        return self._result(prompt, latency_s)

    async def agenerate(self, prompt, max_tokens=None, timeout=None) -> LLMResult:
        latency_s = self._next_latency()
        self._maybe_fail()
        # `asyncio.sleep`, not `time.sleep`: blocking the loop would report a
        # peak concurrency of 1 and make the benchmark measure nothing.
        await asyncio.sleep(latency_s)
        return self._result(prompt, latency_s)

    async def agenerate_chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResult:
        """Scripted chat turn: one tool call first, then the canned email.

        Stateless on purpose — clients are ``lru_cache``d singletons shared
        across concurrent leads, so the script derives from the *message list*:
        tools offered with no ``tool_result`` yet means "ask for the first
        tool", otherwise answer with the canned email.
        """
        latency_s = self._next_latency()
        self._maybe_fail()
        await asyncio.sleep(latency_s)
        prompt = next((m.content for m in messages if m.role == "user"), "")
        if tools and not any(m.role == "tool_result" for m in messages):
            spec = tools[0]
            return LLMResult(
                text="",
                provider=self.provider_name,
                model=self.model,
                response_model=self.model,
                input_tokens=max(1, len(prompt) // 4),
                output_tokens=1,
                finish_reason=FINISH_TOOL_CALLS,
                raw_finish_reason="tool_calls",
                latency_s=latency_s,
                tool_calls=(ToolCallRequest(id="stub_call_1", name=spec.name, arguments={}),),
            )
        return self._result(prompt, latency_s)

    # -- internals ----------------------------------------------------------

    def _next_latency(self):
        self.calls += 1
        return max(MIN_LATENCY_S, self._random.gauss(self.latency_mean_s, self.latency_stddev_s))

    def _maybe_fail(self):
        # One draw compared against cumulative bands, so the two rates never
        # interact: 0.5/0.5 means half and half, not 0.5 then 0.5-of-the-rest.
        draw = self._random.random()
        if draw < self.rate_limit_rate:
            raise LLMRateLimitError(
                "Stub provider: simulated rate limit.",
                provider=self.provider_name,
                status_code=429,
                retry_after=0.05,
            )
        if draw < self.rate_limit_rate + self.failure_rate:
            raise LLMTransientError(
                "Stub provider: simulated upstream failure.",
                provider=self.provider_name,
                status_code=503,
            )

    def _result(self, prompt, latency_s) -> LLMResult:
        text = canned_email(prompt)
        return LLMResult(
            text=text,
            provider=self.provider_name,
            model=self.model,
            response_model=self.model,
            # The four-characters-per-token estimate the copy eval already uses
            # -- plausible, not exact.
            input_tokens=max(1, len(prompt) // 4),
            output_tokens=max(1, len(text) // 4),
            finish_reason=FINISH_STOP,
            raw_finish_reason="stop",
            latency_s=latency_s,
        )


def canned_email(prompt):
    """A well-formed, grounded outreach email for the lead named in ``prompt``.

    Worded to pass the planner's two output gates: a ``Subject:`` line with no
    preamble, exactly one call-to-action sentence and a 60-200 word body (shape
    gate), and no numeric claim at all (grounding gate).
    """
    contact = _first_group(_CONTACT_RE, prompt, "there")
    agency = _first_group(_AGENCY_RE, prompt, "your agency")
    return (
        f"Subject: A quick thought for {agency}\n"
        "\n"
        f"Hi {contact},\n"
        "\n"
        f"I have been looking at how {agency} is working through the portal, and "
        "there is one small change that tends to help agencies at your stage get "
        "more of their quotes over the line. It takes about fifteen minutes to "
        "walk through, and your producers can start using it the same day without "
        "any change to how they already work. I would rather show you than write "
        "it all out here, since the useful part is seeing it against your own book "
        "of business rather than a generic example. Would you have time for a "
        "short call this week?\n"
        "\n"
        "Best,\n"
        "Dana\n"
        "Locked In"
    )


def _first_group(pattern, text, fallback):
    match = pattern.search(text or "")
    if match is None:
        return fallback
    return match.group(1).strip() or fallback


__all__ = [
    "StubClient",
    "StubLLMNotAllowed",
    "ALLOW_ENV_VAR",
    "PROVIDER_NAME",
    "canned_email",
]
