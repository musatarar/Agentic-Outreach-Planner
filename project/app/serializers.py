from rest_framework import serializers

from project.app.models import Lead, OutreachAction


class LeadSerializer(serializers.ModelSerializer):
    """Full Lead representation — all fields."""

    class Meta:
        model = Lead
        fields = "__all__"


class LeadSummarySerializer(serializers.ModelSerializer):
    """Compact Lead representation nested inside an outreach action."""

    class Meta:
        model = Lead
        fields = ["id", "agency_name", "contact_name", "contact_email"]


class OutreachActionSerializer(serializers.ModelSerializer):
    """Outreach action with a nested lead summary, matching the contract shape."""

    lead = LeadSummarySerializer(read_only=True)

    class Meta:
        model = OutreachAction
        fields = [
            "id",
            "lead",
            "priority",
            "action_type",
            "reason",
            "suggested_copy",
            "needs_human",
            "further_action",
            "created_at",
        ]
