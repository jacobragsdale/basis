"""Tracing as every part has it: a provider of each run's or app's own, configured by basis's settings alone, spans that
go only where the settings say, an OTLP export that fails nothing, and a run's one trace."""

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import override

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import get_current_span

from basis.core import traces
from basis.core.settings import Traces

type Records = list[dict[str, object]]


@dataclass
class Receiver:
    """An OTLP/HTTP receiver, which answers each export with the next of `statuses`, then 200."""

    statuses: list[int] = field(default_factory=list)
    posts: list[tuple[str, dict[str, str], bytes]] = field(default_factory=list)
    port: int = 0

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1/traces"

    def spans(self) -> list[str]:
        names: list[str] = []
        for _, _, body in self.posts:
            request = ExportTraceServiceRequest()
            request.ParseFromString(body)
            names.extend(span.name for resource in request.resource_spans for scope in resource.scope_spans for span in scope.spans)
        return names


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    found = Receiver()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["content-length"]))
            found.posts.append((self.path, {key.lower(): value for key, value in self.headers.items()}, body))
            self.send_response(found.statuses.pop(0) if found.statuses else 200)
            self.send_header("content-length", "0")
            self.end_headers()

        @override
        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    found.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield found
    server.shutdown()
    server.server_close()


def otlp(endpoint: str, *, timeout_s: float = 5) -> Traces:
    return Traces.model_validate({"exporter": "otlp", "endpoint": endpoint, "timeout_s": timeout_s})


def test_a_provider_carries_the_service_and_its_version_and_environment_where_known() -> None:
    known = traces.provider("positions", version="abc123", environment="qa").resource.attributes
    unknown = traces.provider("positions", version=None, environment=None).resource.attributes
    assert dict(known) == {"service.name": "positions", "service.version": "abc123", "deployment.environment.name": "qa"}
    assert dict(unknown) == {"service.name": "positions"}


def test_a_provider_carries_the_names_the_platform_gives_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_SERVICE_NAME", "quote-board-api")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "deployment.environment.name=qa,service.version=3f1c2a9e,service.namespace=quote-board")
    attributes = dict(traces.provider("quote-board", version="abc123", environment="qa").resource.attributes)
    assert attributes == {"service.name": "quote-board-api", "service.version": "3f1c2a9e", "deployment.environment.name": "qa", "service.namespace": "quote-board"}


def test_no_otel_variable_but_the_two_that_name_the_process_changes_what_a_provider_or_its_exporter_does(receiver: Receiver, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "OTEL_SDK_DISABLED": "true", "OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT": "0", "OTEL_ATTRIBUTE_COUNT_LIMIT": "0", "OTEL_TRACES_SAMPLER": "always_off",
        "OTEL_BSP_MAX_EXPORT_BATCH_SIZE": "1",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "authorization=leaked", "OTEL_EXPORTER_OTLP_HEADERS": "authorization=leaked",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://127.0.0.1:9/v1/traces", "OTEL_EXPORTER_OTLP_TRACES_COMPRESSION": "gzip",
        "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": "",
    }.items():  # fmt: skip
        monkeypatch.setenv(name, value)
    provider = traces.provider("positions", version=None, environment=None)
    processor = traces.exporter(otlp(receiver.endpoint))
    assert processor is not None
    provider.add_span_processor(processor)
    with provider.get_tracer("test").start_as_current_span("run", attributes={"basis.job": "positions"}) as span:
        assert span.is_recording()
    with provider.get_tracer("test").start_as_current_span("again"):
        pass
    provider.shutdown()
    [(path, headers, _)] = receiver.posts  # one batch of both spans, to the endpoint the settings name, direct
    assert (path, "authorization" in headers, headers.get("content-encoding"), headers["content-type"]) == ("/v1/traces", False, None, "application/x-protobuf")
    assert receiver.spans() == ["run", "again"]
    request = ExportTraceServiceRequest()
    request.ParseFromString(receiver.posts[0][2])
    [run, _] = request.resource_spans[0].scope_spans[0].spans
    assert [(attribute.key, attribute.value.string_value) for attribute in run.attributes] == [("basis.job", "positions")]  # no limit of 0 dropped it
    assert dict(provider.resource.attributes) == {"service.name": "positions"}


def test_without_traces_settings_spans_are_made_and_go_nowhere(capsys: pytest.CaptureFixture[str]) -> None:
    assert traces.exporter(None) is None
    with traces.trace("run", service="positions", settings=None, version=None, environment=None) as (_, span):
        assert span.get_span_context().is_valid
    assert capsys.readouterr() == ("", "")


def test_the_console_exporter_writes_to_stderr_never_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    with traces.trace("run", service="positions", settings=Traces(exporter="console"), version=None, environment=None):
        pass
    out, err = capsys.readouterr()
    assert (out, '"name": "run"' in err) == ("", True)


def test_an_export_is_posted_again_after_a_503_and_then_accepted(receiver: Receiver) -> None:
    receiver.statuses = [503]
    exported = _export(receiver.endpoint)
    assert (exported, len(receiver.posts), receiver.spans()) == (SpanExportResult.SUCCESS, 2, ["run", "run"])


@pytest.mark.parametrize("statuses", [[400], [503, 503, 503, 503, 503]], ids=["refused", "unavailable-past-its-timeout"])
def test_an_export_that_is_not_accepted_fails_nothing_and_is_logged_once(receiver: Receiver, statuses: list[int], caplog: pytest.LogCaptureFixture) -> None:
    receiver.statuses = statuses * 2
    exporter = traces.Otlp(receiver.endpoint, 1)
    assert [_export_with(exporter), _export_with(exporter)] == [SpanExportResult.FAILURE, SpanExportResult.FAILURE]
    warned = [record.getMessage() for record in caplog.records if record.name == "basis.core.traces"]
    assert warned == [f"could not export spans to {receiver.endpoint} (status {statuses[0]}): they are dropped, and this is logged once for each kind of failure"]


def test_an_endpoint_nothing_serves_fails_nothing(caplog: pytest.LogCaptureFixture) -> None:
    assert _export("http://127.0.0.1:9/v1/traces", timeout_s=0.2) == SpanExportResult.FAILURE
    assert [record.getMessage() for record in caplog.records if record.name == "basis.core.traces"] == [
        "could not export spans to http://127.0.0.1:9/v1/traces (ConnectError): they are dropped, and this is logged once for each kind of failure"
    ]


def test_a_runs_trace_is_its_own_whatever_is_current_and_its_tracer_is_current_within_it() -> None:
    exporter = InMemorySpanExporter()
    given = TracerProvider()
    given.add_span_processor(SimpleSpanProcessor(exporter))
    assert not traces.tracer().start_span("outside").is_recording()
    with given.get_tracer("test").start_as_current_span("outer"):
        with traces.trace("run", service="positions", settings=None, version=None, environment=None, given=given, job="positions") as (_, root):
            assert get_current_span() is root
            with traces.tracer().start_as_current_span("warehouse SELECT"):
                pass
        assert traces.tracer().start_span("after").is_recording() is False
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert (spans["run"].parent, dict(spans["run"].attributes or {})) == (None, {"job": "positions"})
    assert spans["warehouse SELECT"].parent is not None
    assert (spans["warehouse SELECT"].parent.span_id, spans["warehouse SELECT"].context.trace_id) == (spans["run"].context.span_id, spans["run"].context.trace_id)
    assert spans["outer"].context.trace_id != spans["run"].context.trace_id


def _export(endpoint: str, *, timeout_s: float = 5) -> SpanExportResult:
    return _export_with(traces.Otlp(endpoint, timeout_s))


def _export_with(exporter: traces.Otlp) -> SpanExportResult:
    spans = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(spans))
    with provider.get_tracer("test").start_as_current_span("run"):
        pass
    return exporter.export(spans.get_finished_spans())


def test_an_agent_that_is_not_running_costs_a_run_no_wait_at_its_exit() -> None:
    """A refused connection is no Agent at the endpoint, which a wait does not bring up: an export fails at once, and
    every later one of the process's, where waiting and posting again held a run's exit for seconds."""
    exporter = traces.Otlp("http://127.0.0.1:9/v1/traces", 10)
    started = time.monotonic()
    assert [_export_with(exporter) for _ in range(5)] == [SpanExportResult.FAILURE] * 5
    assert time.monotonic() - started < 1
