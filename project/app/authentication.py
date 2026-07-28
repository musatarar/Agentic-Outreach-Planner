"""The project's one authentication class (MUS-37).

Before MUS-37 this module held an HTTP Basic Auth class guarding the two
``/api/llm/config*`` views while everything else was ``AllowAny``. Both are
gone: the magic-link session now governs the whole API, including the stored
provider API key -- the actually-sensitive thing in this database, which has
no business sitting behind a *different* credential from everything else.
Two auth systems in a single-operator tool is one too many.
"""

from rest_framework.authentication import SessionAuthentication


class SessionAuthenticationWith401(SessionAuthentication):
    """SessionAuthentication that produces a 401, not a 403, when anonymous.

    DRF chooses 401 vs 403 by asking the *first* authenticator for an
    ``authenticate_header``. ``SessionAuthentication`` returns None, so a
    request with no session gets 403 -- which would mean MUS-38's 401 handling
    never fires and the route guard never redirects. Returning a scheme here
    is the whole fix.
    """

    def authenticate_header(self, request):
        return 'Session realm="api"'
