from django.contrib import admin

from project.app.models import (
    Event,
    Lead,
    LLMConfiguration,
    LLMModel,
    LLMProvider,
    OutreachAction,
)


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "agency_name",
        "contact_name",
        "state",
        "stage",
        "estimated_book_size_usd",
        "quotes_created",
        "quotes_submitted",
        "deals_closed",
        "last_login_date",
        "last_contacted_date",
    )
    list_filter = ("stage", "state")
    search_fields = ("id", "agency_name", "contact_name", "contact_email")


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("id", "lead", "type", "timestamp")
    list_filter = ("type",)
    search_fields = ("lead__id", "lead__agency_name")
    date_hierarchy = "timestamp"


@admin.register(OutreachAction)
class OutreachActionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "lead",
        "priority",
        "action_type",
        "needs_human",
        "created_at",
    )
    list_filter = ("priority", "action_type", "needs_human")
    search_fields = ("lead__id", "lead__agency_name", "reason")


@admin.register(LLMProvider)
class LLMProviderAdmin(admin.ModelAdmin):
    list_display = ("key", "label", "sort_order", "enabled")
    list_filter = ("enabled",)
    search_fields = ("key", "label")


@admin.register(LLMModel)
class LLMModelAdmin(admin.ModelAdmin):
    list_display = (
        "model_id",
        "provider",
        "label",
        "tier",
        "context_window",
        "input_price_per_mtok_usd",
        "output_price_per_mtok_usd",
        "enabled",
    )
    list_filter = ("provider", "tier", "enabled")
    search_fields = ("model_id", "label")


@admin.register(LLMConfiguration)
class LLMConfigurationAdmin(admin.ModelAdmin):
    """The stored (or plaintext) API key must never render here."""

    list_display = ("provider", "model", "max_tokens", "key_last_four", "updated_at")
    # encrypted_api_key must never appear in any admin form or list.
    exclude = ("encrypted_api_key",)
    readonly_fields = ("key_last_four", "updated_at")
