"""A fake provider, for benchmarking the planner without calling anyone (MUS-26e).

The concurrency work in MUS-26 is worth exactly as much as the wall-clock it
saves, and measuring that against a real provider is useless: Groq's free tier
is rate-limited, so a 200-lead run measures the throttle rather than the pool.
This client sleeps for a configurable, seeded duration and returns a canned
email, which makes the benchmark reproducible and free.

**Two things this is careful about, because a fake provider is a very easy thing
to get wrong in a way that flatters the number it produces.**

*It returns a well-formed, grounded email*, not a placeholder. The planner runs
two output gates on whatever comes back -- ``validate_copy`` (shape) and
``verify.verify_copy`` (grounding) -- and both walk the text. A stub returning
``"ok"`` would fail both, sending every lead down the short branch, and the
benchmark would be timing a code path the demo never takes. So the canned copy
interpolates the lead's real name and agency out of the prompt, carries a
subject line, exactly one call to action, and a body inside the 60-200 word
window, and makes no numeric claim the verifier could contradict.

*It reports plausible token counts*, so MUS-25's token metric has something to
record end to end rather than a hole where the interesting number goes.

**It cannot be selected by accident.** Three independent things have to be true,
and the first is the one that matters:

1. ``get_llm_client()`` resolves the provider from an ``LLMConfiguration`` row
   whose ``provider`` is a foreign key to a real ``LLMProvider`` row.
   ``seed_llm_catalog`` creates exactly four, and "stub" is not among them --
   so no database this project produces can point at it, and the Settings UI,
   which lists providers from that same table, can never show it. (The old
   ``LLM_CONFIG_PATH`` belt is gone along with ``config.toml``; this replaces
   it, and is strictly stronger, because a file could be edited and a foreign
   key cannot be satisfied by wishing.)
2. ``StubClient.__init__`` raises unless ``OUTREACH_ALLOW_STUB_LLM=1``. Only
   ``evals/bench_planner.py`` sets it.
3. The benchmark reaches this class through ``build_client("stub")`` and injects
   it directly, so even it never writes a "stub" row anywhere.

It is registered in ``_REGISTRY`` anyway, so it is constructed by the same
factory as every real adapter and cannot drift away from their interface.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import time

from .base import FINISH_STOP, LLMClient, LLMResult
from .errors import LLMRateLimitError, LLMTransientError

# The gate. Deliberately an environment variable rather than a Django setting:
# a setting would appear in `.env.example` and in the README's configuration
# table, which is the opposite of what this needs.
ALLOW_ENV_VAR = "OUTREACH_ALLOW_STUB_LLM"

# Matches Groq's measured median in the committed copy-eval table (README:
# "Latency (med) 1.87s"), so `--concurrency 1` reports a number in the same
# neighbourhood as the real thing rather than an invented one.
DEFAULT_LATENCY_MEAN_S = 1.87
DEFAULT_LATENCY_STDDEV_S = 0.45

# Never sleep less than this. A gaussian around 1.87 with a 0.45 spread has a
# thin negative tail, and a negative sleep is an error rather than a fast call.
MIN_LATENCY_S = 0.01

PROVIDER_NAME = "stub"
DEFAULT_MODEL = "stub-1"

# Pulled out of the prompt so the canned email is about the actual lead. The
# stub is handed a prompt and nothing else -- exactly like a real provider --
# and that is the point: interpolating a name it had to *find* proves the
# prompt-building phase ran, where a hardcoded "Hi there" would not.
_CONTACT_RE = re.compile(r"^- Contact: ([^(\n]+?)\s*\(", re.MULTILINE)
_AGENCY_RE = re.compile(r"^- Agency: ([^(\n]+?)\s*\(", re.MULTILINE)


class StubLLMNotAllowed(RuntimeError):
    """Raised when the stub provider is built without the explicit opt-in."""


class StubClient(LLMClient):
    """An ``LLMClient`` that sleeps and returns a canned, grounded email.

    ``latency_mean_s`` / ``latency_stddev_s`` shape a gaussian per call.
    ``rate_limit_rate`` and ``failure_rate`` are probabilities in ``[0, 1]`` of
    raising a retryable :class:`LLMRateLimitError` / :class:`LLMTransientError`
    -- both retryable on purpose, so a benchmark run with them switched on
    measures the retry path rather than turning into a run of failed leads.
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
        # Its own Random, not the module-global one: a benchmark must not move
        # the global stream out from under anything else in the process, and a
        # seeded global would be a spooky action at a distance for whatever runs
        # next. Note the *order* of draws still depends on task scheduling under
        # concurrency, so a seed reproduces the distribution, not the sequence.
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
        # `asyncio.sleep`, not `time.sleep`. A stub that blocked the loop would
        # report a peak concurrency of 1 and make the benchmark measure nothing
        # -- which is the single most likely way for this file to quietly lie.
        await asyncio.sleep(latency_s)
        return self._result(prompt, latency_s)

    # -- internals ----------------------------------------------------------

    def _next_latency(self):
        self.calls += 1
        return max(MIN_LATENCY_S, self._random.gauss(self.latency_mean_s, self.latency_stddev_s))

    def _maybe_fail(self):
        # Drawn once and compared twice against cumulative bands, so the two
        # rates never interact: rate_limit_rate=0.5 with failure_rate=0.5 means
        # half and half, not 0.5 then 0.5-of-the-rest.
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
            # Roughly the four-characters-per-token rule the copy eval already
            # uses for its estimate. Not exact, and not pretending to be -- the
            # point is that the token metric records a plausible number rather
            # than a None.
            input_tokens=max(1, len(prompt) // 4),
            output_tokens=max(1, len(text) // 4),
            finish_reason=FINISH_STOP,
            raw_finish_reason="stop",
            latency_s=latency_s,
        )


def canned_email(prompt):
    """A well-formed, grounded outreach email for the lead named in ``prompt``.

    Everything about the wording is chosen so the planner's two output gates do
    real work on it and pass:

    * a ``Subject:`` line and no preamble (shape gate);
    * exactly one call-to-action sentence (shape gate -- the second-person
      question at the end is the only one);
    * a body between 60 and 200 words (shape gate);
    * **no numeric claim at all** (grounding gate). The verifier's job is to
      catch a model inventing "your 47 closed deals"; a stub that invented one
      would route every benchmark lead to a human and turn the run into a
      measurement of the failure path.
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
