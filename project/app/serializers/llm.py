"""The LLM catalog and configuration-singleton wire formats."""

from rest_framework import serializers

from project.app.models import LLMModel, LLMProvider


class LLMModelSerializer(serializers.ModelSerializer):
    """A single model within a provider's catalog entry.

    ``id`` on the wire is the provider-facing ``model_id``, not the numeric pk.
    """

    id = serializers.CharField(source="model_id")
    # coerce_to_string=False: prices serialize as JSON numbers, per the frozen contract.
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

    Not a ``ModelSerializer``: the wire format uses provider ``key`` and
    ``model_id`` strings, and the API key is write-only, never echoed back.
    """

    provider = serializers.CharField()
    model = serializers.CharField()
    max_tokens = serializers.IntegerField(min_value=1)
    # Omit from a PUT to keep the stored key, null to clear it, a string to replace it.
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
        # instance.provider_id is the provider's pk (its `key`); instance.model_id
        # would be the *numeric* LLMModel pk, hence the traversal.
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
