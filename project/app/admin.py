from django.contrib import admin

from project.app.models import (
    Event,
    Lead,
    OutreachAction,
    Tenant,
    TenantMembership,
)


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ("slug", "name", "created_at")
    search_fields = ("slug", "name")


@admin.register(TenantMembership)
class TenantMembershipAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "tenant", "created_at")
    list_filter = ("tenant",)
    search_fields = ("user__username", "user__email", "tenant__slug")


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "tenant",
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
    list_filter = ("tenant", "stage", "state")
    search_fields = ("id", "agency_name", "contact_name", "contact_email")


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "lead", "type", "timestamp")
    list_filter = ("tenant", "type")
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
