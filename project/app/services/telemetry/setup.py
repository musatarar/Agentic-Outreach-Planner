"""OpenTelemetry bootstrap (MUS-25).

The design goal is one sentence long: **instrumented code must not know whether
telemetry is switched on.**

That is achieved by using the OTel *API* unconditionally and installing the
*SDK* only when an OTLP endpoint is configured. With no ``TracerProvider``
registered, ``trace.get_tracer()`` returns a ``NoOpTracer`` whose
``start_as_current_span`` yields a ``NonRecordingSpan`` — an object whose
``set_attribute`` is literally an empty method body. So the planner opens spans
and sets attributes in exactly the same statements whether or not anything is
listening, and there is no ``if TRACING_ENABLED:`` anywhere for a later edit to
get wrong on one branch only. It also means CI exercises the same code path the
demo does, minus the exporter.

**Why ``AppConfig.ready()`` and not ``settings.py``.** ``settings.py`` is
imported before the app registry is populated, so anything it touches that
reaches a model explodes in a way that looks like a settings bug. The other
usual answer — wrapping the entrypoint in ``opentelemetry-instrument`` — would
give the Docker path and ``manage.py runserver`` two different startup
sequences, and drags in auto-instrumentation for libraries nobody asked to
trace. ``ready()`` runs once per process for every entrypoint (runserver,
gunicorn, ``manage.py test``, ``manage.py shell``) and runs after apps load.

``ready()`` is not quite "once per process" under ``runserver``, though: the
autoreloader is a parent process that re-executes itself, so ``ready()`` fires
in both, and two live ``BatchSpanProcessor``s would both hold an exporter.
Hence :func:`configure_from_env`'s autoreload guard and the module-level
idempotence flag.

**Why ``atexit``.** ``BatchSpanProcessor`` buffers. Ctrl-C on a dev server
without a flush drops the tail of the buffer, which is reliably the trace you
just produced and were about to screenshot. ``provider.shutdown()`` flushes it.
The SDK would register that handler itself (``shutdown_on_exit=True``), but it
is passed ``False`` and registered here instead, so there is exactly one
registration and it is visible at the place the reasoning lives.

**Why misconfiguration must not be fatal.** :func:`configure_from_env` catches
everything the exporter's constructor can raise. ``OTEL_EXPORTER_OTLP_TIMEOUT``
is parsed as a float, so an operator writing ``10s`` — an entirely natural
thing to write — otherwise takes down ``manage.py check``, ``migrate`` and
``runserver`` alike, and the Docker entrypoint runs all three. A telemetry
configuration error must degrade to no telemetry, never to no application.

**Why OTLP over HTTP.** ``opentelemetry-exporter-otlp-proto-grpc`` pulls in
``grpcio``, whose wheel availability lags new Python minors and whose source
build is the single most common dependency failure in a CI matrix — and this
repo's matrix includes 3.13. Phoenix serves the OTLP HTTP collector on the same
port as its UI, so nothing is lost. Note this makes
``OTEL_EXPORTER_OTLP_PROTOCOL`` inert: the HTTP exporter is imported by name,
so setting that variable to ``grpc`` does not switch transports.
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
    # Imported under TYPE_CHECKING so the SDK is not pulled in at import time.
    # The API is a hard dependency of this app; the SDK is only reached when an
    # endpoint is configured, and keeping that true at module scope is what lets
    # the no-endpoint path stay genuinely cheap.
    from opentelemetry.sdk.metrics.export import MetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import SpanProcessor

logger = logging.getLogger(__name__)

# Identifies this instrumentation to the backend (the "scope" on every span).
INSTRUMENTATION_NAME = "project.app.services.telemetry"
INSTRUMENTATION_VERSION = "0.1.0"

# Used when OTEL_SERVICE_NAME is unset. Matches the value docker-compose.yml
# sets, so a local `runserver` and the compose stack land in the same Phoenix
# project rather than two.
DEFAULT_SERVICE_NAME = "outreach-planner"

# Either variable turns tracing on. The traces-specific one wins in OTLP's own
# precedence rules, and honouring both means an operator who set only the
# specific one does not silently get nothing.
ENDPOINT_ENV_VARS = ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT")

# Cap on how long the exit-time flush may block. The SDK's default is 30s, and
# against an endpoint that has gone away that is 30 seconds between Ctrl-C on
# `runserver` and getting the shell back -- the same atexit handler that saves
# the demo's last trace becomes the thing that makes the demo look broken.
EXPORT_TIMEOUT_MS = 5000

# Metrics are switched on SEPARATELY, and deliberately not by
# OTEL_EXPORTER_OTLP_ENDPOINT. The demo's trace backend is Phoenix, which is not
# an OTel metrics backend -- pointing the metrics exporter at it would produce a
# steady trickle of 404s in the logs of a stack that is otherwise working
# perfectly. So the traces endpoint alone never starts a metrics exporter.
METRICS_ENDPOINT_ENV_VARS = ("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",)

# Dev escape hatch: print metrics to stdout on the periodic interval. Useful for
# eyeballing the histograms without standing up a Prometheus.
CONSOLE_METRICS_ENV = "OUTREACH_OTEL_CONSOLE_METRICS"

# Explicit histogram buckets, both taken from the semantic conventions. The SDK
# default boundaries (0, 5, 10, 25, ... 10000) are wrong for both instruments
# and wrong in opposite directions: a sub-second latency in seconds falls
# entirely in the first bucket, and a 2000-token prompt lands near the top of a
# range that stops at 10000.
#
# Latency, seconds: 0.01 doubling to 81.92.
OPERATION_DURATION_BUCKETS = (
    0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92,
)  # fmt: skip
# Token counts: 1 quadrupling to 67108864. Wide because prompt sizes span four
# orders of magnitude between a one-line judge call and a full context window.
TOKEN_USAGE_BUCKETS = (
    1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304, 16777216, 67108864,
)  # fmt: skip

_install_lock = threading.Lock()
_installed = False
_metrics_installed = False


def get_tracer() -> trace.Tracer:
    """The tracer every module in this app should use.

    Resolved through the API on each call rather than cached at import time:
    :func:`configure` may install the provider *after* a module has been
    imported (it does — ``ready()`` runs after imports), and a tracer cached
    before that would be a no-op forever.
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

    Resolved per call for the same reason :func:`get_tracer` is. Note the
    *instruments* are cached (see :mod:`project.app.services.telemetry.genai`);
    only the meter lookup is repeated.
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

    ``Resource.create({})`` already reads ``OTEL_SERVICE_NAME`` and
    ``OTEL_RESOURCE_ATTRIBUTES``. Passing ``service.name`` unconditionally would
    *override* the operator's ``OTEL_SERVICE_NAME``, so the default is supplied
    only when the environment has not spoken.
    """
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource

    attributes: dict[str, str] = {}
    if not os.environ.get("OTEL_SERVICE_NAME", "").strip():
        attributes[SERVICE_NAME] = DEFAULT_SERVICE_NAME
    return Resource.create(attributes)


def _build_otlp_span_processor() -> SpanProcessor:  # pragma: no cover - see below
    """Construct the real OTLP exporter.

    Deliberately tiny and deliberately the only thing in this package without a
    test. Covering it would mean either standing up a collector in CI or
    asserting that a constructor was called with the arguments we just wrote
    down — the first is not worth it and the second tests nothing. Everything
    that decides *whether* to build one is tested; this is only the build.

    It is also the only thing here that can raise, which is why
    :func:`configure_from_env` wraps it.
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    return BatchSpanProcessor(OTLPSpanExporter(), export_timeout_millis=EXPORT_TIMEOUT_MS)


def configure(span_processor: SpanProcessor | None = None) -> bool:
    """Install an SDK ``TracerProvider`` for this process.

    Returns ``True`` if this call installed one, ``False`` if it did not. Never
    raises on a second call: ``ready()`` can fire twice, and a duplicate
    bootstrap is a startup quirk, not an error worth taking a process down for.

    ``span_processor`` is injected by tests (an ``InMemorySpanExporter`` behind
    a ``SimpleSpanProcessor``); production passes nothing and gets the batching
    OTLP exporter.

    **The return value is checked against reality, not against our own flag.**
    ``trace.set_tracer_provider`` does not raise when a provider is already
    registered — it logs "Overriding of current TracerProvider is not allowed"
    and keeps the old one. Trusting our flag would let ``is_installed()`` report
    ``True`` while ``get_tracer()`` resolves against something else entirely, and
    would leave an unreachable provider with an ``atexit`` handler attached to
    it. So the install is verified and a refusal is reported as a refusal.
    """
    global _installed

    from opentelemetry.sdk.trace import TracerProvider

    with _install_lock:
        if _installed:
            return False
        processor = span_processor if span_processor is not None else _build_otlp_span_processor()
        # shutdown_on_exit=False: the atexit registration is done below instead,
        # so there is exactly one handler and it sits next to its rationale.
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
        # Flush the batch buffer on the way out -- see the module docstring.
        atexit.register(provider.shutdown)
        _installed = True
        return True


def _build_metric_views() -> list:
    """Explicit bucket boundaries for the two GenAI histograms.

    A ``View`` is the only way to set histogram boundaries in this SDK — the
    ``create_histogram`` call has no bucket argument — so leaving them out is
    not "the default is fine", it is "every latency is in bucket one". See
    :data:`OPERATION_DURATION_BUCKETS`.
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

    Answered from the environment alone, before a single SDK module is
    imported. That ordering is the point: ``configure_metrics`` is called on
    every boot, and an unconditional ``from opentelemetry.sdk.metrics import
    ...`` inside it would drag the whole metrics SDK into a process that has
    telemetry switched off — quietly undoing the "costs nothing when off"
    property the traces side is careful about.
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

    Separate from :func:`configure` because the two have genuinely different
    switches: the demo exports traces to Phoenix, which does not ingest OTel
    metrics. Sharing one switch would mean either shipping a metrics exporter
    that 404s all day or not defining the instruments at all.

    With no reader installed, ``metrics.get_meter()`` returns the API's no-op
    meter and every ``record()`` is an empty method body — the same
    "instrumented code does not know" property the tracing side has. So the
    instruments are always created and always recorded to; whether anything
    collects them is an operator's decision, not the instrumented code's.

    The install is verified by reading the provider back, for the same reason
    :func:`configure` verifies its own: ``set_meter_provider`` logs and keeps
    the incumbent rather than raising.
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

    ``runserver`` without ``--noreload`` runs two processes: a parent that
    watches files and re-executes itself, and a child that is the actual server.
    Django marks the child with ``RUN_MAIN=true``. Keying on ``RUN_MAIN`` alone
    would be wrong — it is unset for ``gunicorn``, ``manage.py test`` and every
    other entrypoint, which would disable telemetry everywhere *except*
    runserver. So the ``runserver`` check comes first and the ``RUN_MAIN`` check
    only disambiguates within it.

    The weak point is that ``runserver`` is matched as a literal argv token, so
    a third-party reloading command under a different name (``runserver_plus``)
    would have its watcher install an exporter too. Every entrypoint this repo
    actually ships — the Dockerfile's ``runserver``, local dev, ``gunicorn``,
    ``manage.py test`` — is covered, and the cost of the miss is a duplicate
    exporter rather than a wrong trace.
    """
    if "runserver" not in sys.argv:
        return False
    if "--noreload" in sys.argv:
        return False
    return os.environ.get("RUN_MAIN") != "true"


def _is_test_runner() -> bool:
    """True under ``manage.py test``, where an exporter must never be installed.

    ``ready()`` runs for the test runner like every other entrypoint, so a
    developer with ``OTEL_EXPORTER_OTLP_ENDPOINT`` exported — exactly the state
    the compose stack creates — would otherwise have the suite install a live
    OTLP exporter and **ship every test span to that collector**, lead ids and
    content digests included. It also breaks the suite: the tests install their
    own in-memory provider, and the API refuses the second registration.

    Matched at the management-command position rather than anywhere in argv, so
    an app label that happens to contain "test" cannot switch telemetry off in
    production.
    """
    return len(sys.argv) > 1 and sys.argv[1] == "test"


def configure_from_env() -> bool:
    """Entry point for ``AppConfig.ready()``.

    Returns ``True`` when this call installed at least one provider. Everything
    else — nothing configured, already installed, running in the autoreloader's
    watcher process, running the test suite, or a broken exporter configuration
    — returns ``False`` and leaves the API's no-op providers in place, which is
    a fully supported state and not a degraded one.

    Traces and metrics are decided independently: a stack exporting traces to
    Phoenix and metrics nowhere is the *expected* configuration here, not an
    incomplete one.
    """
    if _is_autoreload_parent() or _is_test_runner():
        return False
    return _install("traces", _configure_traces) | _install("metrics", configure_metrics)


def _configure_traces() -> bool:
    return configure() if otlp_endpoint() is not None else False


def _install(what: str, configure_fn: Callable[[], bool]) -> bool:
    """Run one half of the bootstrap, surviving a bad configuration.

    Deliberately broad, and deliberately only on this path. ``configure()`` and
    ``configure_metrics()`` stay strict so a test that breaks an exporter sees
    the breakage; this is the boot path, where a mistyped ``OTEL_*`` variable
    must cost telemetry and nothing else. ``OTEL_EXPORTER_OTLP_TIMEOUT`` is
    parsed as a float by the exporter's constructor, so ``10s`` — an entirely
    natural thing to write — would otherwise take down ``manage.py check``,
    ``migrate`` and ``runserver`` alike, and the Docker entrypoint runs all
    three.

    The two halves are independent: broken metrics configuration must not cost
    the traces the demo is actually built around.
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

    Does **not** unset the global providers: the OTel API installs each once per
    process by design and re-setting only logs a warning. Tests that need
    recorded spans share one in-memory provider — see
    ``project/app/tests_telemetry_support.py``.
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
