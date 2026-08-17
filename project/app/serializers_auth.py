"""Request serializers for the magic-link auth endpoints (MUS-37).

Input only: both auth responses are small fixed dicts built in the view.
"""

from rest_framework import serializers


class RequestLinkSerializer(serializers.Serializer):
    """Body of ``POST /api/auth/request-link/``. Syntax only; whether the address
    is *allowed* must never change the response (views_auth.AuthRequestLinkView).
    """

    email = serializers.EmailField(max_length=254)


class ConsumeTokenSerializer(serializers.Serializer):
    """Body of ``POST /api/auth/consume/``. ``max_length`` guards against
    hashing megabytes of junk; a real token is 43 characters.
    """

    token = serializers.CharField(max_length=512, trim_whitespace=True)
