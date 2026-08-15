"""Component artifact: assembly (MUS-29) — the acceptance criteria as tests.

A run produces an inspectable trace per lead, survives a mid-run kill without
losing or duplicating work, and the agent gathers context without gaining
authority: crash-then-resume, contested claims, the resume endpoint, and the
red-team fencing of tool results.

``_tool_result``/``_final`` are module-level copies of the same-named helpers in
tests_agent_loop_loop.py (LLMResult builders; component modules do not import
each other).
"""

import dataclasses
import inspect
import re
from datetime import datetime
from datetime import timezone as dt_timezone
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from evals import redteam_payloads
from project.app import models as app_models  # AgentLeadRun/AgentStep: lazy until Task 3 lands
from project.app.models import Event, Lead, OutreachAction
from project.app.services import outreach, sanitize
from project.app.services.agent import loop as agent_loop
from project.app.services.agent import state, tools
from project.app.services.llm.base import FINISH_STOP, FINISH_TOOL_CALLS, LLMClient, LLMResult
from project.app.services.llm.chat_types import ToolCallRequest
from project.app.services.llm.errors import LLMAuthError
from project.app.services.llm.runtime import get_planner_runtime
from project.app.services.llm.stub import canned_email
from project.app.tests_auth_utils import AuthenticatedAPITestCase

# Same channel as tests_planner_async._agency_of: phase 3 hands the client only a
# prompt, whose trusted section carries "- Agency: SYNTH-NNN", and _seed_leads
# below pairs agency SYNTH-NNN with id lead_NNN.
_AGENCY_IN_PROMPT = re.compile(r"^- Agency: (SYNTH-\d+)", re.MULTILINE)


def _lead_of(prompt):
    match = _AGENCY_IN_PROMPT.search(prompt)
    if match is None:
        raise AssertionError(f"could not identify the lead: {prompt[:200]!r}")
    return "lead_" + match.group(1).removeprefix("SYNTH-")


def _tool_result(name="get_lead_history"):
    return LLMResult(
        text="",
        provider="fake",
        model="m",
        finish_reason=FINISH_TOOL_CALLS,
        raw_finish_reason="tool_use",
        tool_calls=(ToolCallRequest(id="c1", name=name, arguments={}),),
    )


def _final(text="Subject: Hi\n\nBody."):
    return LLMResult(
        text=text,
        provider="fake",
        model="m",
        finish_reason=FINISH_STOP,
        raw_finish_reason="end_turn",
    )


def _seed_leads(count):
    """Seed leads exactly as tests_planner_async._make_leads does: ids
    lead_000.., agencies SYNTH-000.., stage demo_completed with no signup date —
    the one date-independent classification (complete_onboarding)."""
    return [
        Lead.objects.create(
            id=f"lead_{index:03d}",
            agency_name=f"SYNTH-{index:03d}",
            contact_name=f"Contact {index:03d}",
            contact_email=f"contact{index:03d}@example.com",
            contact_phone="555-0100",
            state="CA",
            num_producers=3,
            years_in_business=8,
            estimated_book_size_usd=1_000_000 + index,
            stage="demo_completed",
            signed_up_date=None,
        )
        for index in range(count)
    ]


class _ScriptedChatClient(LLMClient):
    provider_name = "fake"

    def __init__(self, scripts, crash_after=None):
        super().__init__(model="fake-model", default_max_tokens=1)
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.crash_after, self.served, self.chat_calls = crash_after, 0, []

    def generate(self, prompt, max_tokens=None, timeout=None):
        raise AssertionError("agent path must not take the blocking seam")

    async def agenerate_chat(self, messages, *, tools=(), max_tokens=None, timeout=None):
        if self.crash_after is not None and self.served >= self.crash_after:
            raise RuntimeError("KILL")  # a hard kill: not an LLMError, so it propagates
        self.served += 1
        self.chat_calls.append({"messages": list(messages), "tools": tuple(tools)})
        return self.scripts[_lead_of(messages[0].content)].pop(0)


class _OutageChatClient(LLMClient):
    """The provider is up but refusing every call — the shape a rate-limited or
    unauthenticated run takes. `LLMError` is a value on the agent path, so each
    run checkpoints `failed` and phase 5 writes a failed-generation row."""

    provider_name = "fake"

    def __init__(self):
        super().__init__(model="fake-model", default_max_tokens=1)
        self.chat_calls = []

    def generate(self, prompt, max_tokens=None, timeout=None):
        raise AssertionError("agent path must not take the blocking seam")

    async def agenerate_chat(self, messages, *, tools=(), max_tokens=None, timeout=None):
        self.chat_calls.append(messages)
        raise LLMAuthError("the provider refused this call")


class _SingleShotClient(LLMClient):
    """Flag-off double: the existing async single-shot seam, and a tripwire on
    the chat path."""

    provider_name = "fake"

    def __init__(self):
        super().__init__(model="fake-model", default_max_tokens=1)
        self.copy_calls = 0

    def generate(self, prompt, max_tokens=None, timeout=None):
        raise AssertionError("planner phase 3 is async-only")

    async def agenerate(self, prompt, max_tokens=None, timeout=None):
        self.copy_calls += 1
        return _final(canned_email(prompt))

    async def agenerate_chat(self, messages, *, tools=(), max_tokens=None, timeout=None):
        raise AssertionError("flag off: the agent chat path must not be taken")


@override_settings(OUTREACH_AGENT_ENABLED=True)
class CrashResumeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _seed_leads(3)

    def _fake_chat(self, crash_after=None):
        # Two chat calls per lead (one tool call, then a final): 3 leads = 6
        # calls total. crash_after=4 serves calls 1-4 then raises KILL, so under
        # any interleaving of the bounded pool at least one lead has persisted
        # its `final` step and at least one has not.
        return _ScriptedChatClient(
            {
                lead_id: [_tool_result(), _final(f"Subject: Hi\n\nBody for {lead_id}.")]
                for lead_id in ("lead_000", "lead_001", "lead_002")
            },
            crash_after=crash_after,
        )

    def test_flag_off_takes_the_single_shot_path_untouched(self):
        self.assertTrue(hasattr(app_models, "AgentLeadRun"))  # red at skeleton
        client = _SingleShotClient()
        with (
            override_settings(OUTREACH_AGENT_ENABLED=False),
            mock.patch.object(outreach, "get_llm_client", return_value=client),
        ):
            planned = outreach.plan_outreach()
        self.assertTrue(planned)
        self.assertGreater(client.copy_calls, 0)
        self.assertEqual(app_models.AgentLeadRun.objects.count(), 0)

    def test_kill_then_resume_loses_no_work_and_duplicates_nothing(self):
        self.assertTrue(hasattr(app_models, "AgentLeadRun"))  # red at skeleton
        client = self._fake_chat(crash_after=4)  # dies after 4 of the 6 scripted calls
        with mock.patch.object(outreach, "get_llm_client", return_value=client):
            with self.assertRaises(RuntimeError):
                outreach.plan_outreach()
        run_id = app_models.AgentLeadRun.objects.values_list("trace_run_id", flat=True).first()
        self.assertEqual(OutreachAction.objects.filter(trace_run_id=run_id).count(), 0)
        done_before = set(
            app_models.AgentLeadRun.objects.filter(status="done").values_list("lead_id", flat=True)
        )
        self.assertTrue(done_before)  # crash_after=4 guarantees ≥ 1
        resumed = self._fake_chat()  # fresh client, fresh counter
        with mock.patch.object(outreach, "get_llm_client", return_value=resumed):
            outreach.plan_outreach(resume_run_id=run_id)
        # (a) finished leads were replayed from the log, not re-billed — no chat
        #     call on resume may target a lead whose run was already done:
        for call in resumed.chat_calls:
            lead_id = _lead_of(call["messages"][0].content)
            self.assertNotIn(lead_id, done_before)
        # (b) exactly one action per (run, lead):
        self.assertEqual(OutreachAction.objects.filter(trace_run_id=run_id).count(), 3)
        # (c) every lead's step log is gapless:
        for run in app_models.AgentLeadRun.objects.filter(trace_run_id=run_id):
            seqs = list(run.steps.order_by("seq").values_list("seq", flat=True))
            self.assertEqual(seqs, list(range(1, len(seqs) + 1)))

    def test_lost_claim_produces_no_duplicate_row_and_a_later_resume_completes(self):
        self.assertTrue(hasattr(app_models, "AgentLeadRun"))  # red at skeleton
        client = self._fake_chat(crash_after=0)  # every call dies: runs exist, no steps
        with mock.patch.object(outreach, "get_llm_client", return_value=client):
            with self.assertRaises(RuntimeError):
                outreach.plan_outreach()
        run_id = app_models.AgentLeadRun.objects.values_list("trace_run_id", flat=True).first()
        contested_pk = app_models.AgentLeadRun.objects.get(
            trace_run_id=run_id, lead_id="lead_001"
        ).pk

        real_claim = state.Checkpoint.claim

        async def rigged(self, lead_run_pk):
            if lead_run_pk == contested_pk:
                return False  # a rival worker holds this lead's run
            return await real_claim(self, lead_run_pk)

        resumed = self._fake_chat()
        with (
            mock.patch.object(state.Checkpoint, "claim", rigged),
            mock.patch.object(outreach, "get_llm_client", return_value=resumed),
        ):
            outreach.plan_outreach(resume_run_id=run_id)
        # The loser wrote nothing for the contested lead — no row, no calls:
        self.assertEqual(
            OutreachAction.objects.filter(trace_run_id=run_id, lead_id="lead_001").count(), 0
        )
        for call in resumed.chat_calls:
            self.assertNotEqual(_lead_of(call["messages"][0].content), "lead_001")
        self.assertEqual(OutreachAction.objects.filter(trace_run_id=run_id).count(), 2)
        # With the rival gone, a later resume completes the lead exactly once:
        final_client = self._fake_chat()
        with mock.patch.object(outreach, "get_llm_client", return_value=final_client):
            outreach.plan_outreach(resume_run_id=run_id)
        self.assertEqual(OutreachAction.objects.filter(trace_run_id=run_id).count(), 3)
        self.assertEqual(
            OutreachAction.objects.filter(trace_run_id=run_id, lead_id="lead_001").count(), 1
        )

    def test_resume_recovers_a_run_checkpointed_terminal_before_the_crash(self):
        """MB2: `failed` and `exhausted` are outside NON_TERMINAL_STATUSES, so
        the claim CAS matches zero rows for a run that checkpointed one of them
        and then died before finalize wrote its `OutreachAction`. That surfaces
        as AgentClaimLost, which phase 5 drops — and nothing ever resets a
        terminal run, so the lead is lost from this resume and every later one.

        The contract defines AgentClaimLost as "another worker won ... whose own
        finalize writes this lead's row". Here no worker ever writes it, and the
        lead would have produced a needs_human row in the non-crash flow.
        """
        self.assertTrue(hasattr(app_models, "AgentLeadRun"))  # red at skeleton
        client = self._fake_chat(crash_after=0)  # every call dies: runs exist, no steps
        with mock.patch.object(outreach, "get_llm_client", return_value=client):
            with self.assertRaises(RuntimeError):
                outreach.plan_outreach()
        run_id = app_models.AgentLeadRun.objects.values_list("trace_run_id", flat=True).first()
        self.assertEqual(OutreachAction.objects.filter(trace_run_id=run_id).count(), 0)

        # The durable checkpoints that committed before the process died: one
        # lead's provider call failed, another ran out of its step budget.
        # Neither reached phase 5, so neither has a row to show for it.
        app_models.AgentLeadRun.objects.filter(trace_run_id=run_id, lead_id="lead_000").update(
            status=app_models.AgentLeadRun.STATUS_FAILED
        )
        app_models.AgentLeadRun.objects.filter(trace_run_id=run_id, lead_id="lead_001").update(
            status=app_models.AgentLeadRun.STATUS_EXHAUSTED
        )

        resumed = self._fake_chat()
        with mock.patch.object(outreach, "get_llm_client", return_value=resumed):
            outreach.plan_outreach(resume_run_id=run_id)

        # Every lead comes back — none silently vanishes from the resume:
        self.assertEqual(OutreachAction.objects.filter(trace_run_id=run_id).count(), 3)
        for lead_id in ("lead_000", "lead_001", "lead_002"):
            with self.subTest(lead_id=lead_id):
                self.assertEqual(
                    OutreachAction.objects.filter(trace_run_id=run_id, lead_id=lead_id).count(), 1
                )

    def test_resume_after_a_provider_outage_replaces_the_failure_rows(self):
        """Found by running the app against a rate-limiting provider: every lead
        failed, phase 5 wrote the failed-generation rows that tell the reviewer
        "re-run the planner once the provider recovers" — and the re-run deleted
        all of them and wrote nothing back.

        Phase 5 supersedes failed-generation rows by design
        (`failed_generation_filter`), but the leads behind them are dropped from
        the rows to write, because their runs are terminal and the claim CAS
        refuses them. Delete plus drop equals a lead that silently leaves the
        queue — strictly worse than the stale row it replaced, and the exact
        instruction the reviewer was given turns into data loss.

        A row recording a failed attempt is therefore *not* the "already
        finalized" that must be left alone: phase 5 itself treats it as
        replaceable.
        """
        self.assertTrue(hasattr(app_models, "AgentLeadRun"))  # red at skeleton
        with mock.patch.object(outreach, "get_llm_client", return_value=_OutageChatClient()):
            outreach.plan_outreach()
        run_id = app_models.AgentLeadRun.objects.values_list("trace_run_id", flat=True).first()
        rows = OutreachAction.objects.filter(trace_run_id=run_id)
        self.assertEqual(rows.count(), 3)
        self.assertEqual(rows.filter(needs_human=True, suggested_copy="").count(), 3)
        self.assertEqual(
            app_models.AgentLeadRun.objects.filter(trace_run_id=run_id, status="failed").count(), 3
        )

        recovered = self._fake_chat()  # the provider is back
        with mock.patch.object(outreach, "get_llm_client", return_value=recovered):
            outreach.plan_outreach(resume_run_id=run_id)

        # No lead leaves the queue, and each now carries the draft the re-run
        # was told to go and fetch.
        self.assertEqual(OutreachAction.objects.filter(trace_run_id=run_id).count(), 3)
        for lead_id in ("lead_000", "lead_001", "lead_002"):
            with self.subTest(lead_id=lead_id):
                action = OutreachAction.objects.get(trace_run_id=run_id, lead_id=lead_id)
                self.assertNotEqual(action.suggested_copy, "")

    def test_resume_leaves_a_terminal_run_that_already_finalized_alone(self):
        """The reset is conditioned on having no row, not on being terminal:
        a run that did reach phase 5 is finished work, and re-running it would
        re-bill the provider for a row the resume must not write twice."""
        self.assertTrue(hasattr(app_models, "AgentLeadRun"))  # red at skeleton
        client = self._fake_chat()
        with mock.patch.object(outreach, "get_llm_client", return_value=client):
            outreach.plan_outreach()
        run_id = app_models.AgentLeadRun.objects.values_list("trace_run_id", flat=True).first()
        self.assertEqual(OutreachAction.objects.filter(trace_run_id=run_id).count(), 3)
        # A run that finalized and was *then* marked failed (a checkpoint that
        # lost a race with its own finalize) still has nothing owed to it.
        app_models.AgentLeadRun.objects.filter(trace_run_id=run_id, lead_id="lead_000").update(
            status=app_models.AgentLeadRun.STATUS_FAILED
        )

        resumed = self._fake_chat()
        with mock.patch.object(outreach, "get_llm_client", return_value=resumed):
            outreach.plan_outreach(resume_run_id=run_id)

        self.assertEqual(resumed.chat_calls, [])  # nothing re-billed
        self.assertEqual(OutreachAction.objects.filter(trace_run_id=run_id).count(), 3)
        self.assertEqual(
            app_models.AgentLeadRun.objects.get(trace_run_id=run_id, lead_id="lead_000").status,
            app_models.AgentLeadRun.STATUS_FAILED,  # left exactly as found
        )

    def test_agent_outcome_cannot_override_the_rules(self):
        self.assertIn(
            "resume_run_id", inspect.signature(outreach.plan_outreach).parameters
        )  # red at skeleton
        fields = {f.name for f in dataclasses.fields(agent_loop.AgentOutcome)}
        self.assertEqual(fields, {"draft_text", "error", "steps_used", "tool_calls_used"})
        src = inspect.getsource(agent_loop) + inspect.getsource(state)
        self.assertNotIn("dispatch", src)  # services/agent/ never imports the send gate


class RedTeamFencingTests(TestCase):
    def test_injected_note_stays_redacted_and_fenced_in_the_folded_conversation(self):
        # First statement calls into the NotImplementedError stub at skeleton.
        payload = redteam_payloads.payloads_for(redteam_payloads.DIRECT_OVERRIDE)[0]
        lead = Lead.objects.create(
            id="lead_rt1",
            agency_name="RT Agency",
            contact_name="C",
            contact_email="rt@example.com",
            contact_phone="1",
            state="TX",
            num_producers=3,
            years_in_business=4,
            estimated_book_size_usd=1,
            stage="active_trial",
        )
        Event.objects.create(
            lead=lead,
            type="call_logged",
            timestamp=datetime(2026, 6, 1, tzinfo=dt_timezone.utc),
            meta={"notes": payload.injected_text},
        )
        ctx = tools.build_tool_context(lead, (), (), (), None)
        out = tools.execute_tool("get_lead_history", {}, ctx)
        self.assertNotIn(payload.injected_text, out)  # redacted at snapshot time

        steps = [
            state.StepRecord(
                seq=1,
                kind=state.KIND_LLM_CALL,
                payload={
                    "text": "",
                    "tool_calls": [{"id": "c1", "name": "get_lead_history", "arguments": {}}],
                },
            ),
            state.StepRecord(
                seq=2,
                kind=state.KIND_TOOL_RESULT,
                payload={"tool_call_id": "c1", "name": "get_lead_history", "result": out},
            ),
        ]
        folded = state.fold_messages("TRUSTED INSTRUCTIONS", steps)
        # Instruction region: no untrusted content, no fencing, no payload.
        self.assertNotIn(payload.injected_text, folded[0].content)
        self.assertNotIn(sanitize.UNTRUSTED_OPEN, folded[0].content)
        # Tool result: fenced exactly once, at fold time.
        fenced = folded[-1]
        self.assertEqual(fenced.role, "tool_result")
        self.assertIn(sanitize.UNTRUSTED_OPEN, fenced.content)
        self.assertIn(sanitize.UNTRUSTED_CLOSE, fenced.content)
        self.assertNotIn(payload.injected_text, fenced.content)


class RuntimeDefaultTests(SimpleTestCase):
    def test_agent_is_disabled_by_default_so_merged_code_is_inert(self):
        """The bench (evals/bench_planner.py) and every existing mock seam keep
        driving the single-shot path until an operator opts in."""
        runtime = get_planner_runtime()
        self.assertTrue(hasattr(runtime, "agent_enabled"))  # red at skeleton
        self.assertFalse(runtime.agent_enabled)
        self.assertGreaterEqual(runtime.agent_per_lead_s, runtime.timeouts.request_s)


class ResumeEndpointTests(AuthenticatedAPITestCase):
    def test_unknown_resume_run_id_is_a_400(self):
        self.assertTrue(hasattr(app_models, "AgentLeadRun"))  # red at skeleton
        resp = self.client.post(
            "/api/outreach/run/",
            data={"resume_run_id": "no-such-run"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "unknown_run")

    def test_resume_with_the_agent_flag_off_is_refused_rather_than_replanned(self):
        """MB3: the view gates resume purely on `AgentLeadRun` rows existing,
        never on the flag, and those rows survive a crash. Every piece of resume
        machinery — run lookup, prior steps, claims, the already-finalized
        filter — sits inside `if runtime.agent_enabled:`, while the telemetry
        layer reuses the supplied run id and stamps it on every new row.

        So a crash with the flag on followed by an operator flipping it off and
        restarting (a natural incident response) re-bills every lead
        single-shot, including ones a flag-on resume would have replayed from
        the log at zero cost, and then serves the crashed agent run's step log
        as the provenance of a draft that was written single-shot.
        """
        self.assertTrue(hasattr(app_models, "AgentLeadRun"))  # red at skeleton
        _seed_leads(2)
        with (
            override_settings(OUTREACH_AGENT_ENABLED=True),
            mock.patch.object(outreach, "get_llm_client", return_value=_ScriptedChatClient({})),
        ):
            with self.assertRaises(Exception):
                outreach.plan_outreach()  # dies before phase 5: rows exist, no actions
        run_id = app_models.AgentLeadRun.objects.values_list("trace_run_id", flat=True).first()
        self.assertIsNotNone(run_id)
        self.assertEqual(OutreachAction.objects.filter(trace_run_id=run_id).count(), 0)

        client = _SingleShotClient()
        with (
            override_settings(OUTREACH_AGENT_ENABLED=False),
            mock.patch.object(outreach, "get_llm_client", return_value=client),
        ):
            resp = self.client.post(
                "/api/outreach/run/",
                data={"resume_run_id": run_id},
                content_type="application/json",
            )

        self.assertEqual(client.copy_calls, 0)  # nothing re-billed single-shot
        # Nor mislabelled: a row stamped with the crashed agent run's id makes
        # the trace endpoint serve that run's step log as this draft's
        # provenance, for a draft the agent never wrote.
        self.assertEqual(OutreachAction.objects.filter(trace_run_id=run_id).count(), 0)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "agent_disabled")


@override_settings(OUTREACH_AGENT_ENABLED=True)
class ResumeValidationTests(TestCase):
    """FU-A: the guards belong at the mechanism, not only at the one view that
    happens to reach it. `plan_outreach` is importable, scriptable and called
    directly by tests and scripts; a guard only the view enforces is a guard
    the next caller does not get."""

    @classmethod
    def setUpTestData(cls):
        _seed_leads(1)

    def test_plan_outreach_refuses_an_unknown_resume_run_id(self):
        client = _ScriptedChatClient({})
        with mock.patch.object(outreach, "get_llm_client", return_value=client):
            with self.assertRaises(outreach.UnknownRun):
                outreach.plan_outreach(resume_run_id="no-such-run")
        # A typo'd id must not mint a fully-billed run under itself.
        self.assertEqual(client.chat_calls, [])
        self.assertEqual(app_models.AgentLeadRun.objects.count(), 0)
        self.assertEqual(OutreachAction.objects.filter(trace_run_id="no-such-run").count(), 0)

    def test_plan_outreach_refuses_a_resume_when_the_agent_is_disabled(self):
        self._crashed_run()
        run_id = app_models.AgentLeadRun.objects.values_list("trace_run_id", flat=True).first()
        single_shot = _SingleShotClient()
        with (
            override_settings(OUTREACH_AGENT_ENABLED=False),
            mock.patch.object(outreach, "get_llm_client", return_value=single_shot),
        ):
            with self.assertRaises(outreach.AgentDisabled):
                outreach.plan_outreach(resume_run_id=run_id)
        self.assertEqual(single_shot.copy_calls, 0)
        self.assertEqual(OutreachAction.objects.filter(trace_run_id=run_id).count(), 0)

    def _crashed_run(self):
        client = _ScriptedChatClient({})
        with mock.patch.object(outreach, "get_llm_client", return_value=client):
            with self.assertRaises(Exception):
                outreach.plan_outreach()
        return client
