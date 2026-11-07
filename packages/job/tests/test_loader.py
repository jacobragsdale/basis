import json
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx2
import pandas as pd
import pytest
from builders import DAY, Price, make_job, prices
from opentelemetry.trace import NoOpTracer

import basis.job as etl
from basis.core import fakes
from basis.core.databases import Database, open_database
from basis.core.interval import Interval
from basis.core.opened import Waits
from basis.core.settings import GivenSettings, SettingsError
from basis.core.snapshots import InputRecord
from basis.job.inputs import Dataset
from basis.job.loader import EmptyInputError, Reach, json_rows
from basis.job.loader import load as loading

type Message = MutableMapping[str, Any]


def no_databases(name: str) -> Database:
    return open_database(GivenSettings(), name)


def no_services(name: str) -> tuple[httpx2.AsyncClient, Waits]:
    msg = f"no service {name} here"
    raise LookupError(msg)


def reaching(fake: fakes.Http) -> Reach:
    """Each service a fetch names as `fake`, whose client waits as the fake's waits do."""
    return lambda name: (fake.client(name), fake.waits)


def load(
    datasets: Mapping[str, Dataset], interval: Interval | None, *, open_database: Callable[[str], Database], data: Path | None, reach: Reach = no_services
) -> tuple[dict[str, pd.DataFrame], dict[str, InputRecord]]:
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
    assert frames["prices"]["note"].tolist() == [pd.NA]
    assert frames["prices"].dtypes.equals(prices({}).dtypes)
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
    frames, records = load(Fetched.declared(), DAY, open_database=no_databases, data=tmp_path, reach=reaching(answering(body, seen)))
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


def test_a_csvs_text_is_never_read_as_a_null_or_a_number_unless_its_row_says_so(tmp_path: Path) -> None:
    (tmp_path / "prices.csv").write_text("day,security_id,price,note,booked_at\n2026-11-05,0012,100.5,NA,\n2026-11-05,S2,100.5,null,\n", encoding="utf-8")
    frames, _ = load(make_job(write=etl.Append()).inputs.declared(), DAY, open_database=no_databases, data=tmp_path)
    assert (frames["prices"]["security_id"].tolist(), frames["prices"]["note"].tolist()) == (["0012", "S2"], ["NA", "null"])


def test_a_fetchs_empty_list_fails_unless_its_dataset_allows_it(tmp_path: Path) -> None:
    with pytest.raises(EmptyInputError, match="prices returned no rows"):
        load(Fetched.declared(), DAY, open_database=no_databases, data=tmp_path, reach=reaching(answering([], [])))


def test_a_fixtures_file_stands_in_for_a_fetch(tmp_path: Path) -> None:
    (tmp_path / "prices.csv").write_text("day,security_id,price,note,booked_at\n2026-11-05,S1,100.5,,\n", encoding="utf-8")
    frames, _ = load(Fetched.declared(), DAY, open_database=no_databases, data=tmp_path)
    assert frames["prices"]["price"].tolist() == [Decimal("100.5")]


def test_a_fetch_declares_its_service_and_its_deadline() -> None:
    with pytest.raises(TypeError, match="http 'Pricing': a resource's name"):
        etl.Fetch(Price, http="Pricing", path="/prices", deadline_s=5, empty="fail")
    with pytest.raises(TypeError, match="pricing's deadline is 0s"):
        etl.Fetch(Price, http="pricing", path="/prices", deadline_s=0, empty="fail")


def test_a_fetchs_interval_ends_are_iso_8601_and_percent_encoded_so_an_offsets_plus_reaches_the_service(tmp_path: Path) -> None:
    class Ranged(etl.Inputs):
        prices = etl.Fetch(Price, http="pricing", path="/prices?from={start}&to={end}&on={start:%Y-%m-%d}", deadline_s=5, empty="allow")

    seen: list[Message] = []

    async def app(scope: Message, receive: Callable[[], Awaitable[Message]], send: Callable[[Message], Awaitable[None]]) -> None:
        await receive()
        seen.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b"[]"})

    interval = Interval.parse("2026-11-05T05:30:00+05:30..2026-11-06T05:30:00+05:30")
    load(Ranged.declared(), interval, open_database=no_databases, data=tmp_path, reach=reaching(fakes.Http(app)))
    [scope] = seen
    assert parse_qs(scope["query_string"].decode()) == {"from": ["2026-11-05T00:00:00+00:00"], "to": ["2026-11-06T00:00:00+00:00"], "on": ["2026-11-05"]}


def test_a_fetchs_nan_or_infinity_is_refused_as_no_json_number() -> None:
    with pytest.raises(ValueError, match="dataset prices has NaN, which is no JSON number: send a value its row reads, or null"):
        json_rows(b'[{"day": "2026-11-05", "security_id": "S1", "price": NaN}]', Price, what="dataset prices")
    with pytest.raises(ValueError, match="has -Infinity, which is no JSON number"):
        json_rows(b'[{"day": "2026-11-05", "security_id": "S1", "price": -Infinity}]', Price, what="dataset prices")
