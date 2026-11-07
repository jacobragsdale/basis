"""Each app's OpenTelemetry tracer provider, which FastAPI's own telemetry makes every request's server span with. The
provider is the app's alone, never the process's global one, so two apps in one process, as in tests, never share
spans. It always makes spans, so every request has a trace id to log and to propagate in `traceparent`, and it exports
them only where the settings name an exporter. Its sampler and resource are given here rather than read from
`OTEL_*` variables, since basis's settings are the API's only configuration."""

import sys

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased

from basis.api.settings import Traces


def provider(service: str) -> TracerProvider:
    """A provider with no exporter yet: `export` adds the settings' one when the app starts."""
    # A sampled-out caller's traceparent is honoured; the app's lifespan shuts the provider down, not an atexit hook.
    return TracerProvider(sampler=ParentBased(ALWAYS_ON), resource=Resource({"service.name": service}), shutdown_on_exit=False)


def exporter(traces: Traces | None) -> SpanProcessor | None:
    """The span processor `traces` names, or none: spans are then made, and dropped."""
    if traces is None:
        return None
    if traces.exporter == "console":
        return SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stdout))  # stdout as it is now, which a test may capture
    return BatchSpanProcessor(OTLPSpanExporter(endpoint=str(traces.endpoint), timeout=traces.timeout_s))
