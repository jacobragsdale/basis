"""A run against real databases is one trace: each statement its read and its publish make, and each commit, is a
span under the run's root, and every record the run logs carries the trace's ids."""

import io
import json
import logging
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from builders import Price
from integration.scratch import Scratch
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import basis.job as etl
from basis.core.interval import Interval
from basis.core.logs import JsonFormatter
from basis.core.settings import GivenSettings
from basis.core.storage import Root
from basis.job import ddl, runs
from basis.job.job import Job
from basis.job.outputs import Target

pytestmark = pytest.mark.integration


@pytest.fixture
def source(scratch: Scratch) -> Iterator[Scratch]:
    scratch.drop("etl_it_source", f"prices_{scratch.suffix}")
    scratch.execute(ddl.create_table(Target(Price, "source", "etl_it_source", etl.Append()), scratch.dialect))
    scratch.execute(ddl.create_table(Target(Price, "warehouse", f"prices_{scratch.suffix}", etl.Append()), scratch.dialect))
    connection = scratch.connect()
    try:
        placeholders = ", ".join("?" * 5) if scratch.dialect == "mssql" else ", ".join(f":{index}" for index in range(1, 6))
        connection.cursor().execute(f"INSERT INTO etl_it_source (day, security_id, price, note, booked_at) VALUES ({placeholders})", [date(2026, 11, 5), "S1", Decimal("100.5"), None, None])
        connection.commit()
    finally:
        connection.close()
    yield scratch
    scratch.drop("etl_it_source", f"prices_{scratch.suffix}")


def copying(dialect: str, table: str) -> Job:
    class Inputs(etl.Inputs):
        prices = etl.Query(Price, database="source", sql=f"sql/{dialect}_prices.sql", empty="fail")

    class Outputs(etl.Outputs):
        prices = etl.Table(Price, database="warehouse", table=table, write=etl.Append())

    def transform(inputs: Inputs, _params: etl.Params) -> Outputs:
        return Outputs(prices=inputs.prices)

    return Job("copying", "tests.copying", etl.Params, Inputs, Outputs, transform, None, Path(__file__).parent)


def test_a_published_run_is_one_trace_with_a_span_for_each_statement_and_its_records_carry_its_ids(source: Scratch, tmp_path: Path) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    framework = logging.getLogger("basis")
    level = framework.level
    framework.addHandler(handler)
    framework.setLevel(logging.INFO)
    params = tmp_path / "params.json"
    params.write_text("{}", encoding="utf-8")
    isolation = {"isolation": "read_committed"} if source.dialect == "oracle" else {}  # Oracle refuses a read-only read of a table made seconds before
    settings = GivenSettings(databases={"source": source.settings | isolation, "warehouse": source.settings})
    try:
        result = runs.run(
            copying(source.dialect, f"prices_{source.suffix}"), settings, runs.Request(params, Interval.parse("2026-11-05..2026-11-06"), publish=True),
            snapshot_root=Root.at(tmp_path / "snapshots"), provider=provider,
        )  # fmt: skip
    finally:
        framework.removeHandler(handler)
        framework.setLevel(level)
    assert result.record.status == "published", result.record
    spans = exporter.get_finished_spans()
    [root] = [span for span in spans if span.parent is None]
    assert {span.context.trace_id for span in spans} == {root.context.trace_id}
    names = [span.name for span in spans]
    assert {"source SELECT", "warehouse INSERT", "warehouse commit"} <= set(names), names
    query = (Path(__file__).parent / "sql" / f"{source.dialect}_prices.sql").read_text(encoding="utf-8")
    [read] = [span for span in spans if (span.attributes or {}).get("db.query.text") == query]
    assert (read.name, dict(read.attributes or {})["db.response.returned_rows"]) == ("source SELECT", 1)
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    ours = [record for record in records if record.get("run_id") == result.record.run_id]
    assert ours
    assert {record["trace_id"] for record in ours} == {f"{root.context.trace_id:032x}"}
