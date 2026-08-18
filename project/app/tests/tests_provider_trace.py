"""ProviderTrace: one audit row per provider call, referenced by its consumers (MUS-71)."""

from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase

from project.app.models import (
    AgentLeadRun,
    AgentStep,
    Lead,
    LLMModel,
    LLMProvider,
    ProviderTrace,
    ProviderTraceContent,
)


def _seed_catalog():
    """One provider + model, shaped as ``seed_llm_catalog`` would leave them."""
    provider = LLMProvider.objects.create(
        key="claude",
        label="Anthropic Claude",
        api_key_url="https://console.anthropic.com/settings/keys",
        api_key_label="Anthropic API key",
        api_key_prefix="sk-ant-",
    )
    LLMModel.objects.create(
        provider=provider,
        model_id="model-a",
        label="Model A",
        context_window=100_000,
        default_max_tokens=500,
        input_price_per_mtok_usd="1.00",
        output_price_per_mtok_usd="1.00",
    )


class ProviderTraceTests(TestCase):
    """The three decisions that must not regress: snapshot, call grain, ordering."""

    def test_audit_survives_a_catalog_wipe(self):
        """Provider/model are snapshot strings, so deleting the catalog rewrites nothing."""
        _seed_catalog()
        trace = ProviderTrace.objects.create(
            provider="claude",
            model_id="model-a",
            trace_run_id="11111111-1111-1111-1111-111111111111",
        )

        LLMProvider.objects.all().delete()  # cascades LLMModel
        self.assertEqual(LLMModel.objects.count(), 0)

        trace.refresh_from_db()
        self.assertEqual(trace.provider, "claude")
        self.assertEqual(trace.model_id, "model-a")
        self.assertEqual(trace.trace_run_id, "11111111-1111-1111-1111-111111111111")

    def test_two_rows_may_share_one_trace_run_id(self):
        """One run makes several calls, so the run id is indexed but never unique."""
        run_id = "22222222-2222-2222-2222-222222222222"
        ProviderTrace.objects.create(provider="claude", model_id="model-a", trace_run_id=run_id)
        ProviderTrace.objects.create(provider="claude", model_id="model-a", trace_run_id=run_id)

        self.assertEqual(ProviderTrace.objects.filter(trace_run_id=run_id).count(), 2)

    def test_default_ordering_is_newest_first(self):
        """An unordered queryset reads the newest call first."""
        older = ProviderTrace.objects.create(provider="claude", model_id="model-a")
        newer = ProviderTrace.objects.create(provider="groq", model_id="model-b")

        self.assertEqual(list(ProviderTrace.objects.all()), [newer, older])


class AgentStepProviderTraceTests(TestCase):
    """The step log points at the audit row instead of re-recording provider/model."""

    @classmethod
    def setUpTestData(cls):
        cls.lead = Lead.objects.create(
            id="lead_pt1",
            agency_name="A",
            contact_name="C",
            contact_email="c@a.com",
            contact_phone="1",
            state="TX",
            num_producers=3,
            years_in_business=4,
            estimated_book_size_usd=1_000_000,
            stage="active_trial",
        )

    def _run(self, trace_run_id="run-pt-1"):
        return AgentLeadRun.objects.create(lead=self.lead, trace_run_id=trace_run_id)

    def test_a_step_that_made_no_provider_call_has_no_trace(self):
        """tool_result and final steps reference nothing -- only llm_call ever does."""
        step = AgentStep.objects.create(lead_run=self._run(), seq=1, kind="tool_result")
        self.assertIsNone(step.provider_trace_id)

    def test_a_trace_a_step_references_cannot_be_deleted(self):
        """PROTECT: audit is never collateral of tidying up something that cites it."""
        trace = ProviderTrace.objects.create(provider="claude", model_id="model-a")
        AgentStep.objects.create(lead_run=self._run(), seq=1, kind="llm_call", provider_trace=trace)

        with self.assertRaises(ProtectedError):
            trace.delete()

    def test_audit_outlives_the_step_that_referenced_it(self):
        """Deleting the lead cascades run and steps away; the audit row stays."""
        trace = ProviderTrace.objects.create(provider="claude", model_id="model-a")
        AgentStep.objects.create(lead_run=self._run(), seq=1, kind="llm_call", provider_trace=trace)

        self.lead.delete()

        self.assertEqual(AgentStep.objects.count(), 0)
        self.assertTrue(ProviderTrace.objects.filter(pk=trace.pk).exists())


class ProviderTraceContentTests(TestCase):
    """Request/reasoning/response live in a side table with its own lifetime."""

    def setUp(self):
        super().setUp()
        self.trace = ProviderTrace.objects.create(provider="claude", model_id="model-a")

    def test_a_trace_holds_at_most_one_content_row(self):
        ProviderTraceContent.objects.create(trace=self.trace, request="ask", response="answer")

        with self.assertRaises(IntegrityError), transaction.atomic():
            ProviderTraceContent.objects.create(trace=self.trace, request="again")

    def test_purging_content_leaves_the_audit_skeleton(self):
        """Retention can drop the bytes; who ran what, and when, survives."""
        ProviderTraceContent.objects.create(trace=self.trace, request="ask", response="answer")

        ProviderTraceContent.objects.filter(trace=self.trace).delete()

        self.trace.refresh_from_db()
        self.assertEqual(self.trace.provider, "claude")
        self.assertFalse(ProviderTraceContent.objects.filter(trace=self.trace).exists())

    def test_deleting_a_trace_takes_its_content_with_it(self):
        ProviderTraceContent.objects.create(trace=self.trace, request="ask")

        self.trace.delete()

        self.assertEqual(ProviderTraceContent.objects.count(), 0)

    def test_reasoning_is_empty_for_providers_that_expose_none(self):
        """Absent reasoning is "", not a claim that the model reasoned emptily."""
        content = ProviderTraceContent.objects.create(trace=self.trace, request="ask")

        content.refresh_from_db()
        self.assertEqual(content.reasoning, "")
