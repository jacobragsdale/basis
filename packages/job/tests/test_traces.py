"""A job's run as one trace: a span for each SQL statement and each GET under its root, its records carrying the
trace's ids and the job's name, and its clocks' and ids' values kept for its replay, which is a trace of its own."""

import io
import json
import logging
from collections.abc import Awaitable, Callable, Generator, Iterator, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Never
from uuid import UUID

import pyarrow as pa
import pytest
from builders import DAY, Price, params_file
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import basis.job as etl
from basis.core import fakes, logs
from basis.core.databases import Database
from basis.core.databases.mssql import MssqlReader
from basis.core.errors import KINDS, BadReply, ServiceError, Unavailable
from basis.core.logs import JsonFormatter
from basis.core.settings import GivenSettings
from basis.core.storage import Root
from basis.job import runs
from basis.job.job import Job, JobError

type Message = MutableMapping[str, Any]
type Records = Callable[[], list[dict[str, object]]]


@pytest.fixture
def logged() -> Iterator[Records]:
    """The records basis logs during the test, each as the JSON line basis-core writes it, at INFO."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    framework = logging.getLogger("basis")
    level = framework.level
    framework.addHandler(handler)
    framework.setLevel(logging.INFO)
    try:
        yield lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
    finally:
        framework.removeHandler(handler)
        framework.setLevel(level)


class Stamped(etl.Row):
    day: date
    security_id: Annotated[str, etl.MaxLength(8)]
    price: float
    venue: Annotated[str, etl.MaxLength(8)]
    stamped_at: Annotated[datetime, etl.Zone("UTC")]
    price_id: Annotated[str, etl.MaxLength(36)]


class Venue(etl.Row):
    security_id: Annotated[str, etl.MaxLength(8)]
    venue: Annotated[str, etl.MaxLength(8)]


def stamping(sql: Path, *, more_ids: int = 0) -> Job:
    """A job that reads prices from a database and venues from another service, and stamps each row with the time and
    a new id; `more_ids` asks for that many ids besides, as a job changed since its run may."""

    class Inputs(etl.Inputs):
        prices = etl.Query(Price, database="source", sql=str(sql), empty="fail")
        venues = etl.Fetch(Venue, http="venues", path="/venues/{start:%Y-%m-%d}", deadline_s=5, empty="fail")
        clock = etl.Clock()
        ids = etl.Ids()

    class Outputs(etl.Outputs):
        stamped = etl.Table(Stamped, database="warehouse", table="stamped", write=etl.Append())

    def transform(inputs: Inputs, _params: etl.Params) -> Outputs:
        joined = inputs.prices.merge(inputs.venues, on="security_id", how="inner", validate="1:1").sort_values("security_id", ignore_index=True)
        made = inputs.ids.new(len(joined))
        inputs.ids.new(more_ids)
        stamped = joined[["day", "security_id", "price", "venue"]].assign(stamped_at=inputs.clock.now(), price_id=[str(made_id) for made_id in made])
        return Outputs(stamped=stamped)

    return Job("stamping", "tests.stamping", etl.Params, Inputs, Outputs, transform, None, Path(__file__).parent)


class _Cursor:
    def __init__(self, executed: list[str]) -> None:
        self.executed = executed

    def execute(self, sql: str, binds: object = None) -> None:
        del binds
        self.executed.append(sql)

    def arrow_reader(self, *, batch_size: int) -> pa.RecordBatchReader:
        del batch_size
        batch = pa.record_batch({
            "day": pa.array([date(2026, 11, 5)] * 2), "security_id": ["S1", "S2"], "price": pa.array([Decimal("100.5"), Decimal(99)], pa.decimal128(18, 4)),
            "note": pa.array([None, None], pa.string()), "booked_at": pa.array([None, None], pa.timestamp("us")),
        })  # fmt: skip
        return pa.RecordBatchReader.from_batches(batch.schema, [batch])

    def close(self) -> None:
        pass


class _Connection:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self.executed)


@dataclass(frozen=True)
class _Driverless:
    """basis-core's SQL Server reader, on a connection that answers every query with the same two prices."""

    name: str

    @property
    def writable(self) -> bool:
        return False

    @contextmanager
    def reading(self) -> Generator[MssqlReader]:
        yield MssqlReader(self.name, _Connection(), 100)  # ty: ignore[invalid-argument-type] -- a connection without a driver

    def writing(self) -> Never:
        raise NotImplementedError

    def statements(self, *, writes: bool) -> Never:
        del writes
        raise NotImplementedError


def _databases(name: str) -> Database:
    return _Driverless(name)


def venues(*, status: int = 200) -> fakes.Http:
    async def app(scope: Message, receive: Callable[[], Awaitable[Message]], send: Callable[[Message], Awaitable[None]]) -> None:
        del scope
        await receive()
        body = json.dumps([{"security_id": "S1", "venue": "XNYS"}, {"security_id": "S2", "venue": "XLON"}]).encode()
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})

    return fakes.Http(app)


def traced() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def run(tmp_path: Path, job: Job, provider: TracerProvider, **faked: fakes.Fake) -> runs.Result:
    request = runs.Request(params_file(tmp_path), DAY)
    return runs.run(job, GivenSettings(), request, snapshot_root=Root.at(tmp_path / "snapshots"), databases=_databases, faked=faked, provider=provider)


@pytest.fixture
def sql(tmp_path: Path) -> Path:
    path = tmp_path / "prices.sql"
    path.write_text("SELECT day, security_id, price, note, booked_at FROM prices WHERE day >= %(interval_start)s", encoding="utf-8")
    return path


def test_a_run_is_one_trace_with_a_span_for_each_statement_and_get_and_its_records_carry_its_ids(tmp_path: Path, sql: Path, logged: Records) -> None:
    provider, exporter = traced()
    result = run(tmp_path, stamping(sql), provider, venues=venues())
    assert result.record.status == "checked", result.record
    spans = exporter.get_finished_spans()
    [root] = [span for span in spans if span.parent is None]
    assert (root.name, dict(root.attributes or {})) == ("run", {"basis.job": "stamping", "basis.run_id": result.record.run_id})
    assert {span.context.trace_id for span in spans} == {root.context.trace_id}
    assert sorted(span.name for span in spans if span is not root) == ["clock now", "ids new", "ids new", "source SELECT", "venues GET"]
    statement = next(span for span in spans if span.name == "source SELECT")
    assert dict(statement.attributes or {})["db.query.text"] == sql.read_text(encoding="utf-8")
    trace_id = f"{root.context.trace_id:032x}"
    records = [record for record in logged() if record.get("run_id") == result.record.run_id]
    assert records
    assert {(record["trace_id"], record["service"]) for record in records} == {(trace_id, "stamping")}
    assert any(record["message"] == "run summary" and record["span_id"] == f"{root.context.span_id:016x}" for record in records)


def test_a_run_keeps_its_clocks_times_and_its_ids_and_a_replay_is_given_them_again(tmp_path: Path, sql: Path) -> None:
    provider, _ = traced()
    result = run(tmp_path, stamping(sql), provider, venues=venues())
    assert result.snapshot is not None
    stamped = result.planned["stamped"].changes
    assert stamped is not None
    assert stamped.insert is not None
    kept = result.snapshot.record()
    assert kept.clocks == {"clock": stamped.insert["stamped_at"].unique().tolist()}
    assert kept.ids == {"ids": [UUID(found) for found in stamped.insert["price_id"].tolist()]}
    replaying, spans = traced()
    replayed, differences = runs.replay(stamping(sql), result.snapshot, provider=replaying)
    assert (replayed.record.status, differences, replayed.record.clocks, replayed.record.ids) == ("checked", [], kept.clocks, kept.ids)
    [root] = [span for span in spans.get_finished_spans() if span.parent is None]
    assert (root.name, "venues GET" in {span.name for span in spans.get_finished_spans()}) == ("replay", False)  # a replay reaches no service


def test_a_replay_that_asks_for_more_than_its_run_kept_is_given_new_ones_and_says_so(tmp_path: Path, sql: Path, logged: Records) -> None:
    provider, _ = traced()
    result = run(tmp_path, stamping(sql), provider, venues=venues())
    assert result.snapshot is not None
    replayed, differences = runs.replay(stamping(sql, more_ids=2), result.snapshot, provider=traced()[0])
    assert (replayed.record.status, differences) == ("checked", [])
    assert replayed.record.ids["ids"][:2] == result.record.ids["ids"]
    assert len(set(replayed.record.ids["ids"])) == 4
    said = [record["message"] for record in logged() if str(record["message"]).startswith("the replay asks")]
    assert said == [f"the replay asks ids ids for more than run {result.record.run_id} kept: the rest are a replay's own"]


def test_a_fixtures_clock_and_ids_are_fakes(tmp_path: Path, sql: Path) -> None:
    at = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
    result = run(tmp_path, stamping(sql), traced()[0], venues=venues(), clock=fakes.Clock(at=at), ids=fakes.Ids())
    assert (result.record.clocks, result.record.ids) == ({"clock": [at]}, {"ids": [UUID("00000000-0000-7000-8000-000000000001"), UUID("00000000-0000-7000-8000-000000000002")]})


@pytest.mark.parametrize(
    ("faked", "message"),
    [
        ({"pricing": venues()}, "a fake is given for pricing, which stamping neither fetches from, declares nor sends to"),
        ({"clock": fakes.Ids()}, "clock is given a fakes.Ids, and stamping takes a fakes.Clock for it"),
    ],
    ids=["undeclared", "wrong-kind"],
)
def test_a_fake_fakes_what_the_job_declares_of_its_kind(tmp_path: Path, sql: Path, faked: dict[str, fakes.Fake], message: str) -> None:
    with pytest.raises(JobError, match=message):
        run(tmp_path, stamping(sql), traced()[0], **faked)


def test_a_service_the_settings_do_not_configure_fails_the_run_naming_its_variable(tmp_path: Path, sql: Path) -> None:
    result = run(tmp_path, stamping(sql), traced()[0])
    assert result.record.status == "failed"
    assert result.record.error is not None
    assert "set BASIS_HTTP__VENUES__URL" in result.record.error


@pytest.mark.parametrize("kind", [kind for kind in KINDS if kind is not BadReply], ids=lambda kind: kind.__name__)
def test_a_service_that_fails_its_get_fails_the_run_naming_the_kind(tmp_path: Path, sql: Path, kind: type[ServiceError]) -> None:
    """An Unavailable GET is made again until no backoff fits its deadline, 5s: at once, on the fake's waits."""
    fake = venues()
    fake.fails("/venues/2026-11-05", kind)
    result = run(tmp_path, stamping(sql), traced()[0], venues=fake)
    assert (result.record.status, result.record.error) == ("failed", f"{kind.__name__}: venues answered {fakes.ANSWERS[kind]} to GET /venues/2026-11-05")
    assert len(fake.waits.waited) == (8 if kind is Unavailable else 0)


def test_a_get_unavailable_once_is_made_again_and_the_run_goes_on(tmp_path: Path, sql: Path) -> None:
    fake = venues()
    fake.fails("/venues/2026-11-05", Unavailable, times=1)
    result = run(tmp_path, stamping(sql), traced()[0], venues=fake)
    assert (result.record.status, result.record.error) == ("checked", None)


def test_a_reply_that_is_not_the_fetchs_rows_fails_the_run_as_a_bad_reply(tmp_path: Path, sql: Path) -> None:
    fake = venues()
    fake.fails("/venues/2026-11-05", BadReply)
    result = run(tmp_path, stamping(sql), traced()[0], venues=fake)
    assert result.record.status == "failed"
    assert result.record.error is not None
    assert result.record.error.startswith("BadReply: venues answered GET /venues/2026-11-05 with a body that is not rows of Venue: Expecting value")


def test_a_runs_spans_carry_the_settings_commit_as_their_version(tmp_path: Path, sql: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = GivenSettings(git_sha="abc123", traces={"exporter": "console"})
    request = runs.Request(params_file(tmp_path), DAY)  # a run from Python, whose request names no commit
    runs.run(stamping(sql), settings, request, snapshot_root=Root.at(tmp_path / "snapshots"), databases=_databases, faked={"venues": venues()})
    err = capsys.readouterr().err
    assert '"service.version": "abc123"' in err
    assert '"service.name": "stamping"' in err


def test_a_replays_records_carry_its_own_fields_whatever_an_earlier_run_bound(tmp_path: Path, sql: Path, logged: Records, monkeypatch: pytest.MonkeyPatch) -> None:
    result = run(tmp_path, stamping(sql), traced()[0], venues=venues())
    assert result.snapshot is not None
    monkeypatch.setattr(logs, "_bound", {})  # the process's fields, which this test binds, as they were after it
    logs.bind(run_id="an-earlier-run", job="another-job", service="another-job")
    replayed, _ = runs.replay(stamping(sql), result.snapshot, provider=traced()[0])
    ours = [record for record in logged() if record.get("replay_of") == result.record.run_id]
    assert ours
    assert {(record["run_id"], record["job"], record["service"]) for record in ours} == {(replayed.record.run_id, "stamping", "stamping")}


def test_a_run_that_follows_a_replay_in_the_same_process_logs_none_of_the_replays_fields(tmp_path: Path, sql: Path, logged: Records) -> None:
    first = run(tmp_path, stamping(sql), traced()[0], venues=venues())
    assert first.snapshot is not None
    replayed, _ = runs.replay(stamping(sql), first.snapshot, provider=traced()[0])
    after = run(tmp_path, stamping(sql), traced()[0], venues=venues())
    records = logged()
    assert {record.get("replay_of") for record in records if record.get("run_id") == replayed.record.run_id} == {first.record.run_id}
    ours = [record for record in records if record.get("run_id") == after.record.run_id]
    assert ours
    assert all("replay_of" not in record for record in ours)
