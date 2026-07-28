"""Request serializers for the magic-link auth endpoints (MUS-37).

Deliberately in their own module rather than in ``serializers.py``: MUS-37 and
MUS-39 both add endpoints in the same wave, and keeping every new serializer
out of the shared file is what makes those two branches merge without a
conflict (contract section 8.1).

These validate *input only*. Both auth responses are small fixed dicts built
in the view, so there is no response serializer to keep in sync with the
pinned bodies in contract section 5.1.
"""

from rest_framework import serializers


class RequestLinkSerializer(serializers.Serializer):
    """Body of ``POST /api/auth/request-link/``.

    Only syntax is checked here. Whether the address is *allowed* is decided
    later and must never change the response -- see
    ``views_auth.AuthRequestLinkView``.
    """

    email = serializers.EmailField(max_length=254)


class ConsumeTokenSerializer(serializers.Serializer):
    """Body of ``POST /api/auth/consume/``.

    ``max_length`` is a cheap guard against someone posting a megabyte of
    junk to make the server hash it; a real token is 43 characters.
    """

    token = serializers.CharField(max_length=512, trim_whitespace=True)
