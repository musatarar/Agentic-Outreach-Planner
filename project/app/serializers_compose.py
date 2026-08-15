"""Serializers for the Run Composer (MUS-47).

Output only. Scope validation is not a serializer concern here: it has to fail with the
offending filter's *name* attached (see ``compose.scope.ScopeError``), and a DRF
``ValidationError`` flattens that into a field-errors dict the frontend cannot point a
chip at. The views validate through ``validate_scope`` and serialize afterwards.
"""

from rest_framework import serializers

from project.app.models import SavedScope


class SavedScopeSerializer(serializers.ModelSerializer):
    """A named filter set. Exactly the fields the frontend's ``SavedScope`` type reads."""

    class Meta:
        model = SavedScope
        fields = ["id", "name", "filters", "created_at", "created_by"]
