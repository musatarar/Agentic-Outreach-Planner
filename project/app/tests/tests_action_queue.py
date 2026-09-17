"""The actions engine end to end: the queue, the cron's claim, and the passes
that take one job from queued to a chosen action."""

import datetime
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db.models import Q
from django.test import TestCase
from django.utils import timezone

from project.app.actions import evaluate, inference, services
from project.app.actions.models import ActionJob, TenantCatalog
from project.app.models import ActionType, Event, Lead, OutreachRule
from project.app.rules import utils
from project.app.rules.utils import _all_of, _cond

TODAY = datetime.date(2026, 6, 12)
TENANT = "acme"


class EngineTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.owner = get_user_model().objects.create_user(username="planner@lockedin.example")
        TenantCatalog.objects.create(tenant=TENANT, owner=self.owner)

    def _lead(self, lead_id="lead_001", tenant=TENANT, **kwargs):
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
        return Lead.objects.create(id=lead_id, tenant=tenant, **fields)

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
    def test_a_queued_job_snapshots_the_leads_tenant_and_its_events(self):
        lead = self._lead()
        self._event(lead)
        self._event(lead, "quote_created")

        job = services.enqueue_lead(lead)

        self.assertEqual(job.status, ActionJob.STATUS_QUEUED)
        self.assertEqual(job.tenant, TENANT)
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


class TenantScopingTests(EngineTestCase):
    def test_a_rule_belonging_to_another_tenants_catalog_never_fires(self):
        other = get_user_model().objects.create_user(username="other@elsewhere.example")
        TenantCatalog.objects.create(tenant="globex", owner=other)
        self._rule(self._action("nudge_usage", owner=other), "theirs", OutreachRule.WEIGHT_HIGH)
        job = services.enqueue_lead(self._lead())

        self._run(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ActionJob.STATUS_NO_ACTION)
        self.assertEqual(job.decision["rules_evaluated"], 0)

    def test_a_tenant_with_no_catalog_row_has_no_rules(self):
        self._rule(self._action("nudge_usage"), "ours", OutreachRule.WEIGHT_HIGH)
        job = services.enqueue_lead(self._lead(tenant="unmapped"))

        self._run(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ActionJob.STATUS_NO_ACTION)
        self.assertEqual(job.decision["rules_evaluated"], 0)

    def test_two_owners_mapped_to_one_tenant_are_evaluated_together(self):
        colleague = get_user_model().objects.create_user(username="colleague@lockedin.example")
        TenantCatalog.objects.create(tenant=TENANT, owner=colleague)
        self._rule(self._action("nudge_usage"), "mine", OutreachRule.WEIGHT_LOW)
        self._rule(self._action("nudge_usage", owner=colleague), "theirs", OutreachRule.WEIGHT_LOW)
        job = services.enqueue_lead(self._lead())

        self._run(job)

        job.refresh_from_db()
        self.assertEqual(job.decision["rules_evaluated"], 2)


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

        self._run(job)

        job.refresh_from_db()
        self.assertEqual(job.decision["inference"]["candidate_rule_ids"], [rule.pk])

    def test_the_stub_chooses_no_action_and_says_what_is_missing(self):
        self._inference_rule(self._action("set_up_appointment"), "they need help")
        job = services.enqueue_lead(self._lead())

        self._run(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ActionJob.STATUS_NO_ACTION)
        self.assertIsNone(job.selected_action)
        self.assertNotIn("selected", job.decision)
        self.assertIn("TODO", job.decision["inference"]["todo"])

    def test_an_inference_rule_gated_by_conditions_is_not_asked_until_they_hold(self):
        action = self._action("set_up_appointment")
        self._inference_rule(
            action,
            "gated",
            conditions=_all_of(_cond("deals_closed", ">", 100)),
        )
        job = services.enqueue_lead(self._lead())

        self._run(job)

        job.refresh_from_db()
        self.assertEqual(job.decision["inference"]["candidate_rule_ids"], [])

    def test_a_match_from_the_pass_chooses_an_action_through_the_same_tally(self):
        action = self._action("set_up_appointment")
        rule = self._inference_rule(action, "they need help")
        job = services.enqueue_lead(self._lead())
        result = inference.InferenceResult(matched=(rule,), candidates=(rule,), todo="")

        with mock.patch.object(inference, "infer", return_value=result):
            self._run(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ActionJob.STATUS_INFERRED_ACTION_CHOSEN)
        self.assertEqual(job.selected_action, action)
        self.assertEqual(job.decision["selected"]["action_key"], "set_up_appointment")

    def test_both_passes_tally_together_so_two_weak_agreeing_rules_decide(self):
        action = self._action("nudge_usage")
        self._rule(action, "modest momentum", OutreachRule.WEIGHT_LOW)
        inferred = self._inference_rule(action, "they need help", OutreachRule.WEIGHT_LOW)
        job = services.enqueue_lead(self._lead())
        result = inference.InferenceResult(matched=(inferred,), candidates=(inferred,), todo="")

        with mock.patch.object(inference, "infer", return_value=result):
            self._run(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ActionJob.STATUS_INFERRED_ACTION_CHOSEN)
        self.assertEqual(job.decision["selected"]["weight"], 2)


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

    def test_the_seed_command_maps_the_demo_tenant_to_the_catalog_it_seeds(self):
        call_command("seed_rules_catalog", owner="demo@lockedin.example")

        catalog = TenantCatalog.objects.get(tenant="", owner__username="demo@lockedin.example")
        self.assertTrue(services.rules_for_tenant(catalog.tenant).exists())

    def test_the_seeded_catalog_chooses_the_planners_action_for_a_dormant_lead(self):
        call_command("seed_rules_catalog", owner="demo@lockedin.example", tenant=TENANT)
        lead = self._lead(last_login_date=TODAY - datetime.timedelta(days=60))

        job = self._run(services.enqueue_lead(lead))

        job.refresh_from_db()
        self.assertEqual(job.status, ActionJob.STATUS_DETERMINISTIC_ACTION_CHOSEN)
        self.assertEqual(job.selected_action.key, "reengage_dormant")


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
