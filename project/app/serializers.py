from rest_framework import serializers

from project.app.models import (
    Lead,
    LLMModel,
    LLMProvider,
    OutreachAction,
    ReviewDecision,
)
from project.app.services.actions import SELECTABLE_ACTION_TYPES


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


class LLMModelSerializer(serializers.ModelSerializer):
    """A single model within a provider's catalog entry.

    ``id`` on the wire is the provider-facing model id (``model_id`` on the
    model), not the row's numeric primary key.
    """

    id = serializers.CharField(source="model_id")
    # coerce_to_string=False so these serialize as JSON numbers (15.0), not
    # DRF's default quoted-string representation ("15.0000"), matching the
    # frozen API contract.
    input_price_per_mtok_usd = serializers.DecimalField(
        max_digits=10, decimal_places=4, coerce_to_string=False
    )
    output_price_per_mtok_usd = serializers.DecimalField(
        max_digits=10, decimal_places=4, coerce_to_string=False
    )

    class Meta:
        model = LLMModel
        fields = [
            "id",
            "label",
            "context_window",
            "default_max_tokens",
            "input_price_per_mtok_usd",
            "output_price_per_mtok_usd",
            "tier",
            "notes",
        ]


class LLMProviderSerializer(serializers.ModelSerializer):
    """A provider entry (with nested models) for GET /api/llm/catalog/."""

    models = LLMModelSerializer(many=True, read_only=True)

    class Meta:
        model = LLMProvider
        fields = [
            "key",
            "label",
            "api_key_url",
            "api_key_label",
            "api_key_prefix",
            "models",
        ]


class LLMConfigurationSerializer(serializers.Serializer):
    """Serializes/validates the LLM configuration singleton.

    Not a ``ModelSerializer``: the wire format uses the provider's ``key``
    and the model's ``model_id`` (human-readable strings), never their DB
    primary keys, and the API key is write-only and never echoed back.
    """

    provider = serializers.CharField()
    model = serializers.CharField()
    max_tokens = serializers.IntegerField(min_value=1)
    # Write-only: omit entirely from a PUT to keep the stored key, send
    # explicit null to clear it, or a string to set/replace it.
    api_key = serializers.CharField(
        write_only=True, required=False, allow_null=True, allow_blank=True
    )

    has_key = serializers.SerializerMethodField()
    key_last_four = serializers.SerializerMethodField()
    key_source = serializers.SerializerMethodField()
    updated_at = serializers.SerializerMethodField()

    def get_has_key(self, obj):
        return bool(getattr(obj, "encrypted_api_key", None))

    def get_key_last_four(self, obj):
        return getattr(obj, "key_last_four", "") or ""

    def get_key_source(self, obj):
        return getattr(obj, "resolved_key_source", "none")

    def get_updated_at(self, obj):
        return getattr(obj, "updated_at", None)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        # instance.provider_id is the provider's own pk (its `key` field), so
        # no extra query is needed. instance.model_id (Django's auto FK-id
        # accessor for the `model` field) would be the *numeric* LLMModel pk,
        # not the provider-facing model_id string -- that needs the traversal.
        ret["provider"] = instance.provider_id
        ret["model"] = instance.model.model_id
        return ret

    def validate(self, data):
        provider_key = data.get("provider")
        model_id = data.get("model")

        try:
            provider = LLMProvider.objects.get(key=provider_key)
        except LLMProvider.DoesNotExist:
            raise serializers.ValidationError({"provider": "Unknown provider."})

        try:
            model = LLMModel.objects.get(provider=provider, model_id=model_id)
        except LLMModel.DoesNotExist:
            raise serializers.ValidationError(
                {"model": "The selected model does not belong to the selected provider."}
            )

        max_tokens = data.get("max_tokens")
        if max_tokens is not None and max_tokens > model.context_window:
            raise serializers.ValidationError(
                {"max_tokens": "max_tokens exceeds the selected model's context window."}
            )

        data["provider"] = provider
        data["model"] = model
        return data


class ReviewDecisionSerializer(serializers.ModelSerializer):
    """A BD reviewer's decision on a needs_human outreach action.

    The field surface is pinned to the triage fields: the send-decision
    columns (``approved_copy``, ``approved_body_sha256``, ``voided_at``) are
    written only by the queue endpoints and must never be client-writable
    here — a crafted POST could otherwise forge a live send approval.
    """

    class Meta:
        model = ReviewDecision
        fields = [
            "id",
            "outreach_action",
            "kind",
            "status",
            "selected_action_type",
            "proposed_name",
            "proposed_what",
            "proposed_when",
            "reviewer",
            "created_at",
        ]
        read_only_fields = ["status", "created_at"]
        # Drop DRF's auto UniqueValidator on the OneToOne field so a duplicate
        # surfaces as the DB IntegrityError -> 409 (one code path, race-safe),
        # rather than an inconsistent 400 that a true concurrent insert dodges.
        extra_kwargs = {"outreach_action": {"validators": []}}

    def validate(self, data):
        action = data.get("outreach_action")
        if action is not None and not action.needs_human:
            raise serializers.ValidationError(
                {"outreach_action": "This action does not require human review."}
            )
        kind = data.get("kind")
        if kind in ReviewDecision.SEND_KINDS:
            raise serializers.ValidationError(
                {
                    "kind": "Send decisions are recorded by POST /api/queue/{id}/approve/ "
                    "or /dismiss/, not this endpoint."
                }
            )
        if kind == ReviewDecision.KIND_SELECT:
            if data.get("selected_action_type") not in SELECTABLE_ACTION_TYPES:
                raise serializers.ValidationError(
                    {"selected_action_type": "Must be a selectable action type."}
                )
            data["status"] = ReviewDecision.STATUS_RESOLVED
        elif kind == ReviewDecision.KIND_PROPOSE:
            if not (data.get("proposed_name") or "").strip():
                raise serializers.ValidationError({"proposed_name": "This field is required."})
            if not (data.get("proposed_what") or "").strip():
                raise serializers.ValidationError({"proposed_what": "This field is required."})
            data["status"] = ReviewDecision.STATUS_PENDING
        else:
            raise serializers.ValidationError({"kind": "Unknown decision kind."})
        return data
