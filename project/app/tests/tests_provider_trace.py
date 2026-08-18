"""ProviderTrace: one audit row per provider call, snapshotting the catalog (MUS-71)."""

from django.test import TestCase

from project.app.models import LLMModel, LLMProvider, ProviderTrace


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
