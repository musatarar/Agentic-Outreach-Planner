"""Load LLM provider selection from ``config.toml``.

``config.toml`` lives at the repo root (committed, no secrets) and selects the
active provider plus per-provider model overrides. API keys are NOT stored here
-- they stay in ``.env`` and are read from the environment by each adapter.

Python 3.11+ has ``tomllib`` in the stdlib; on 3.9/3.10 we fall back to the
pure-Python ``tomli`` package (``pip install tomli``).
"""

import os
from functools import lru_cache
from pathlib import Path

try:  # py3.11+
    import tomllib
except ModuleNotFoundError:  # py3.9 / 3.10 -- requires `pip install tomli`
    import tomli as tomllib

# Repo root is three levels up from this file:
# project/app/services/llm/config.py -> <repo root>
_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config.toml"

# Provider used when config.toml is missing or has no [llm] provider set, so
# the app still boots without a config file.
_FALLBACK_PROVIDER = "claude"


@lru_cache(maxsize=1)
def _load_config():
    """Read and cache the parsed ``config.toml`` (empty dict if absent)."""
    path = Path(os.environ.get("LLM_CONFIG_PATH", _DEFAULT_CONFIG_PATH))
    if not path.exists():
        return {}
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def get_provider():
    """Name of the active provider, e.g. ``"claude"`` or ``"groq"``."""
    llm = _load_config().get("llm", {})
    return llm.get("provider", _FALLBACK_PROVIDER)


def get_provider_config(name):
    """Per-provider settings block (``model``, ``max_tokens``, ...).

    Returns an empty dict when the provider has no section, so adapters fall
    back to their own built-in defaults.
    """
    return _load_config().get("llm", {}).get(name, {})
