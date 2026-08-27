"""Phase 3 shares one cooldown gate per run, on both copy paths.

The gate's own behaviour lives in tests_llm_cooldown.py; these pin the plumbing:
one gate per `plan_outreach` run, capped by OUTREACH_MAX_BACKOFF_S, waited on
before every provider attempt and told about every rate limit. Helpers are
duplicated from the retry suite because component modules do not import each other.
"""

import asyncio
from unittest import mock

from django.test import TestCase, override_settings

from project.app.models import Lead
from project.app.services import outreach
from project.app.services.agent import loop as agent_loop
from project.app.services.agent import state, tools
from project.app.services.llm import LLMRateLimitError, LLMResult
from project.app.services.llm.base import LLMClient
from project.app.services.llm.cooldown import CooldownGate
from project.app.services.llm.runtime import get_planner_runtime

GOOD_COPY = (
    "Subject: A quick idea for your team\n\n"
    "Hi there,\n\n"
    "You have been steadily working through quotes in the portal, and I wanted "
    "to share one small change that usually helps agencies of your size get "
    "more of them over the line. It takes about fifteen minutes to walk "
    "through, and your producers can start using it the same day. I would "
    "rather show you than write it all out here, since the useful part is "
    "seeing it against your own book of business and your own workflow. Would "
    "you have time for a short call this week?\n\n"
    "Best,\nDana"
)

NO_SLEEP = {"OUTREACH_INITIAL_BACKOFF_S": 0.0, "OUTREACH_MAX_BACKOFF_S": 0.0}


def rate_limit(retry_after=None):
    return LLMRateLimitError(
        "Rate limit reached", provider="groq", status_code=429, retry_after=retry_after
    )


def _lead(lead_id="lead_001", **overrides):
    # demo_completed with no signup date -> complete_onboarding, the one
    # classification that is date-independent.
    defaults = dict(
        id=lead_id,
        agency_name=f"Agency {lead_id}",
        contact_name="Priya Nair",
        contact_email=f"{lead_id}@example.test",
        contact_phone="555-0000",
        state="CO",
        num_producers=4,
        years_in_business=12,
        estimated_book_size_usd=5_000_000,
        stage="demo_completed",
        signed_up_date=None,
    )
    defaults.update(overrides)
    return Lead.objects.create(**defaults)


class _ScriptedClient:
    """A provider that fails on cue, then succeeds; shared by every lead."""

    provider_name = "groq"

    def __init__(self, *errors, then=GOOD_COPY):
        self.script = list(errors)
        self.then = then
        self.attempts = 0

    async def agenerate(self, prompt, max_tokens=None, timeout=None):
        self.attempts += 1
        if self.script:
            raise self.script.pop(0)
        return LLMResult(text=self.then, provider=self.provider_name, model="scripted-model")

    async def aclose(self):
        return None


class _RateLimitedChatClient(LLMClient):
    """Every chat call draws a retryable 429 carrying Retry-After guidance."""

    provider_name = "groq"

    def __init__(self):
        super().__init__(model="fake-model", default_max_tokens=1)
        self.chat_calls = []

    def generate(self, prompt, max_tokens=None, timeout=None):
        raise AssertionError("agent path must not take the blocking seam")

    async def agenerate_chat(self, messages, *, tools=(), max_tokens=None, timeout=None):
        self.chat_calls.append(messages)
        raise rate_limit(retry_after=30.0)

    async def aclose(self):
        return None


def _recording_gate_cls(created):
    """A real CooldownGate that also counts waits and remembers observations."""

    class _RecordingGate(CooldownGate):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.waits = 0
            self.observed = []
            created.append(self)

        async def wait(self):
            self.waits += 1
            await super().wait()

        def observe(self, error):
            self.observed.append(error)
            super().observe(error)

    return _RecordingGate


def _with_client(client):
    return mock.patch.object(outreach, "get_llm_client", return_value=client)


@override_settings(COPY_VERIFY_LEVEL="off", **NO_SLEEP)
class SingleShotPathGateTests(TestCase):
    def test_one_gate_serves_every_lead_and_every_attempt_in_the_run(self):
        _lead("lead_001")
        _lead("lead_002")
        client = _ScriptedClient(rate_limit(retry_after=30.0))
        created = []

        with mock.patch.object(outreach, "CooldownGate", _recording_gate_cls(created)):
            with _with_client(client):
                outreach.plan_outreach()

        # One gate for the whole run, not one per lead.
        self.assertEqual(len(created), 1)
        gate = created[0]
        # Two leads, one 429 between them: three attempts, each behind the gate.
        self.assertEqual(client.attempts, 3)
        self.assertEqual(gate.waits, 3)
        self.assertEqual(len(gate.observed), 1)
        self.assertIsInstance(gate.observed[0], LLMRateLimitError)

    def test_the_gates_cap_is_the_runs_max_backoff_knob(self):
        # The same knob that caps per-attempt backoff caps the fleet pause, so
        # a hostile Retry-After cannot park the run (the pinned property in
        # tests_planner_retry).
        _lead()
        created = []

        with override_settings(OUTREACH_MAX_BACKOFF_S=7.5):
            with mock.patch.object(outreach, "CooldownGate", _recording_gate_cls(created)):
                with _with_client(_ScriptedClient()):
                    outreach.plan_outreach()

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].max_cooldown_s, 7.5)


@override_settings(COPY_VERIFY_LEVEL="off", OUTREACH_MAX_ATTEMPTS=2, **NO_SLEEP)
class AgentPathGateTests(TestCase):
    """The agent loop's provider retries ride the same gate as the single-shot path."""

    @classmethod
    def setUpTestData(cls):
        cls.lead = Lead.objects.create(
            id="lead_gate",
            agency_name="A",
            contact_name="C",
            contact_email="c@a.test",
            contact_phone="1",
            state="TX",
            num_producers=3,
            years_in_business=4,
            estimated_book_size_usd=1,
            stage="active_trial",
        )

    def test_run_agent_lead_waits_on_and_reports_to_the_gate(self):
        pks = state.create_lead_runs("run-gate-1", [self.lead.id])
        created = []
        gate = _recording_gate_cls(created)(0.0)

        outcome = asyncio.run(
            agent_loop.run_agent_lead(
                prompt="PROMPT",
                lead_run_pk=pks[self.lead.id],
                prior_steps=(),
                context=tools.build_tool_context(self.lead, (), (), (), None),
                client=_RateLimitedChatClient(),
                runtime=get_planner_runtime(),
                checkpoint=state.Checkpoint(),
                gate=gate,
            )
        )

        self.assertIsInstance(outcome.error, LLMRateLimitError)
        self.assertEqual(outcome.attempts, 2)  # OUTREACH_MAX_ATTEMPTS
        self.assertEqual(gate.waits, 2)
        self.assertEqual(len(gate.observed), 2)

    @override_settings(OUTREACH_AGENT_ENABLED=True)
    def test_an_agent_run_threads_the_planner_gate_through(self):
        created = []
        client = _RateLimitedChatClient()

        with mock.patch.object(outreach, "CooldownGate", _recording_gate_cls(created)):
            with _with_client(client):
                outreach.plan_outreach()

        self.assertEqual(len(created), 1)
        gate = created[0]
        self.assertEqual(len(client.chat_calls), 2)  # OUTREACH_MAX_ATTEMPTS
        self.assertEqual(gate.waits, 2)
        self.assertEqual(len(gate.observed), 2)
