"""OpenTelemetry instrumentation for the outreach planner (MUS-25).

``setup`` bootstraps (SDK only when an OTLP endpoint is configured),
``semconv`` pins every attribute and metric name, ``genai`` holds the span and
metric helpers.
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
