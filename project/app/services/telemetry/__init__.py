"""OpenTelemetry instrumentation for the outreach planner (MUS-25).

Two modules so far, one job each:

* :mod:`~project.app.services.telemetry.setup` — bootstrap. Installs an SDK
  ``TracerProvider`` when an OTLP endpoint is configured, and does nothing at
  all when one is not. Callers never branch on which happened.
* :mod:`~project.app.services.telemetry.semconv` — every attribute and metric
  name, in one file, with the targeted spec version in its header.

A third, ``genai``, arrives in 25-b with the span and metric helpers the
planner and the LLM layer call.

It lives under ``services/`` rather than beside ``settings.py`` so that
``mypy project/app/services/`` — the exact target CI runs — covers it.
"""

from . import semconv
from .setup import (
    DEFAULT_SERVICE_NAME,
    configure,
    configure_from_env,
    get_tracer,
    is_installed,
    otlp_endpoint,
)

__all__ = [
    "semconv",
    "DEFAULT_SERVICE_NAME",
    "configure",
    "configure_from_env",
    "get_tracer",
    "is_installed",
    "otlp_endpoint",
]
