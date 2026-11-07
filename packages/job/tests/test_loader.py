import json
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import polars as pl
import pytest
from builders import DAY, Price, make_job, prices
from opentelemetry.trace import NoOpTracer

import basis.job as etl
from basis.core import fakes
from basis.core.databases import Database, open_database
from basis.core.interval import Interval
from basis.core.settings import GivenSettings, SettingsError
from basis.core.snapshots import InputRecord
from basis.job.inputs import Dataset
from basis.job.loader import EmptyInputError, json_rows
from basis.job.loader import load as loading

type Message = MutableMapping[str, Any]


def no_databases(name: str) -> Database:
    return open_database(GivenSettings(), name)


def no_services(name: str) -> httpx2.AsyncClient:
    msg = f"no service {name} here"
    raise LookupError(msg)


def load(
    datasets: Mapping[str, Dataset], interval: Interval | None, *, open_database: Callable[[str], Database], data: Path | None, reach: Callable[[str], httpx2.AsyncClient] = no_services
) -> tuple[dict[str, pl.DataFrame], dict[str, InputRecord]]:
    return loading(datasets, interval, open_database=open_database, reach=reach, tracer=NoOpTracer(), data=data)


def answering(body: object, seen: list[str]) -> fakes.Http:
    """Another service, which answers every GET with `body` as JSON, and keeps each path it is asked for."""

    async def app(scope: Message, receive: Callable[[], Awaitable[Message]], send: Callable[[Message], Awaitable[None]]) -> None:
        await receive()
        seen.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": json.dumps(body).encode() if not isinstance(body, bytes) else body})

    return fakes.Http(app)


def test_a_csv_reads_as_text_its_row_converts_and_an_empty_field_is_null(tmp_path: Path) -> None:
    (tmp_path / "prices.csv").write_text("day,security_id,price,note,booked_at\n2026-11-05,S1,100.5,,\n", encoding="utf-8")
    frames, records = load(make_job(write=etl.Append()).inputs.declared(), DAY, open_database=no_databases, data=tmp_path)
    assert frames["prices"].row(0, named=True)["note"] is None
    assert frames["prices"].schema == prices({}).schema
    assert records["prices"].source == f"data: {tmp_path / 'prices.csv'}"


def test_an_empty_input_fails_unless_its_dataset_allows_it(tmp_path: Path) -> None:
    (tmp_path / "prices.csv").write_text("day,security_id,price,note,booked_at\n", encoding="utf-8")
    with pytest.raises(EmptyInputError, match="prices returned no rows"):
        load(make_job(write=etl.Append()).inputs.declared(), DAY, open_database=no_databases, data=tmp_path)


def test_a_dataset_with_no_file_reads_its_source_and_says_what_to_configure(tmp_path: Path) -> None:
    class Inputs(etl.Inputs):
        prices = etl.Query(Price, database="markets", sql="prices.sql", empty="allow")

    with pytest.raises(SettingsError, match="BASIS_DATABASES__MARKETS__KIND"):
        load(Inputs.declared(), DAY, open_database=no_databases, data=tmp_path)


class Fetched(etl.Inputs):
    prices = etl.Fetch(Price, http="pricing", path="/prices/{start:%Y-%m-%d}", deadline_s=5, empty="fail")


def test_a_fetch_gets_its_intervals_rows_and_a_number_keeps_the_digits_it_was_sent_with(tmp_path: Path) -> None:
    seen: list[str] = []
    body = b'[{"day": "2026-11-05", "security_id": "S1", "price": 100.1234, "booked_at": "2026-11-05T09:30:00"}, {"day": "2026-11-05", "security_id": "S2", "price": 7}]'
    frames, records = load(Fetched.declared(), DAY, open_database=no_databases, data=tmp_path, reach=answering(body, seen).client)
    assert seen == ["/prices/2026-11-05"]
    expected = prices({"price": Decimal("100.1234"), "booked_at": datetime(2026, 11, 5, 9, 30)}, {"security_id": "S2", "price": Decimal(7)})
    assert frames["prices"].equals(expected)
    assert (records["prices"].source, records["prices"].rows) == ("pricing: GET /prices/{start:%Y-%m-%d}", 2)


@pytest.mark.parametrize(
    ("body", "error", "message"),
    [
        (b'{"day": "2026-11-05"}', TypeError, "is not a JSON list of objects, one per row"),
        (b'[["2026-11-05"]]', TypeError, "is not a JSON list of objects, one per row"),
        (b'[{"day": "2026-11-05", "security_id": "S1", "price": 1, "venue": "X"}]', ValueError, "has fields its row has no column for: venue"),
        (b'[{"day": "2026-11-05", "security_id": "S1", "price": {"amount": 1}}]', TypeError, "has an object or a list in price"),
    ],
    ids=["an-object", "a-list-of-lists", "an-unknown-field", "a-nested-value"],
)
def test_a_fetchs_rows_are_a_list_of_flat_objects_of_its_rows_columns(body: bytes, error: type[Exception], message: str) -> None:
    with pytest.raises(error, match=message):
        json_rows(body, Price, what="dataset prices")


def test_a_fetchs_empty_list_fails_unless_its_dataset_allows_it(tmp_path: Path) -> None:
    with pytest.raises(EmptyInputError, match="prices returned no rows"):
        load(Fetched.declared(), DAY, open_database=no_databases, data=tmp_path, reach=answering([], []).client)


def test_a_fixtures_file_stands_in_for_a_fetch(tmp_path: Path) -> None:
    (tmp_path / "prices.csv").write_text("day,security_id,price,note,booked_at\n2026-11-05,S1,100.5,,\n", encoding="utf-8")
    frames, _ = load(Fetched.declared(), DAY, open_database=no_databases, data=tmp_path)
    assert frames["prices"].get_column("price").to_list() == [Decimal("100.5")]


def test_a_fetch_declares_its_service_and_its_deadline() -> None:
    with pytest.raises(TypeError, match="http 'Pricing': a resource's name"):
        etl.Fetch(Price, http="Pricing", path="/prices", deadline_s=5, empty="fail")
    with pytest.raises(TypeError, match="pricing's deadline is 0s"):
        etl.Fetch(Price, http="pricing", path="/prices", deadline_s=0, empty="fail")
