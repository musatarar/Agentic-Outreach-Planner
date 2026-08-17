"""The project's one authentication class (MUS-37): the magic-link session
governs the whole API, including the stored provider API key."""

from rest_framework.authentication import SessionAuthentication


class SessionAuthenticationWith401(SessionAuthentication):
    """SessionAuthentication that produces a 401, not a 403, when anonymous.

    DRF emits 403 when the first authenticator returns no ``authenticate_header``
    (SessionAuthentication returns None); returning a scheme is the whole fix.
    """

    def authenticate_header(self, request):
        return 'Session realm="api"'
