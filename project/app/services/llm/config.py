"""Resolve the active LLM provider/model/key from the environment.

Three variables, nothing else:

``LLM_PROVIDER``
    Which adapter runs, e.g. ``groq`` (the default) or ``claude``. An
    unsupported value fails loudly rather than silently falling back.
``LLM_MODEL``
    Optional model id for that provider. Blank means the adapter's own
    ``DEFAULT_MODEL``, so the model is never restated in two places.
the provider's key variable
    ``GROQ_API_KEY``, ``ANTHROPIC_API_KEY``, ... per :data:`PROVIDER_ENV_VARS`.

Blank counts as unset everywhere (that is what an untouched ``.env`` line and
a docker-compose ``${VAR:-}`` passthrough produce). Django is deliberately not
imported here: the package must stay importable without it.
"""

import os

PROVIDER_VAR = "LLM_PROVIDER"
MODEL_VAR = "LLM_MODEL"

# Provider -> env var name(s) its adapter accepts, in priority order. Also the
# set of values LLM_PROVIDER accepts, so adding a provider means editing this
# map and the client registry in __init__.py -- those two places, no more.
# CLAUDE_API_KEY is a legacy alias handled here.
PROVIDER_ENV_VARS = {
    "claude": ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
    "chatgpt": ("OPENAI_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    # Benchmarking only, and keyless. Naming it here does NOT make it usable:
    # the adapter still refuses to be built without the explicit opt-in its own
    # module documents (see stub.py). Listing it keeps that refusal the barrier,
    # rather than hiding the provider behind a confusing "unsupported value".
    "stub": (),
}

DEFAULT_PROVIDER = "groq"


def _env(name):
    """``os.environ[name]`` with blank read as unset."""
    return (os.environ.get(name) or "").strip() or None


def _configured_provider():
    return _env(PROVIDER_VAR) or DEFAULT_PROVIDER


def get_provider():
    """Name of the active provider, e.g. ``"claude"`` or ``"groq"``."""
    name = _configured_provider()
    if name not in PROVIDER_ENV_VARS:
        raise ValueError(
            f"{PROVIDER_VAR}={name!r} is not a supported LLM provider. "
            f"Valid choices: {', '.join(sorted(PROVIDER_ENV_VARS))}."
        )
    return name


def get_model():
    """The configured model id, or ``None`` for the adapter's default."""
    return _env(MODEL_VAR)


def get_provider_config(name):
    """Build overrides for provider ``name``.

    ``{"model": ...}`` when ``LLM_MODEL`` names one *and* ``name`` is the
    active provider, else ``{}`` -- a model id belongs to the provider it was
    configured for, so it never leaks onto a different one.
    """
    model = get_model()
    if model and name == _configured_provider():
        return {"model": model}
    return {}


def resolve_api_key(provider=None):
    """The API key for ``provider`` (default: the active one), or ``None``."""
    provider = provider or get_provider()
    for env_var in PROVIDER_ENV_VARS.get(provider, ()):
        value = os.environ.get(env_var)
        if value:
            return value
    return None
