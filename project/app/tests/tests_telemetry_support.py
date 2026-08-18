"""Shared in-memory tracing for the telemetry tests — owns the one process-global
provider install; no tests here. Named ``tests_*`` so coverage's omit list skips it."""

import threading

from django.test import SimpleTestCase
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from project.app.services import telemetry

_exporter = InMemorySpanExporter()
_lock = threading.Lock()
_installed = False


def install_in_memory_tracing() -> InMemorySpanExporter:
    """Install (once) an SDK provider recording spans in memory; raises if the
    install did not take. ``SimpleSpanProcessor`` so tests never need to flush."""
    global _installed
    with _lock:
        if not _installed:
            if not telemetry.configure(SimpleSpanProcessor(_exporter)):
                raise RuntimeError(
                    "Could not install the in-memory TracerProvider: something else "
                    "installed one first. Check that OTEL_EXPORTER_OTLP_ENDPOINT is not "
                    "set for this process."
                )
            _installed = True
        return _exporter


def reset_spans() -> None:
    """Drop everything recorded so far. Call from ``setUp``."""
    _exporter.clear()


def spans_named(name: str) -> list:
    """Finished spans with this exact name, in the order they finished."""
    return [span for span in _exporter.get_finished_spans() if span.name == name]


def one_span_named(name: str):
    """The single finished span with this name; fails loudly if there isn't one."""
    matches = spans_named(name)
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one span named {name!r}, found {len(matches)}: "
            f"{[s.name for s in _exporter.get_finished_spans()]}"
        )
    return matches[0]


class RecordingMixin:
    """Installs the shared provider and clears recorded spans between tests;
    a mixin because callers need different Django base classes."""

    exporter: InMemorySpanExporter

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.exporter = install_in_memory_tracing()

    def setUp(self):
        super().setUp()
        reset_spans()


class RecordingTestCase(RecordingMixin, SimpleTestCase):
    """A database-free test that asserts on recorded spans."""
