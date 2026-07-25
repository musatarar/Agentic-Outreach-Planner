"""Load LLM provider selection from ``config.toml``.

``config.toml`` lives at the repo root (committed, no secrets) and selects the
active provider plus per-provider model overrides. API keys are NOT stored here
-- they stay in ``.env`` and are read from the environment by each adapter.

Uses ``tomllib`` from the stdlib (Python 3.11+).
"""

import os
import tomllib
from functools import lru_cache
from pathlib import Path

# Repo root is four levels up from this file:
# project/app/services/llm/config.py -> <repo root>
_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[4] / "config.toml"

# Provider used when config.toml exists but has no [llm] provider set. A missing
# config.toml is now a hard error (see _load_config), not a silent fallback.
_FALLBACK_PROVIDER = "claude"


@lru_cache(maxsize=1)
def _load_config():
    """Read and cache the parsed ``config.toml``.

    Fails loudly if the file is missing: a silent fallback to defaults hides
    misconfiguration (e.g. a wrong path) and sends every request to the
    fallback provider regardless of what the operator selected.
    """
    path = Path(os.environ.get("LLM_CONFIG_PATH", _DEFAULT_CONFIG_PATH))
    if not path.exists():
        raise FileNotFoundError(
            f"LLM config file not found at '{path}'. Create config.toml at the "
            f"repo root (or set LLM_CONFIG_PATH to its location)."
        )
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
