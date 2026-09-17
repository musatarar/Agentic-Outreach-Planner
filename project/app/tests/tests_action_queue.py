"""The actions engine end to end: the queue, the cron's claim, and the passes
that take one job from queued to a chosen action."""

import datetime
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db.models import Q
from django.test import TestCase, override_settings
from django.utils import timezone

from project.app.actions import evaluate, inference, services
from project.app.actions.models import ActionJob
from project.app.models import ActionType, Event, Lead, OutreachRule
from project.app.rules import utils
from project.app.rules.utils import _all_of, _cond

TODAY = datetime.date(2026, 6, 12)


def _section(*holding):
    """An inference section as ``rules.inference.infer`` returns one, with
    every named rule holding."""
    return {
        "rules_evaluated": len(holding),
        "matched_rule_ids": [rule.pk for rule in holding],
        "matched_rules": [rule.name for rule in holding],
        "verdicts": [
            {"rule_id": rule.pk, "holds": True, "evidence_quote": None} for rule in holding
        ],
        "unevaluable_rule_ids": [],
    }


class EngineTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.owner = get_user_model().objects.create_user(username="planner@lockedin.example")

    def _lead(self, lead_id="lead_001", owner=None, **kwargs):
        fields = dict(
            agency_name="Summit Risk Advisors",
            contact_name="Priya Nair",
            contact_email="priya@summitrisk.example.com",
            contact_phone="555-0100",
            state="CO",
            num_producers=4,
            years_in_business=9,
            estimated_book_size_usd=1_400_000,
            stage="active_trial",
            signed_up_date=TODAY - datetime.timedelta(days=50),
            last_login_date=TODAY - datetime.timedelta(days=2),
            last_contacted_date=TODAY - datetime.timedelta(days=5),
            quotes_created=10,
            quotes_submitted=6,
            deals_closed=3,
            hubspot_notes="",
        )
        fields.update(kwargs)
        return Lead.objects.create(id=lead_id, owner=owner or self.owner, **fields)

    def _event(self, lead, type_="login", **meta):
        return Event.objects.create(lead=lead, type=type_, timestamp=timezone.now(), meta=meta)

    def _action(self, key, owner=None):
        return ActionType.objects.create(owner=owner or self.owner, key=key, label=key)

    def _rule(self, action, name, weight=OutreachRule.WEIGHT_MEDIUM, **kwargs):
        kwargs.setdefault("owner", action.owner)
        kwargs.setdefault("kind", OutreachRule.KIND_DETERMINISTIC)
        kwargs.setdefault("conditions", _all_of(_cond("deals_closed", ">", 2)))
        return OutreachRule.objects.create(action=action, name=name, weight=weight, **kwargs)

    def _run(self, job):
        self.assertTrue(services.claim(job))
        return services.run_job(job, today=TODAY)


class EnqueueTests(EngineTestCase):
    def test_a_queued_job_holds_the_lead_and_the_events_it_was_queued_with(self):
        lead = self._lead()
        self._event(lead)
        self._event(lead, "quote_created")

        job = services.enqueue_lead(lead)

        self.assertEqual(job.status, ActionJob.STATUS_QUEUED)
        self.assertEqual(job.lead, lead)
        self.assertEqual(job.events.count(), 2)

    def test_a_job_keeps_the_events_it_was_queued_with_when_new_ones_arrive(self):
        lead = self._lead()
        self._event(lead)
        job = services.enqueue_lead(lead)

        self._event(lead, "deal_closed")

        self.assertEqual(job.events.count(), 1)
        self.assertEqual(lead.events.count(), 2)

    def test_a_lead_cannot_hold_two_open_jobs(self):
        lead = self._lead()
        first = services.enqueue_lead(lead)
        second = services.enqueue_lead(lead)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ActionJob.objects.count(), 1)

    def test_a_lead_can_be_queued_again_once_its_job_is_finished(self):
        lead = self._lead()
        first = services.enqueue_lead(lead)
        self._run(first)

        second = services.enqueue_lead(lead)

        self.assertNotEqual(first.pk, second.pk)

    def test_enqueue_pending_leads_skips_the_leads_already_in_flight(self):
        first = self._lead("lead_001")
        second = self._lead("lead_002")
        services.enqueue_lead(first)

        queued = services.enqueue_pending_leads()

        self.assertEqual([job.lead_id for job in queued], [second.id])

    def test_enqueue_pending_leads_can_be_narrowed_to_named_leads(self):
        self._lead("lead_001")
        self._lead("lead_002")

        queued = services.enqueue_pending_leads(lead_ids=["lead_002"])

        self.assertEqual([job.lead_id for job in queued], ["lead_002"])


class SettledLeadTests(EngineTestCase):
    """A lead a run decided today, with nothing new since, is not asked again."""

    def _decided_today(self, lead, status=ActionJob.STATUS_NO_ACTION, **kwargs):
        job = services.enqueue_lead(lead)
        ActionJob.objects.filter(pk=job.pk).update(
            status=status, finished_at=timezone.now(), **kwargs
        )
        return ActionJob.objects.get(pk=job.pk)

    def test_a_lead_decided_today_with_no_new_events_is_not_queued_again(self):
        lead = self._lead()
        self._event(lead)
        self._decided_today(lead)

        self.assertEqual(services.enqueue_pending_leads(), [])

    def test_an_event_newer_than_the_run_queues_the_lead_again(self):
        lead = self._lead()
        self._decided_today(lead)
        self._event(lead, "deal_closed")

        queued = services.enqueue_pending_leads()

        self.assertEqual([job.lead_id for job in queued], [lead.id])

    def test_an_event_that_arrived_mid_run_still_queues_the_lead_again(self):
        lead = self._lead()
        job = services.enqueue_lead(lead)
        # Queued, then the event lands, then the run ends: it judged neither.
        event = self._event(lead, "login")
        ActionJob.objects.filter(pk=job.pk).update(
            status=ActionJob.STATUS_NO_ACTION,
            finished_at=event.timestamp + datetime.timedelta(minutes=1),
        )

        queued = services.enqueue_pending_leads()

        self.assertEqual([job.lead_id for job in queued], [lead.id])

    def test_a_run_that_failed_today_does_not_settle_the_lead(self):
        lead = self._lead()
        self._decided_today(lead, status=ActionJob.STATUS_FAILED)

        queued = services.enqueue_pending_leads()

        self.assertEqual([job.lead_id for job in queued], [lead.id])

    def test_a_run_decided_yesterday_does_not_settle_the_lead(self):
        lead = self._lead()
        job = self._decided_today(lead)
        ActionJob.objects.filter(pk=job.pk).update(
            finished_at=timezone.now() - datetime.timedelta(days=1)
        )

        queued = services.enqueue_pending_leads()

        self.assertEqual([job.lead_id for job in queued], [lead.id])

    def test_a_lead_no_run_has_touched_is_still_queued(self):
        settled = self._lead("lead_001")
        self._decided_today(settled)
        untouched = self._lead("lead_002")

        queued = services.enqueue_pending_leads()

        self.assertEqual([job.lead_id for job in queued], [untouched.id])

    def test_the_second_tick_of_a_day_queues_nothing_new(self):
        self._lead("lead_001")
        self._lead("lead_002")
        call_command("run_action_jobs")

        out = StringIO()
        call_command("run_action_jobs", stdout=out)

        self.assertIn("queued 0 lead(s)", out.getvalue())
        self.assertEqual(ActionJob.objects.count(), 2)


class ClaimTests(EngineTestCase):
    def test_claiming_moves_the_job_to_processing_and_counts_the_attempt(self):
        job = services.enqueue_lead(self._lead())

        self.assertTrue(services.claim(job))

        job.refresh_from_db()
        self.assertEqual(job.status, ActionJob.STATUS_PROCESSING)
        self.assertEqual(job.attempts, 1)
        self.assertIsNotNone(job.started_at)

    def test_a_second_claim_of_the_same_job_is_refused(self):
        job = services.enqueue_lead(self._lead())
        self.assertTrue(services.claim(job))

        self.assertFalse(services.claim(ActionJob.objects.get(pk=job.pk)))

    def test_a_job_another_worker_already_finished_is_left_alone(self):
        job = services.enqueue_lead(self._lead())
        self.assertTrue(services.claim(job))
        ActionJob.objects.filter(pk=job.pk).update(status=ActionJob.STATUS_NO_ACTION)

        services.run_job(job, today=TODAY)

        job.refresh_from_db()
        self.assertEqual(job.status, ActionJob.STATUS_NO_ACTION)
        self.assertEqual(job.decision, {})

    def test_a_transition_the_state_machine_does_not_allow_is_refused(self):
        job = services.enqueue_lead(self._lead())

        with self.assertRaises(ValueError):
            services._transition(
                job, ActionJob.STATUS_QUEUED, ActionJob.STATUS_INFERRED_ACTION_CHOSEN
            )


class DeterministicPassTests(EngineTestCase):
    def test_a_matching_rule_chooses_its_action_without_reaching_inference(self):
        action = self._action("power_user_reward")
        self._rule(action, "Modest deal momentum", OutreachRule.WEIGHT_HIGH)
        job = services.enqueue_lead(self._lead())

        with mock.patch.object(inference, "infer") as infer:
            self._run(job)

        infer.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, ActionJob.STATUS_DETERMINISTIC_ACTION_CHOSEN)
        self.assertEqual(job.selected_action, action)
        self.assertEqual(job.decision["selected"]["action_key"], "power_user_reward")
        self.assertEqual(job.decision["selected"]["reasons"], ["Modest deal momentum"])
        self.assertIsNotNone(job.finished_at)

    def test_the_heaviest_tally_wins_when_two_actions_are_argued_for(self):
        weak = self._action("nudge_usage")
        strong = self._action("reengage_dormant")
        self._rule(weak, "modest momentum", OutreachRule.WEIGHT_MEDIUM)
        self._rule(strong, "dormant", OutreachRule.WEIGHT_HIGH)
        job = services.enqueue_lead(self._lead())

        self._run(job)

        job.refresh_from_db()
        self.assertEqual(job.selected_action, strong)
        self.assertEqual(job.decision["selected"]["weight"], 3)

    def test_two_weak_rules_agreeing_clear_the_floor_one_alone_does_not(self):
        action = self._action("nudge_usage")
        self._rule(action, "modest momentum", OutreachRule.WEIGHT_LOW)
        alone = services.enqueue_lead(self._lead("lead_001"))
        self._run(alone)
        alone.refresh_from_db()
        self.assertEqual(alone.status, ActionJob.STATUS_NO_ACTION)

        self._rule(action, "logs in but never submits", OutreachRule.WEIGHT_LOW)
        together = services.enqueue_lead(self._lead("lead_002"))
        self._run(together)

        together.refresh_from_db()
        self.assertEqual(together.status, ActionJob.STATUS_DETERMINISTIC_ACTION_CHOSEN)
        self.assertEqual(together.decision["selected"]["weight"], 2)

    def test_a_disabled_rule_or_a_disabled_action_never_fires(self):
        self._rule(self._action("nudge_usage"), "off", OutreachRule.WEIGHT_HIGH, enabled=False)
        self._rule(
            self._action("reengage_dormant", owner=self.owner),
            "action off",
            OutreachRule.WEIGHT_HIGH,
        )
        ActionType.objects.filter(key="reengage_dormant").update(enabled=False)
        job = services.enqueue_lead(self._lead())

        self._run(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ActionJob.STATUS_NO_ACTION)

    def test_the_job_is_judged_on_its_own_events_not_the_leads_later_ones(self):
        lead = self._lead(last_contacted_date=TODAY - datetime.timedelta(days=15))
        action = self._action("follow_up_after_hold")
        self._rule(
            action,
            "went quiet",
            OutreachRule.WEIGHT_HIGH,
            conditions=_all_of(_cond("gone_quiet", "==", True, source="derived")),
        )
        job = services.enqueue_lead(lead)
        # The corroborating event lands after the job was queued.
        self._event(lead, "email_sent", outcome="no_reply")

        self._run(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ActionJob.STATUS_NO_ACTION)

    def test_a_rule_the_engine_cannot_evaluate_is_recorded_instead_of_firing(self):
        rule = self._rule(self._action("nudge_usage"), "stale vocabulary", OutreachRule.WEIGHT_HIGH)
        # Written before the field it names left the vocabulary.
        OutreachRule.objects.filter(pk=rule.pk).update(
            conditions={
                "version": utils.SCHEMA_VERSION,
                "operator": "all_of",
                "conditions": [
                    {
                        "field": "favourite_colour",
                        "operator": "==",
                        "source": "lead",
                        "threshold": "red",
                    }
                ],
            }
        )
        job = services.enqueue_lead(self._lead())

        self._run(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ActionJob.STATUS_NO_ACTION)
        self.assertEqual(job.decision["unevaluable_rule_ids"], [rule.pk])


class OwnerScopingTests(EngineTestCase):
    def test_another_users_rules_never_fire_on_this_leads_job(self):
        other = get_user_model().objects.create_user(username="other@elsewhere.example")
        self._rule(self._action("nudge_usage", owner=other), "theirs", OutreachRule.WEIGHT_HIGH)
        job = services.enqueue_lead(self._lead())

        self._run(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ActionJob.STATUS_NO_ACTION)
        self.assertEqual(job.decision["rules_evaluated"], 0)

    def test_an_unowned_lead_has_no_rules(self):
        self._rule(self._action("nudge_usage"), "ours", OutreachRule.WEIGHT_HIGH)
        lead = self._lead()
        Lead.objects.filter(pk=lead.pk).update(owner=None)
        job = services.enqueue_lead(Lead.objects.get(pk=lead.pk))

        self._run(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ActionJob.STATUS_NO_ACTION)
        self.assertEqual(job.decision["rules_evaluated"], 0)
        self.assertIsNone(job.decision["owner_id"])

    def test_the_job_runs_the_catalog_of_the_user_whose_book_the_lead_is_in(self):
        colleague = get_user_model().objects.create_user(username="colleague@lockedin.example")
        self._rule(self._action("nudge_usage"), "mine", OutreachRule.WEIGHT_HIGH)
        self._rule(
            self._action("reengage_dormant", owner=colleague), "theirs", OutreachRule.WEIGHT_HIGH
        )
        job = services.enqueue_lead(self._lead(owner=colleague))

        self._run(job)

        job.refresh_from_db()
        self.assertEqual(job.decision["owner_id"], colleague.pk)
        self.assertEqual(job.decision["selected"]["action_key"], "reengage_dormant")


@override_settings(ACTIONS_LLM_DRY_RUN=False)
class InferencePassTests(EngineTestCase):
    def _inference_rule(self, action, name, weight=OutreachRule.WEIGHT_HIGH, **kwargs):
        return self._rule(
            action,
            name,
            weight,
            kind=OutreachRule.KIND_INFERENCE,
            conditions=kwargs.pop("conditions", {}),
            inference_prompt=kwargs.pop("inference_prompt", "the notes say they need help"),
            **kwargs,
        )

    def test_a_lead_no_deterministic_rule_resolves_reaches_the_inference_pass(self):
        action = self._action("set_up_appointment")
        rule = self._inference_rule(action, "they need help")
        job = services.enqueue_lead(self._lead())

        with mock.patch.object(inference, "infer", wraps=inference.infer) as infer:
            self._run(job)

        candidates, _lead, today = infer.call_args.args
        self.assertEqual([candidate.pk for candidate in candidates], [rule.pk])
        self.assertEqual(today, TODAY)
        job.refresh_from_db()
        self.assertIn("inference", job.decision)

    def test_the_stub_chooses_no_action_and_says_what_is_missing(self):
        self._inference_rule(self._action("set_up_appointment"), "they need help")
        job = services.enqueue_lead(self._lead())

        self._run(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ActionJob.STATUS_NO_ACTION)
        self.assertIsNone(job.selected_action)
        self.assertNotIn("selected", job.decision)
        self.assertIn("TODO", job.decision["inference"]["reason"])

    def test_a_candidate_the_stub_never_asked_about_is_unevaluable_not_a_refusal(self):
        rule = self._inference_rule(self._action("set_up_appointment"), "they need help")
        job = services.enqueue_lead(self._lead())

        self._run(job)

        job.refresh_from_db()
        self.assertEqual(job.decision["unevaluable_rule_ids"], [rule.pk])
        self.assertEqual(job.decision["inference"]["matched_rule_ids"], [])
        self.assertEqual(job.decision["inference"]["verdicts"], [])

    def test_both_passes_unevaluable_rules_land_in_one_list(self):
        deterministic = self._rule(
            self._action("nudge_usage"), "stale vocabulary", OutreachRule.WEIGHT_HIGH
        )
        OutreachRule.objects.filter(pk=deterministic.pk).update(
            conditions={
                "version": utils.SCHEMA_VERSION,
                "operator": "all_of",
                "conditions": [
                    {
                        "field": "favourite_colour",
                        "operator": "==",
                        "source": "lead",
                        "threshold": "red",
                    }
                ],
            }
        )
        inferred = self._inference_rule(self._action("set_up_appointment"), "they need help")
        job = services.enqueue_lead(self._lead())

        self._run(job)

        job.refresh_from_db()
        self.assertEqual(
            job.decision["unevaluable_rule_ids"], sorted([deterministic.pk, inferred.pk])
        )
        self.assertNotIn("unevaluable_rule_ids", job.decision["inference"])
        self.assertNotIn("rules_evaluated", job.decision["inference"])

    def test_an_inference_rule_gated_by_conditions_is_not_asked_until_they_hold(self):
        action = self._action("set_up_appointment")
        self._inference_rule(
            action,
            "gated",
            conditions=_all_of(_cond("deals_closed", ">", 100)),
        )
        job = services.enqueue_lead(self._lead())

        with mock.patch.object(inference, "infer", wraps=inference.infer) as infer:
            self._run(job)

        candidates, _lead, _today = infer.call_args.args
        self.assertEqual(list(candidates), [])
        job.refresh_from_db()
        self.assertEqual(job.decision["unevaluable_rule_ids"], [])

    def test_a_match_from_the_pass_chooses_an_action_through_the_same_tally(self):
        action = self._action("set_up_appointment")
        rule = self._inference_rule(action, "they need help")
        job = services.enqueue_lead(self._lead())

        with mock.patch.object(inference, "infer", return_value=_section(rule)):
            self._run(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ActionJob.STATUS_INFERRED_ACTION_CHOSEN)
        self.assertEqual(job.selected_action, action)
        self.assertEqual(job.decision["selected"]["action_key"], "set_up_appointment")
        self.assertEqual(job.decision["inference"]["matched_rule_ids"], [rule.pk])

    def test_a_verdict_naming_a_rule_that_was_never_a_candidate_is_not_a_match(self):
        action = self._action("set_up_appointment")
        candidate = self._inference_rule(action, "they need help")
        other = get_user_model().objects.create_user(username="other@elsewhere.example")
        stranger = self._inference_rule(
            self._action("nudge_usage", owner=other), "someone else's rule", owner=other
        )
        job = services.enqueue_lead(self._lead())
        section = _section(candidate)
        section["matched_rule_ids"] = [stranger.pk]
        section["matched_rules"] = [stranger.name]

        with mock.patch.object(inference, "infer", return_value=section):
            self._run(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ActionJob.STATUS_NO_ACTION)
        self.assertIsNone(job.selected_action)

    def test_both_passes_tally_together_so_two_weak_agreeing_rules_decide(self):
        action = self._action("nudge_usage")
        self._rule(action, "modest momentum", OutreachRule.WEIGHT_LOW)
        inferred = self._inference_rule(action, "they need help", OutreachRule.WEIGHT_LOW)
        job = services.enqueue_lead(self._lead())

        with mock.patch.object(inference, "infer", return_value=_section(inferred)):
            self._run(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ActionJob.STATUS_INFERRED_ACTION_CHOSEN)
        self.assertEqual(job.decision["selected"]["weight"], 2)


class DryRunTests(EngineTestCase):
    """ACTIONS_LLM_DRY_RUN decides whether a run may reach the provider at all."""

    def _inference_rule(self, name="they need help"):
        return self._rule(
            self._action("set_up_appointment"),
            name,
            OutreachRule.WEIGHT_HIGH,
            kind=OutreachRule.KIND_INFERENCE,
            conditions={},
            inference_prompt="the notes say they need help",
        )

    @override_settings(ACTIONS_LLM_DRY_RUN=True)
    def test_a_dry_run_never_calls_the_inference_pass(self):
        self._inference_rule()
        job = services.enqueue_lead(self._lead())

        with mock.patch.object(inference, "infer") as infer:
            self._run(job)

        infer.assert_not_called()

    @override_settings(ACTIONS_LLM_DRY_RUN=True)
    def test_a_dry_run_leaves_every_candidate_unevaluable_and_says_why(self):
        rule = self._inference_rule()
        job = services.enqueue_lead(self._lead())

        self._run(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ActionJob.STATUS_NO_ACTION)
        self.assertEqual(job.decision["unevaluable_rule_ids"], [rule.pk])
        self.assertIn("ACTIONS_LLM_DRY_RUN", job.decision["inference"]["reason"])

    @override_settings(ACTIONS_LLM_DRY_RUN=True)
    def test_a_dry_run_still_resolves_a_lead_the_deterministic_pass_settles(self):
        self._rule(self._action("nudge_usage"), "modest momentum", OutreachRule.WEIGHT_HIGH)
        job = services.enqueue_lead(self._lead())

        self._run(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ActionJob.STATUS_DETERMINISTIC_ACTION_CHOSEN)

    @override_settings(ACTIONS_LLM_DRY_RUN=False)
    def test_the_inference_pass_runs_when_the_dry_run_flag_is_off(self):
        self._inference_rule()
        job = services.enqueue_lead(self._lead())

        with mock.patch.object(inference, "infer", wraps=inference.infer) as infer:
            self._run(job)

        infer.assert_called_once()

    @override_settings(ACTIONS_LLM_DRY_RUN=True)
    def test_the_command_says_a_tick_is_dry_before_it_runs(self):
        self._lead()
        out = StringIO()

        call_command("run_action_jobs", stdout=out)

        self.assertIn("ACTIONS_LLM_DRY_RUN", out.getvalue())


class FailureTests(EngineTestCase):
    def test_a_job_that_raises_is_recorded_as_failed_rather_than_sinking_the_batch(self):
        first = services.enqueue_lead(self._lead("lead_001"))
        second = services.enqueue_lead(self._lead("lead_002"))

        with mock.patch.object(services, "_resolve", side_effect=[RuntimeError("boom"), second]):
            with self.assertLogs("project.app.actions.services", level="ERROR"):
                jobs = services.run_queue(today=TODAY)

        self.assertEqual(len(jobs), 2)
        first.refresh_from_db()
        self.assertEqual(first.status, ActionJob.STATUS_FAILED)
        self.assertEqual(first.error, "boom")

    def test_a_failed_job_is_queued_again_and_counts_a_second_attempt(self):
        lead = self._lead()
        job = services.enqueue_lead(lead)
        with mock.patch.object(services, "_resolve", side_effect=RuntimeError("boom")):
            with self.assertLogs("project.app.actions.services", level="ERROR"):
                services.run_queue(today=TODAY)

        requeued = services.enqueue_lead(lead)
        self.assertNotEqual(requeued.pk, job.pk)


class CronCommandTests(EngineTestCase):
    def test_the_command_queues_every_lead_then_runs_the_batch(self):
        self._rule(self._action("nudge_usage"), "modest momentum", OutreachRule.WEIGHT_HIGH)
        self._lead("lead_001")
        self._lead("lead_002")

        call_command("run_action_jobs")

        self.assertEqual(ActionJob.objects.count(), 2)
        self.assertEqual(
            ActionJob.objects.filter(status=ActionJob.STATUS_DETERMINISTIC_ACTION_CHOSEN).count(), 2
        )

    def test_the_limit_bounds_one_tick_and_leaves_the_rest_queued(self):
        self._lead("lead_001")
        self._lead("lead_002")

        call_command("run_action_jobs", limit=1)

        self.assertEqual(ActionJob.objects.filter(status=ActionJob.STATUS_QUEUED).count(), 1)

    def test_no_enqueue_drains_the_queue_without_filling_it(self):
        self._lead("lead_001")

        call_command("run_action_jobs", no_enqueue=True)

        self.assertEqual(ActionJob.objects.count(), 0)

    def test_no_job_is_left_in_a_working_state_after_a_tick(self):
        self._lead("lead_001")
        call_command("run_action_jobs")

        self.assertFalse(ActionJob.objects.filter(status__in=ActionJob.OPEN_STATUSES).exists())


class SeededCatalogTests(EngineTestCase):
    """The seeded catalog and this engine share one vocabulary, or the demo
    rules would validate and then never fire."""

    def test_every_seeded_deterministic_payload_evaluates(self):
        from project.app.management.commands import seed_rules_catalog

        lead = self._lead()
        for spec in seed_rules_catalog.RULES:
            conditions = spec.get("conditions")
            if not conditions:
                continue
            with self.subTest(spec["name"]):
                self.assertIsInstance(evaluate.matches(conditions, lead, TODAY), bool)

    def test_the_seeded_catalog_is_what_its_owners_leads_are_run_against(self):
        call_command("seed_rules_catalog", owner="demo@lockedin.example")
        demo = get_user_model().objects.get(username="demo@lockedin.example")

        self.assertTrue(services.rules_for_lead(self._lead(owner=demo)).exists())
        self.assertFalse(services.rules_for_lead(self._lead("lead_002")).exists())

    def test_the_seeded_catalog_chooses_the_planners_action_for_a_dormant_lead(self):
        call_command("seed_rules_catalog", owner=self.owner.username)
        lead = self._lead(last_login_date=TODAY - datetime.timedelta(days=60))

        job = self._run(services.enqueue_lead(lead))

        job.refresh_from_db()
        self.assertEqual(job.status, ActionJob.STATUS_DETERMINISTIC_ACTION_CHOSEN)
        self.assertEqual(job.selected_action.key, "reengage_dormant")


class VocabularyCoverageTests(EngineTestCase):
    """What the catalog can store against what this engine can evaluate. The
    vocabulary is read off the Lead and Event columns, so it can widen without
    anyone touching the engine."""

    def test_every_computed_field_in_the_vocabulary_has_a_resolver(self):
        fields = utils.fields_by_source()
        for source in (utils.SOURCE_DERIVED, utils.SOURCE_NOTES):
            for field in fields[source]:
                with self.subTest(source=source, field=field):
                    self.assertIn(field, evaluate.RESOLVERS[source])

    def test_every_lead_column_in_the_vocabulary_is_read_straight_off_the_row(self):
        lead = self._lead()
        for field in utils.fields_by_source()[utils.SOURCE_LEAD]:
            with self.subTest(field=field):
                self.assertTrue(hasattr(lead, field))

    def test_an_event_column_stores_in_a_rule_but_has_no_verdict_yet(self):
        unresolved = set(utils.fields_by_source()[utils.SOURCE_EVENTS]) - set(
            evaluate.RESOLVERS[utils.SOURCE_EVENTS]
        )
        self.assertEqual(unresolved, {"type", "timestamp"})

        payload = _all_of(
            _cond("deals_closed", ">", 0),
            _cond("type", "==", "login", source=utils.SOURCE_EVENTS),
        )
        utils.validate_conditions(payload)
        with self.assertRaises(evaluate.ConditionError):
            evaluate.matches(payload, self._lead(), TODAY)


class ConstraintTests(EngineTestCase):
    def test_the_open_job_constraint_names_every_working_status(self):
        constraint = next(
            c for c in ActionJob._meta.constraints if c.name == "ajob_one_open_per_lead"
        )
        self.assertEqual(constraint.condition, Q(status__in=ActionJob.OPEN_STATUSES))

    def test_the_status_constraint_lists_exactly_the_declared_statuses(self):
        constraint = next(c for c in ActionJob._meta.constraints if c.name == "ajob_status_known")
        self.assertEqual(
            set(constraint.check.children[0][1]), {value for value, _ in ActionJob.STATUS_CHOICES}
        )
