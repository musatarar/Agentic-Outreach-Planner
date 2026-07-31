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

**Why OTLP over HTTP.** ``opentelemetry-exporter-otlp-proto-grpc`` pulls in
``grpcio``, whose wheel availability lags new Python minors and whose source
build is the single most common dependency failure in a CI matrix — and this
repo's matrix includes 3.13. Phoenix serves the OTLP HTTP collector on the same
port as its UI, so nothing is lost.
"""

from __future__ import annotations

import atexit
import os
import sys
import threading
from typing import TYPE_CHECKING

from opentelemetry import trace

if TYPE_CHECKING:  # pragma: no cover - annotations only
    # Imported under TYPE_CHECKING so the SDK is not pulled in at import time.
    # The API is a hard dependency of this app; the SDK is only reached when an
    # endpoint is configured, and keeping that true at module scope is what lets
    # the no-endpoint path stay genuinely cheap.
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import SpanProcessor

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

_install_lock = threading.Lock()
_installed = False


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


def _build_resource() -> "Resource":
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


def _build_otlp_span_processor() -> "SpanProcessor":  # pragma: no cover - see below
    """Construct the real OTLP exporter.

    Deliberately three lines and deliberately the only thing in this package
    without a test. Covering it would mean either standing up a collector in CI
    or asserting that a constructor was called with the arguments we just wrote
    down — the first is not worth it and the second tests nothing. Everything
    that decides *whether* to build one is tested; this is only the build.
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    return BatchSpanProcessor(OTLPSpanExporter())


def configure(span_processor: "SpanProcessor | None" = None) -> bool:
    """Install an SDK ``TracerProvider`` for this process.

    Returns ``True`` if this call installed one, ``False`` if one was already
    installed. Never raises on a second call: ``ready()`` can fire twice, and a
    duplicate bootstrap is a startup quirk, not an error worth taking a process
    down for.

    ``span_processor`` is injected by tests (an ``InMemorySpanExporter`` behind
    a ``SimpleSpanProcessor``); production passes nothing and gets the batching
    OTLP exporter.
    """
    global _installed

    from opentelemetry.sdk.trace import TracerProvider

    with _install_lock:
        if _installed:
            return False
        processor = span_processor if span_processor is not None else _build_otlp_span_processor()
        provider = TracerProvider(resource=_build_resource())
        provider.add_span_processor(processor)
        trace.set_tracer_provider(provider)
        # Flush the batch buffer on the way out -- see the module docstring.
        atexit.register(provider.shutdown)
        _installed = True
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
    """
    if "runserver" not in sys.argv:
        return False
    if "--noreload" in sys.argv:
        return False
    return os.environ.get("RUN_MAIN") != "true"


def configure_from_env() -> bool:
    """Entry point for ``AppConfig.ready()``.

    Returns ``True`` when this call installed a provider. Everything else —
    no endpoint configured, already installed, running in the autoreloader's
    watcher process — returns ``False`` and leaves the API's no-op provider in
    place, which is a fully supported state and not a degraded one.
    """
    if otlp_endpoint() is None:
        return False
    if _is_autoreload_parent():
        return False
    return configure()


def _reset_for_tests() -> None:
    """Clear the idempotence flag. Test-support only.

    Does **not** unset the global ``TracerProvider``: the OTel API installs it
    once per process by design and re-setting it only logs a warning. Tests that
    need recorded spans share one in-memory provider — see
    ``project/app/tests_telemetry_support.py``.
    """
    global _installed
    with _install_lock:
        _installed = False


__all__ = [
    "DEFAULT_SERVICE_NAME",
    "ENDPOINT_ENV_VARS",
    "INSTRUMENTATION_NAME",
    "INSTRUMENTATION_VERSION",
    "configure",
    "configure_from_env",
    "get_tracer",
    "is_installed",
    "otlp_endpoint",
]
