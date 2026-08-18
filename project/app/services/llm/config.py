"""Resolve the active LLM provider/model/key configuration from the database.

Key precedence per provider: a stored (encrypted) key on the active
:class:`~project.app.models.LLMConfiguration` row (``"database"``), else the
provider's env var (``"environment"``), else its catalog default model with no
key (``"none"``).
"""

import os

from project.app.services.crypto import decrypt_key

# Provider -> env var name(s) its adapter accepts, in priority order.
# CLAUDE_API_KEY is a legacy alias handled here.
PROVIDER_ENV_VARS = {
    "claude": ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
    "chatgpt": ("OPENAI_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "groq": ("GROQ_API_KEY",),
}

# Used only when no LLMConfiguration row exists at all (fresh DB).
_DEFAULT_PROVIDER = "groq"
_DEFAULT_MAX_TOKENS = 500


def _env_key_for(provider):
    for env_var in PROVIDER_ENV_VARS.get(provider, ()):
        value = os.environ.get(env_var)
        if value:
            return value
    return None


def _active_row():
    from project.app.models import LLMConfiguration

    return LLMConfiguration.objects.select_related("model").filter(pk=1).first()


def get_provider():
    """Name of the active provider, e.g. ``"claude"`` or ``"groq"``."""
    row = _active_row()
    return row.provider_id if row else _DEFAULT_PROVIDER


def get_provider_config(name):
    """Model/max_tokens for ``name`` (not necessarily the active provider).

    The active configuration's values when ``name`` is active; otherwise
    ``name``'s catalog default model (lowest ``sort_order`` among enabled
    models), or ``{}`` when ``name`` has no catalog entries.
    """
    from project.app.models import LLMModel

    row = _active_row()
    if row and row.provider_id == name:
        return {"model": row.model.model_id, "max_tokens": row.max_tokens}

    model = (
        LLMModel.objects.filter(provider_id=name, enabled=True)
        .order_by("sort_order", "model_id")
        .first()
    )
    if model is None:
        return {}
    return {"model": model.model_id, "max_tokens": model.default_max_tokens}


def resolve_active_key(provider=None):
    """Resolve ``(api_key, key_source)`` for ``provider`` (default: the
    active provider).

    ``api_key`` is the plaintext key or ``None``; ``key_source`` is one of
    ``"database"``, ``"environment"``, ``"none"``.
    """
    provider = provider or get_provider()
    row = _active_row()
    if row and row.provider_id == provider and row.encrypted_api_key:
        return decrypt_key(bytes(row.encrypted_api_key)), "database"

    env_key = _env_key_for(provider)
    if env_key:
        return env_key, "environment"
    return None, "none"
