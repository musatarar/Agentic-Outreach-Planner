"""HTTP Basic Auth for the LLM configuration endpoints only.

Every other endpoint in this API is intentionally AllowAny (see
SECURITY.md) -- there is no global ``REST_FRAMEWORK`` auth default. This
class is applied explicitly to the 3 ``/api/llm/*`` views that read/write
provider API keys, checked against a single credential pair from
``LLM_ADMIN_USERNAME`` / ``LLM_ADMIN_PASSWORD`` (see project/settings.py).
"""

import hmac

from django.conf import settings
from rest_framework.authentication import BasicAuthentication
from rest_framework.exceptions import AuthenticationFailed


class LLMAdminUser:
    """Minimal truthy "user" for the single configured admin credential.

    Not a real ``django.contrib.auth`` user -- there's no user model backing
    this login, just one shared credential pair for the LLM admin surface.
    """

    is_authenticated = True

    def __str__(self):
        return "llm-admin"


class LLMAdminBasicAuthentication(BasicAuthentication):
    """``BasicAuthentication`` subclass checked against env-configured creds.

    Uses ``hmac.compare_digest`` for both the username and password
    comparisons so a wrong guess can't be timed to leak how many characters
    matched.
    """

    def authenticate_credentials(self, userid, password, request=None):
        expected_user = getattr(settings, "LLM_ADMIN_USERNAME", "") or ""
        expected_pass = getattr(settings, "LLM_ADMIN_PASSWORD", "") or ""
        if not expected_user or not expected_pass:
            raise AuthenticationFailed(
                "LLM admin credentials are not configured on the server "
                "(set LLM_ADMIN_USERNAME / LLM_ADMIN_PASSWORD)."
            )

        user_ok = hmac.compare_digest(userid, expected_user)
        pass_ok = hmac.compare_digest(password, expected_pass)
        if not (user_ok and pass_ok):
            raise AuthenticationFailed("Invalid credentials.")

        return (LLMAdminUser(), None)
