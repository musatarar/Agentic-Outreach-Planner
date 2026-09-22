from django.contrib import admin

from project.app.models import (
    ActionJob,
    ActionType,
    Event,
    Lead,
    OutreachAction,
    OutreachRule,
    Shape,
)


@admin.register(Shape)
class ShapeAdmin(admin.ModelAdmin):
    list_display = ("owner", "updated_at")
    search_fields = ("owner__username",)


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    # A lead's columns are its owner's to declare, so the only names here are
    # the structural ones.
    list_display = ("id", "owner")
    list_filter = ("owner",)
    search_fields = ("id",)


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("id", "lead", "timestamp")
    search_fields = ("lead__id",)
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
    search_fields = ("lead__id", "reason")


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
    search_fields = ("lead__id",)
