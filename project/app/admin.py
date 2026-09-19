from django.contrib import admin

from project.app.models import (
    ActionJob,
    ActionType,
    Event,
    Lead,
    OutreachGeneratedCopy,
    OutreachRule,
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


@admin.register(OutreachGeneratedCopy)
class OutreachGeneratedCopyAdmin(admin.ModelAdmin):
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


@admin.register(ActionType)
class ActionTypeAdmin(admin.ModelAdmin):
    list_display = ("key", "label", "owner", "urgency", "enabled", "updated_at")
    list_filter = ("urgency", "enabled")
    search_fields = ("key", "label", "owner__username")


@admin.register(OutreachRule)
class OutreachRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "kind", "action", "weight", "enabled", "updated_at")
    list_filter = ("kind", "weight", "enabled")
    search_fields = ("name", "action__key", "owner__username")


@admin.register(ActionJob)
class ActionJobAdmin(admin.ModelAdmin):
    list_display = ("id", "lead", "status", "selected_action", "attempts", "created_at")
    list_filter = ("status",)
    search_fields = ("lead__id", "lead__agency_name")
