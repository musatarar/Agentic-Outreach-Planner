"""Seed the LLM provider/model catalog (idempotent, safe to re-run).

Model IDs and per-Mtok pricing were read from each vendor's docs at write
time and drift -- re-verify before trusting them for a cost estimate.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from project.app.models import LLMModel, LLMProvider

# Key-issuance URLs are frozen per the MUS-32 spec -- do not change without
# updating the API contract.
PROVIDERS = [
    {
        "key": "claude",
        "label": "Anthropic Claude",
        "api_key_url": "https://console.anthropic.com/settings/keys",
        "api_key_label": "Anthropic API key",
        "api_key_prefix": "sk-ant-",
        "sort_order": 1,
        "models": [
            {
                "model_id": "claude-opus-5",
                "label": "Opus 5",
                "context_window": 1_000_000,
                "default_max_tokens": 500,
                "input_price_per_mtok_usd": "5.00",
                "output_price_per_mtok_usd": "25.00",
                "tier": "flagship",
                "notes": "For complex agentic coding and enterprise work.",
                "sort_order": 1,
            },
            {
                "model_id": "claude-sonnet-5",
                "label": "Sonnet 5",
                "context_window": 1_000_000,
                "default_max_tokens": 500,
                "input_price_per_mtok_usd": "3.00",
                "output_price_per_mtok_usd": "15.00",
                "tier": "balanced",
                "notes": "Best combination of speed and intelligence.",
                "sort_order": 2,
            },
            {
                "model_id": "claude-haiku-4-5",
                "label": "Haiku 4.5",
                "context_window": 200_000,
                "default_max_tokens": 500,
                "input_price_per_mtok_usd": "1.00",
                "output_price_per_mtok_usd": "5.00",
                "tier": "fast",
                "notes": "Fastest Claude model, near-frontier intelligence.",
                "sort_order": 3,
            },
        ],
    },
    {
        "key": "chatgpt",
        "label": "OpenAI ChatGPT",
        "api_key_url": "https://platform.openai.com/api-keys",
        "api_key_label": "OpenAI API key",
        "api_key_prefix": "sk-",
        "sort_order": 2,
        "models": [
            {
                "model_id": "gpt-5.6-sol",
                "label": "GPT-5.6 Sol",
                "context_window": 1_050_000,
                "default_max_tokens": 500,
                "input_price_per_mtok_usd": "5.00",
                "output_price_per_mtok_usd": "30.00",
                "tier": "flagship",
                "notes": "Flagship GPT-5.6 model.",
                "sort_order": 1,
            },
            {
                "model_id": "gpt-5.6-luna",
                "label": "GPT-5.6 Luna",
                "context_window": 1_050_000,
                "default_max_tokens": 500,
                "input_price_per_mtok_usd": "1.00",
                "output_price_per_mtok_usd": "6.00",
                "tier": "mini",
                "notes": "Cheapest GPT-5.6 tier, good for lightweight tasks.",
                "sort_order": 2,
            },
        ],
    },
    {
        "key": "deepseek",
        "label": "DeepSeek",
        "api_key_url": "https://platform.deepseek.com/api_keys",
        "api_key_label": "DeepSeek API key",
        "api_key_prefix": "sk-",
        "sort_order": 3,
        "models": [
            {
                "model_id": "deepseek-v4-flash",
                "label": "DeepSeek V4 Flash",
                "context_window": 1_000_000,
                "default_max_tokens": 500,
                "input_price_per_mtok_usd": "0.14",
                "output_price_per_mtok_usd": "0.28",
                "tier": "fast",
                "notes": "Non-thinking mode; replaces the retired deepseek-chat.",
                "sort_order": 1,
            },
            {
                "model_id": "deepseek-v4-pro",
                "label": "DeepSeek V4 Pro",
                "context_window": 1_000_000,
                "default_max_tokens": 500,
                "input_price_per_mtok_usd": "0.435",
                "output_price_per_mtok_usd": "0.87",
                "tier": "reasoning",
                "notes": "Thinking mode; replaces the retired deepseek-reasoner.",
                "sort_order": 2,
            },
        ],
    },
    {
        "key": "groq",
        "label": "Groq",
        "api_key_url": "https://console.groq.com/keys",
        "api_key_label": "Groq API key",
        "api_key_prefix": "gsk_",
        "sort_order": 4,
        "models": [
            {
                "model_id": "openai/gpt-oss-20b",
                "label": "Llama 3.1 8B Instant",
                "context_window": 131_072,
                "default_max_tokens": 500,
                "input_price_per_mtok_usd": "0.05",
                "output_price_per_mtok_usd": "0.08",
                "tier": "free",
                "notes": "Free tier, no credit card required.",
                "sort_order": 1,
            },
        ],
    },
]


class Command(BaseCommand):
    help = "Seed the LLM provider/model catalog (idempotent, safe to re-run)."

    @transaction.atomic
    def handle(self, *args, **options):
        provider_count = 0
        model_count = 0

        for entry in PROVIDERS:
            provider, _ = LLMProvider.objects.update_or_create(
                key=entry["key"],
                defaults={
                    "label": entry["label"],
                    "api_key_url": entry["api_key_url"],
                    "api_key_label": entry["api_key_label"],
                    "api_key_prefix": entry["api_key_prefix"],
                    "sort_order": entry["sort_order"],
                    "enabled": True,
                },
            )
            provider_count += 1

            for model_entry in entry["models"]:
                LLMModel.objects.update_or_create(
                    provider=provider,
                    model_id=model_entry["model_id"],
                    defaults={
                        "label": model_entry["label"],
                        "context_window": model_entry["context_window"],
                        "default_max_tokens": model_entry["default_max_tokens"],
                        "input_price_per_mtok_usd": Decimal(
                            model_entry["input_price_per_mtok_usd"]
                        ),
                        "output_price_per_mtok_usd": Decimal(
                            model_entry["output_price_per_mtok_usd"]
                        ),
                        "tier": model_entry["tier"],
                        "notes": model_entry["notes"],
                        "sort_order": model_entry["sort_order"],
                        "enabled": True,
                    },
                )
                model_count += 1

        self.stdout.write(
            self.style.SUCCESS(f"Seeded {provider_count} providers and {model_count} models.")
        )
