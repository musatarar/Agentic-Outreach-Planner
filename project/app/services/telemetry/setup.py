"""OpenTelemetry bootstrap (MUS-25).

Instrumented code must not know whether telemetry is switched on: the OTel API
is used unconditionally and the SDK installed only when an OTLP endpoint is
configured, so the no-endpoint path runs the same statements against no-op
providers. Installed from ``AppConfig.ready()`` (after apps load, once per
entrypoint), flushed at ``atexit``, and never fatal — a bad ``OTEL_*`` variable
costs telemetry, not the application. Transport is OTLP over HTTP, which makes
``OTEL_EXPORTER_OTLP_PROTOCOL`` inert.
"""

from __future__ import annotations

import atexit
import logging
import os
import sys
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from opentelemetry import metrics, trace

from . import semconv

if TYPE_CHECKING:  # pragma: no cover - annotations only
    # Under TYPE_CHECKING so the SDK is not imported when no endpoint is set.
    from opentelemetry.sdk.metrics.export import MetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import SpanProcessor

logger = logging.getLogger(__name__)

# Identifies this instrumentation to the backend (the "scope" on every span).
INSTRUMENTATION_NAME = "project.app.services.telemetry"
INSTRUMENTATION_VERSION = "0.1.0"

# Used when OTEL_SERVICE_NAME is unset. Matches docker-compose.yml, so local
# `runserver` and the compose stack land in the same Phoenix project.
DEFAULT_SERVICE_NAME = "outreach-planner"

# Either variable turns tracing on; the traces-specific one wins.
ENDPOINT_ENV_VARS = ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT")

# Cap on the exit-time flush. The SDK's 30s default means 30 seconds between
# Ctrl-C and a shell when the endpoint has gone away.
EXPORT_TIMEOUT_MS = 5000

# Metrics are switched on SEPARATELY, never by OTEL_EXPORTER_OTLP_ENDPOINT:
# Phoenix is not a metrics backend, so a traces endpoint alone must not start a
# metrics exporter that 404s all day.
METRICS_ENDPOINT_ENV_VARS = ("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",)

# Dev escape hatch: print metrics to stdout on the periodic interval.
CONSOLE_METRICS_ENV = "OUTREACH_OTEL_CONSOLE_METRICS"

# The semantic conventions' advised boundaries VERBATIM -- update by copying
# from the spec, not by extending the pattern. The SDK's defaults are wrong for
# both instruments in opposite directions.
#
# Latency, seconds: 0.01 doubling to 81.92.
OPERATION_DURATION_BUCKETS = (
    0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92,
)  # fmt: skip
# Token counts: 1 quadrupling to 67108864.
TOKEN_USAGE_BUCKETS = (
    1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304, 16777216, 67108864,
)  # fmt: skip

_install_lock = threading.Lock()
_installed = False
_metrics_installed = False


def get_tracer() -> trace.Tracer:
    """The tracer every module in this app should use.

    Resolved per call, not cached at import time: ``ready()`` installs the
    provider after imports, and a tracer cached before that stays a no-op.
    """
    return trace.get_tracer(INSTRUMENTATION_NAME, INSTRUMENTATION_VERSION)


def otlp_endpoint() -> str | None:
    """The configured OTLP endpoint, or ``None`` when tracing is off."""
    for name in ENDPOINT_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def is_installed() -> bool:
    """Whether this process has installed an SDK ``TracerProvider``."""
    return _installed


def get_meter() -> metrics.Meter:
    """The meter every module in this app should use.

    Resolved per call for the same reason :func:`get_tracer` is; the
    instruments themselves are cached in ``genai``.
    """
    return metrics.get_meter(INSTRUMENTATION_NAME, INSTRUMENTATION_VERSION)


def otlp_metrics_endpoint() -> str | None:
    """The configured OTLP **metrics** endpoint, or ``None``."""
    for name in METRICS_ENDPOINT_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def is_metrics_installed() -> bool:
    """Whether this process has installed an SDK ``MeterProvider``."""
    return _metrics_installed


def _build_resource() -> Resource:
    """Service identity for every span this process emits.

    ``Resource.create`` already reads the ``OTEL_*`` variables, so the default
    service name is supplied only when the environment has not set one.
    """
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource

    attributes: dict[str, str] = {}
    if not os.environ.get("OTEL_SERVICE_NAME", "").strip():
        attributes[SERVICE_NAME] = DEFAULT_SERVICE_NAME
    return Resource.create(attributes)


def _build_otlp_span_processor() -> SpanProcessor:  # pragma: no cover - see below
    """Construct the real OTLP exporter.

    Untested by design (it would need a live collector), and the only thing
    here that can raise — which is why :func:`configure_from_env` wraps it.
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    return BatchSpanProcessor(OTLPSpanExporter(), export_timeout_millis=EXPORT_TIMEOUT_MS)


def configure(span_processor: SpanProcessor | None = None) -> bool:
    """Install an SDK ``TracerProvider`` for this process.

    Returns ``True`` only if this call installed one; never raises on a second
    call, since ``ready()`` can fire twice. ``span_processor`` is injected by
    tests; production gets the batching OTLP exporter. The install is verified
    by reading the provider back, because ``set_tracer_provider`` keeps the
    incumbent and merely logs rather than raising.
    """
    global _installed

    from opentelemetry.sdk.trace import TracerProvider

    with _install_lock:
        if _installed:
            return False
        processor = span_processor if span_processor is not None else _build_otlp_span_processor()
        # shutdown_on_exit=False: registered below instead, so there is exactly
        # one handler.
        provider = TracerProvider(resource=_build_resource(), shutdown_on_exit=False)
        provider.add_span_processor(processor)
        trace.set_tracer_provider(provider)
        if trace.get_tracer_provider() is not provider:
            logger.warning(
                "A TracerProvider was already registered; this one was refused by the "
                "OpenTelemetry API and has been discarded."
            )
            provider.shutdown()
            return False
        # Flush the batch buffer on the way out, or Ctrl-C drops its tail.
        atexit.register(provider.shutdown)
        _installed = True
        return True


def _build_metric_views() -> list:
    """Explicit bucket boundaries for the two GenAI histograms.

    A ``View`` is the only way to set boundaries in this SDK; ``create_histogram``
    has no bucket argument.
    """
    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View

    return [
        View(
            instrument_name=semconv.METRIC_OPERATION_DURATION,
            aggregation=ExplicitBucketHistogramAggregation(OPERATION_DURATION_BUCKETS),
        ),
        View(
            instrument_name=semconv.METRIC_TOKEN_USAGE,
            aggregation=ExplicitBucketHistogramAggregation(TOKEN_USAGE_BUCKETS),
        ),
    ]


def _console_metrics_enabled() -> bool:
    return os.environ.get(CONSOLE_METRICS_ENV, "").strip() == "1"


def metrics_enabled() -> bool:
    """Whether anything is configured to collect metrics.

    Answered from the environment alone, before any SDK module is imported, so
    a process with telemetry off never pays for the metrics SDK.
    """
    return otlp_metrics_endpoint() is not None or _console_metrics_enabled()


def _build_metric_readers() -> list[MetricReader]:  # pragma: no cover - see below
    """Construct the real metric readers. The metrics twin of
    :func:`_build_otlp_span_processor`, uncovered for the same reason."""
    from opentelemetry.sdk.metrics.export import (
        ConsoleMetricExporter,
        PeriodicExportingMetricReader,
    )

    readers: list[MetricReader] = []
    if otlp_metrics_endpoint() is not None:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

        readers.append(PeriodicExportingMetricReader(OTLPMetricExporter()))
    if _console_metrics_enabled():
        readers.append(PeriodicExportingMetricReader(ConsoleMetricExporter()))
    return readers


def configure_metrics(metric_reader: MetricReader | None = None) -> bool:
    """Install an SDK ``MeterProvider`` for this process.

    Switched separately from :func:`configure` because Phoenix ingests traces
    but not metrics. With no reader installed the API's no-op meter takes over,
    so instruments are always created and recorded to. The install is verified
    by reading the provider back, as in :func:`configure`.
    """
    global _metrics_installed

    if metric_reader is None and not metrics_enabled():
        # Before the import, deliberately -- see `metrics_enabled`.
        return False

    from opentelemetry.sdk.metrics import MeterProvider

    with _install_lock:
        if _metrics_installed:
            return False
        readers = [metric_reader] if metric_reader is not None else _build_metric_readers()
        if not readers:
            return False
        provider = MeterProvider(
            metric_readers=readers,
            resource=_build_resource(),
            views=_build_metric_views(),
            shutdown_on_exit=False,
        )
        metrics.set_meter_provider(provider)
        if metrics.get_meter_provider() is not provider:
            logger.warning(
                "A MeterProvider was already registered; this one was refused by the "
                "OpenTelemetry API and has been discarded."
            )
            provider.shutdown()
            return False
        atexit.register(provider.shutdown)
        _metrics_installed = True
        return True


def _is_autoreload_parent() -> bool:
    """True in ``runserver``'s watcher process, which must not export.

    Django marks the reloader's child with ``RUN_MAIN=true``, but that is unset
    for every other entrypoint — so the ``runserver`` check comes first and
    ``RUN_MAIN`` only disambiguates within it.
    """
    if "runserver" not in sys.argv:
        return False
    if "--noreload" in sys.argv:
        return False
    return os.environ.get("RUN_MAIN") != "true"


def _is_test_runner() -> bool:
    """True under ``manage.py test``, where an exporter must never be installed.

    Otherwise a developer with ``OTEL_EXPORTER_OTLP_ENDPOINT`` set ships every
    test span to that collector, and the suite's own in-memory provider is
    refused. Matched at the management-command position, not anywhere in argv.
    """
    return len(sys.argv) > 1 and sys.argv[1] == "test"


def configure_from_env() -> bool:
    """Entry point for ``AppConfig.ready()``.

    Returns ``True`` when this call installed at least one provider; everything
    else returns ``False`` and leaves the API's no-op providers in place, which
    is a supported state. Traces and metrics are decided independently.
    """
    if _is_autoreload_parent() or _is_test_runner():
        return False
    # Two statements, not `a or b`: both halves must run. Short-circuiting would
    # silently skip metrics whenever traces installed first.
    traces = _install("traces", _configure_traces)
    metrics_installed = _install("metrics", configure_metrics)
    return traces or metrics_installed


def _configure_traces() -> bool:
    return configure() if otlp_endpoint() is not None else False


def _install(what: str, configure_fn: Callable[[], bool]) -> bool:
    """Run one half of the bootstrap, surviving a bad configuration.

    Broad by design and only on the boot path, where a mistyped ``OTEL_*``
    variable must cost telemetry and nothing else; ``configure()`` and
    ``configure_metrics()`` stay strict for tests. The two halves are
    independent, so broken metrics configuration does not cost traces.
    """
    try:
        return configure_fn()
    except Exception:
        logger.warning(
            "OpenTelemetry %s could not be configured; continuing without them.",
            what,
            exc_info=True,
        )
        return False


def _reset_for_tests() -> None:
    """Clear the idempotence flags. Test-support only.

    Does not unset the global providers — the OTel API installs each once per
    process; tests share one in-memory provider instead.
    """
    global _installed, _metrics_installed
    with _install_lock:
        _installed = False
        _metrics_installed = False


__all__ = [
    "CONSOLE_METRICS_ENV",
    "DEFAULT_SERVICE_NAME",
    "ENDPOINT_ENV_VARS",
    "INSTRUMENTATION_NAME",
    "INSTRUMENTATION_VERSION",
    "METRICS_ENDPOINT_ENV_VARS",
    "OPERATION_DURATION_BUCKETS",
    "TOKEN_USAGE_BUCKETS",
    "configure",
    "configure_from_env",
    "configure_metrics",
    "get_meter",
    "get_tracer",
    "is_installed",
    "is_metrics_installed",
    "otlp_endpoint",
    "otlp_metrics_endpoint",
]
