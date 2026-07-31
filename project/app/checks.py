"""Django system checks for project.app.

Registered from AppConfig.ready() so they run on every ``manage.py`` command
(via the system check framework) -- not just the first time an LLM call is
made, which would let a misconfigured deploy silently boot.
"""

import os

from django.core.checks import Error, register
from django.db import connections
from django.db.utils import DatabaseError

from project.app.services.crypto import ENCRYPTION_KEY_ENV_VAR


@register()
def llm_key_encryption_check(app_configs, **kwargs):
    """Fail loudly at boot if a stored LLM API key exists but
    ``LLM_KEY_ENCRYPTION_KEY`` is unset -- that key would be undecryptable
    and every LLM call would break at first use instead of at startup.

    Guards all DB access: a fresh clone/CI run may not have migrations
    applied yet (e.g. before `makemigrations`/first `migrate`), in which case
    this check is a silent no-op rather than a crash.
    """
    if os.environ.get(ENCRYPTION_KEY_ENV_VAR):
        return []

    connection = connections["default"]
    try:
        existing_tables = connection.introspection.table_names()
    except DatabaseError:
        return []

    from project.app.models import LLMConfiguration

    table_name = LLMConfiguration._meta.db_table
    if table_name not in existing_tables:
        return []

    try:
        has_stored_key = LLMConfiguration.objects.filter(encrypted_api_key__isnull=False).exists()
    except DatabaseError:
        return []

    if not has_stored_key:
        return []

    return [
        Error(
            f"{ENCRYPTION_KEY_ENV_VAR} is not set, but a stored LLM API key exists "
            "in LLMConfiguration. Set this env var before starting the app -- "
            "otherwise the stored key cannot be decrypted.",
            id="app.E001",
        )
    ]
