"""Lead representations shared by the planner-facing endpoints.

A lead is ``id`` plus the blob its owner's shape describes, so the wire carries
both and nothing interprets the blob here.
"""

from rest_framework import serializers

from project.app.models import Lead, Shape


class LeadSerializer(serializers.ModelSerializer):
    """Full Lead representation — all fields."""

    class Meta:
        model = Lead
        fields = "__all__"


class LeadSummarySerializer(serializers.ModelSerializer):
    """Compact Lead representation nested inside an outreach action."""

    class Meta:
        model = Lead
        fields = ["id", "data"]


class ShapeSerializer(serializers.ModelSerializer):
    """What one user says a lead and an event are.

    ``owner`` is bound from the session, never the payload, and the write path
    runs ``full_clean()`` so the model's own checks are the only verdict.
    """

    class Meta:
        model = Shape
        fields = ["lead_columns", "event_columns", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]
        # A PUT replaces the declaration whole, so an omitted list is refused
        # rather than read as "keep the stored one".
        extra_kwargs = {
            "lead_columns": {"required": True},
            "event_columns": {"required": True},
        }
