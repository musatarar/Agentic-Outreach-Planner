"""Lead representations shared by the planner-facing endpoints."""

from rest_framework import serializers

from project.app.models import Lead


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
