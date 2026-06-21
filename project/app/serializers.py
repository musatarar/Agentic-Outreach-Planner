from rest_framework import serializers

from project.app.models import Lead, OutreachAction, ReviewDecision
from project.app.services.actions import ACTION_TYPES


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


class ReviewDecisionSerializer(serializers.ModelSerializer):
    """A BD reviewer's decision on a needs_human outreach action."""

    class Meta:
        model = ReviewDecision
        fields = "__all__"
        read_only_fields = ["status", "created_at"]

    def validate(self, data):
        kind = data.get("kind")
        if kind == ReviewDecision.KIND_SELECT:
            if data.get("selected_action_type") not in ACTION_TYPES:
                raise serializers.ValidationError(
                    {"selected_action_type": "Must be a known action type."}
                )
            data["status"] = ReviewDecision.STATUS_RESOLVED
        elif kind == ReviewDecision.KIND_PROPOSE:
            if not (data.get("proposed_name") or "").strip():
                raise serializers.ValidationError(
                    {"proposed_name": "This field is required."}
                )
            if not (data.get("proposed_what") or "").strip():
                raise serializers.ValidationError(
                    {"proposed_what": "This field is required."}
                )
            data["status"] = ReviewDecision.STATUS_PENDING
        else:
            raise serializers.ValidationError({"kind": "Unknown decision kind."})
        return data
