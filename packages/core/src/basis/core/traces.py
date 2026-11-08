"""Tracing, the same for every part. Each run and each app has its own OpenTelemetry tracer provider, and nothing sets
the process's global one, so two in one process, as in tests, never share spans. A provider always makes spans, so every
record logged inside one carries its trace's and span's ids (`basis.core.logs`); it exports them only where the
settings name an exporter (`settings.Traces`): over OTLP/HTTP to `BASIS_TRACES__ENDPOINT`, the Datadog Agent on the
node in a pod, or to stderr on a desktop that asks to see them. With no exporter set, as on a desktop, spans go
nowhere and nothing fails. There are no metrics: Datadog's APM derives requests, errors and latency from the spans.

basis's settings are the only configuration. The provider, its processors and the exporter are given every setting, so
no `OTEL_*` variable changes what they do, but for the two that name the process, `OTEL_SERVICE_NAME` and
`OTEL_RESOURCE_ATTRIBUTES`, which a deploy platform sets and the provider's resource takes (`basis.core.telemetry`); and the one switch the SDK reads whatever it is given, `OTEL_SDK_DISABLED`,
is overridden. The SDK's own OTLP/HTTP exporter is not used: it reads `OTEL_EXPORTER_OTLP_*` variables, its headers
and certificates among them, whatever it is given, and its session follows `HTTP_PROXY`, which in a pod would send the
spans meant for the node's Agent through a proxy. `Otlp` posts the SDK's own encoding of them with httpx2 instead,
which reads no variable.

`trace` is a run's one trace: a provider of its own, its root span, and its tracer current in the block (`tracer`),
which basis-core's own SQL statements make their spans on. An app makes its provider with `provider` and adds the
settings' `exporter` itself."""

import logging
import sys
import threading
import time
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import override

import httpx2
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanLimits, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased
from opentelemetry.trace import NoOpTracer, Span, Tracer

from basis.core import telemetry
from basis.core.settings import Traces

log = logging.getLogger(__name__)
SCOPE = "basis"
"""The instrumentation scope every span of basis's is made in."""
_LIMITS = SpanLimits(
    max_attributes=128, max_events=128, max_links=128, max_span_attributes=128, max_event_attributes=128, max_link_attributes=128,
    max_attribute_length=SpanLimits.UNSET, max_span_attribute_length=SpanLimits.UNSET,
)  # fmt: skip
"""The SDK's defaults, given, so that no `OTEL_*_LIMIT` variable changes them."""
_NO_METRICS = NoOpMeterProvider()
"""Where the SDK's own metrics go, should `OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED` turn them on: nowhere."""
_RETRIED = frozenset({429, 502, 503, 504})
"""The statuses an export is tried again after, as OTLP's specification says."""


def provider(service: str, *, version: str | None, environment: str | None) -> TracerProvider:
    """A provider of `service`'s with no exporter yet, whose spans carry its `version`, the repo's commit, and its
    `environment`, each where it is known, or the names the platform gives the process in their place
    (`basis.core.telemetry`). A sampled-out caller's `traceparent` is honoured."""
    attributes = telemetry.resource(service, version=version, environment=environment)
    made = TracerProvider(
        sampler=ParentBased(ALWAYS_ON), resource=Resource(attributes), shutdown_on_exit=False, id_generator=RandomIdGenerator(), span_limits=_LIMITS, meter_provider=_NO_METRICS
    )  # shut down by whoever made it, as a run's end or an app's lifespan does, never by an atexit hook
    made._disabled = False  # noqa: SLF001 -- read from OTEL_SDK_DISABLED whatever the provider is given; it configures nothing basis decides
    return made


def exporter(traces: Traces | None) -> SpanProcessor | None:
    """The span processor `traces` names, or none: spans are then made, and dropped."""
    if traces is None:
        return None
    if traces.exporter == "console":
        return SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stderr), meter_provider=_NO_METRICS)  # stdout carries a command's results
    if traces.endpoint is None:  # Traces refuses an otlp exporter without one
        msg = "the otlp exporter needs an endpoint, BASIS_TRACES__ENDPOINT"
        raise ValueError(msg)
    milliseconds = traces.timeout_s * 1000
    return BatchSpanProcessor(
        Otlp(str(traces.endpoint), traces.timeout_s), max_queue_size=2048, schedule_delay_millis=5000, max_export_batch_size=512, export_timeout_millis=milliseconds, meter_provider=_NO_METRICS
    )


class Otlp(SpanExporter):
    """Spans, in OTLP's protobuf encoding, posted to `endpoint` within `timeout_s`, and posted again after a 429 or
    a 502, 503 or 504, or a failure to connect, while time is left. A batch that is not accepted then is dropped, which
    fails nothing: each kind of failure is logged once."""

    def __init__(self, endpoint: str, timeout_s: float) -> None:
        self._endpoint = endpoint
        self._timeout_s = timeout_s
        self._client = httpx2.Client(timeout=timeout_s, trust_env=False)
        self._stopped = threading.Event()
        self._reported: set[str] = set()

    @override
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        body = encode_spans(spans).SerializeToString()
        until, wait_s = time.monotonic() + self._timeout_s, 0.5
        while (failure := self._post(body, until)) is not None:
            kind, again = failure
            if not again or self._stopped.is_set() or until - time.monotonic() <= wait_s:
                self._report(kind)
                return SpanExportResult.FAILURE
            self._stopped.wait(wait_s)
            wait_s *= 2
        return SpanExportResult.SUCCESS

    def _post(self, body: bytes, until: float) -> tuple[str, bool] | None:
        """Why the endpoint did not accept `body`, and whether to try again, or None when it did."""
        try:
            response = self._client.post(self._endpoint, content=body, headers={"content-type": "application/x-protobuf"}, timeout=max(until - time.monotonic(), 0.001))
        except httpx2.TransportError as error:  # a connection refused, or a timeout
            return type(error).__name__, True
        if response.is_success:
            return None
        return f"status {response.status_code}", response.status_code in _RETRIED

    def _report(self, kind: str) -> None:
        if kind in self._reported:
            return
        self._reported.add(kind)
        log.warning("could not export spans to %s (%s): they are dropped, and this is logged once for each kind of failure", self._endpoint, kind, extra={"endpoint": self._endpoint, "failure": kind})

    @override
    def shutdown(self) -> None:
        self._stopped.set()
        self._client.close()


_TRACER: ContextVar[Tracer | None] = ContextVar("basis.core.traces.tracer", default=None)
_NONE = NoOpTracer()


def tracer() -> Tracer:
    """The tracer of the run whose code is running, or one that makes no spans outside every run."""
    found = _TRACER.get()
    return found if found is not None else _NONE


@contextmanager
def tracing(used: Tracer) -> Generator[None]:
    """`used` as the tracer of the code the block runs, in its context alone: a task the block starts carries it, and
    a worker thread that copies the context."""
    token = _TRACER.set(used)
    try:
        yield
    finally:
        _TRACER.reset(token)


@contextmanager
def trace(  # noqa: PLR0913 -- the run, and where its spans go, by keyword
    operation: str, *, service: str, settings: Traces | None, version: str | None, environment: str | None, given: TracerProvider | None = None, **attributes: str
) -> Generator[tuple[Tracer, Span]]:
    """A run's one trace: a provider of its own, exporting where `settings` say, or `given`, a test's with an
    exporter of its own; and the root span `operation`, a trace of its own whatever is current, with the provider's
    tracer current in the block. When the block ends, so does the span, and a provider of the run's own shuts down,
    exporting the spans still queued, as a short process must before it exits."""
    made = given if given is not None else provider(service, version=version, environment=environment)
    if given is None and (processor := exporter(settings)) is not None:
        made.add_span_processor(processor)
    used = made.get_tracer(SCOPE)
    try:
        with tracing(used), used.start_as_current_span(operation, context=Context(), attributes=attributes) as span:
            yield used, span
    finally:
        if given is None:
            made.shutdown()
