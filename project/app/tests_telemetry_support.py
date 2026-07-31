"""Shared in-memory tracing for the telemetry tests. Contains no tests itself.

The OpenTelemetry API installs a global ``TracerProvider`` **once per process**
and refuses (with a warning) to replace it. That is correct for an application
and awkward for a test suite, because several test modules each want recorded
spans and unittest gives no control over which of them runs first.

So this module owns the one install. Every test module calls
:func:`install_in_memory_tracing`, the first call wins, and all of them share the
returned exporter. Per-test isolation comes from :func:`reset_spans`, not from
re-installing.

Named ``tests_*`` so ``[tool.coverage.run] omit`` skips it -- it is scaffolding,
and holding it to the app's coverage bar would be measuring the ruler.
"""

from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from project.app.services import telemetry

_exporter = InMemorySpanExporter()
_installed = False


def install_in_memory_tracing() -> InMemorySpanExporter:
    """Install (once) an SDK provider that records spans in memory.

    ``SimpleSpanProcessor``, not ``BatchSpanProcessor``: a test asserting on
    spans immediately after the code that emitted them must not have to flush or
    sleep first.
    """
    global _installed
    if not _installed:
        telemetry.configure(SimpleSpanProcessor(_exporter))
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
