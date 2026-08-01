"""Shared in-memory tracing for the telemetry tests. Contains no tests itself.

The OpenTelemetry API installs a global ``TracerProvider`` **once per process**
and refuses (with a warning, not an exception) to replace it. That is correct
for an application and awkward for a test suite, because several test modules
each want recorded spans and unittest gives no control over which of them runs
first.

So this module owns the one install. Every test module calls
:func:`install_in_memory_tracing`, the first call wins, and all of them share
the returned exporter.

**Two contracts every recording test must honour**, because the provider is
global and stays recording for the rest of the process once installed:

1. Call :func:`reset_spans` in ``setUp``. Without it a module inherits every
   span every earlier module emitted, and :func:`spans_named` starts matching
   somebody else's. :class:`RecordingTestCase` exists so this is inherited
   rather than remembered.
2. Do not let anything else install first. ``configure_from_env`` refuses to
   install under ``manage.py test`` precisely so a stray
   ``OTEL_EXPORTER_OTLP_ENDPOINT`` in a developer's shell cannot both break
   these tests and ship their spans to a live collector — but the install below
   still asserts it actually won, rather than silently recording into a provider
   that was never registered.

Named ``tests_*`` so ``[tool.coverage.run] omit`` skips it — it is scaffolding,
and holding it to the app's coverage bar would be measuring the ruler.
"""

import threading

from django.test import SimpleTestCase
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from project.app.services import telemetry

_exporter = InMemorySpanExporter()
_lock = threading.Lock()
_installed = False


def install_in_memory_tracing() -> InMemorySpanExporter:
    """Install (once) an SDK provider that records spans in memory.

    ``SimpleSpanProcessor``, not ``BatchSpanProcessor``: a test asserting on
    spans immediately after the code that emitted them must not have to flush or
    sleep first.

    Raises rather than returning a dead exporter if the install did not take.
    A returned-but-unregistered exporter would make every downstream test fail
    with ``not enough values to unpack`` and point at the wrong thing.
    """
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
    """Installs the shared provider and clears it between tests.

    A mixin rather than a base class because the two callers need different
    Django bases — span tests that touch no database use ``SimpleTestCase``,
    the planner tests need ``TestCase`` — and the "clear the shared exporter in
    ``setUp``" contract must be inherited by both rather than remembered by
    either. Forgetting it does not fail; it makes a test pass on a span some
    earlier module emitted, which is worse.
    """

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
