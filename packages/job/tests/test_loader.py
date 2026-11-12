import json
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from datetime import datetime
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
from basis.core.errors import BadReply
from basis.core.interval import Interval
from basis.core.opened import Waits
from basis.core.settings import GivenSettings, SettingsError
from basis.core.snapshots import InputRecord
from basis.job.inputs import Dataset
from basis.job.loader import EmptyInputError, Reach, json_rows
from basis.job.loader import load as loading
from basis.job.rows import SchemaError

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


def test_a_fetch_gets_its_intervals_rows_and_a_number_is_the_nearest_float_to_the_digits_it_was_sent_with(tmp_path: Path) -> None:
    seen: list[str] = []
    body = b'[{"day": "2026-11-05", "security_id": "S1", "price": 100.1234, "booked_at": "2026-11-05T09:30:00"}, {"day": "2026-11-05", "security_id": "S2", "price": 7}]'
    frames, records = load(Fetched.declared(), DAY, open_database=no_databases, data=tmp_path, reach=reaching(answering(body, seen)))
    assert seen == ["/prices/2026-11-05"]
    expected = prices({"price": 100.1234, "booked_at": datetime(2026, 11, 5, 9, 30)}, {"security_id": "S2", "price": 7.0})
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


class Note(etl.Row):
    note: str | None


class Noted(etl.Inputs):
    notes = etl.File(Note, path="notes.csv", empty="fail")


def test_a_blank_line_in_a_csv_of_one_column_is_a_row_whose_value_is_null(tmp_path: Path) -> None:
    (tmp_path / "notes.csv").write_text("note\na\n\nb\n", encoding="utf-8")
    frames, records = load(Noted.declared(), DAY, open_database=no_databases, data=tmp_path)
    assert (frames["notes"]["note"].tolist(), records["notes"].rows) == (["a", pd.NA, "b"], 3)


def test_a_csv_header_that_names_a_column_twice_is_refused_as_named_more_than_once(tmp_path: Path) -> None:
    (tmp_path / "prices.csv").write_text("day,security_id,price,note,booked_at,price\n2026-11-05,S1,100.5,,,100.5\n", encoding="utf-8")
    with pytest.raises(SchemaError, match="columns named more than once: price"):
        load(make_job(write=etl.Append()).inputs.declared(), DAY, open_database=no_databases, data=tmp_path)


def test_a_fetchs_empty_list_fails_unless_its_dataset_allows_it(tmp_path: Path) -> None:
    with pytest.raises(EmptyInputError, match="prices returned no rows"):
        load(Fetched.declared(), DAY, open_database=no_databases, data=tmp_path, reach=reaching(answering([], [])))


def test_a_fixtures_file_stands_in_for_a_fetch(tmp_path: Path) -> None:
    (tmp_path / "prices.csv").write_text("day,security_id,price,note,booked_at\n2026-11-05,S1,100.5,,\n", encoding="utf-8")
    frames, _ = load(Fetched.declared(), DAY, open_database=no_databases, data=tmp_path)
    assert frames["prices"]["price"].tolist() == [100.5]


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


@pytest.mark.parametrize(("zone", "start"), [(None, datetime(2026, 11, 5)), ("America/New_York", datetime(2026, 11, 4, 19))], ids=["utc", "new-york"])
def test_a_query_binds_its_interval_in_the_zone_it_says_its_column_compares_in(tmp_path: Path, zone: str | None, start: datetime) -> None:
    sql = tmp_path / "prices.sql"
    sql.write_text("SELECT * FROM prices WHERE booked_at >= %(interval_start)s AND booked_at < %(interval_end)s", encoding="utf-8")

    class Inputs(etl.Inputs):
        prices = etl.Query(Price, database="source", sql=str(sql), empty="allow", zone=zone)

    bound: list[object] = []
    source = fakes.Database("source")
    source.answers[sql.read_text(encoding="utf-8")] = lambda binds: bound.append(binds["interval_start"]) or prices({})
    load(Inputs.declared(), DAY, open_database=lambda _: source, data=None)
    assert bound == [start]


class Service:
    """Another service that answers each request with the next of `pages`, as JSON, and keeps each request it is sent,
    as its method, path, query and JSON body."""

    def __init__(self, *pages: object) -> None:
        self.pages = list(pages)
        self.asked: list[tuple[str, str, dict[str, list[str]], object]] = []

    async def __call__(self, scope: Message, receive: Callable[[], Awaitable[Message]], send: Callable[[Message], Awaitable[None]]) -> None:
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        self.asked.append((scope["method"], scope["path"], parse_qs(scope["query_string"].decode()), json.loads(body) if body else None))
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": json.dumps(self.pages.pop(0)).encode()})


ROW = {"day": "2026-11-05", "security_id": "S1", "price": 1}


def fetched(fetch: etl.Fetch, service: Service, tmp_path: Path) -> pd.DataFrame:
    class Inputs(etl.Inputs):
        prices = fetch

    frames, _ = load(Inputs.declared(), DAY, open_database=no_databases, data=tmp_path, reach=reaching(fakes.Http(service)))
    return frames["prices"]


def test_a_fetch_may_post_a_body_that_only_reads_its_interval_formatted_into_it(tmp_path: Path) -> None:
    service = Service([ROW])
    fetch = etl.Fetch(Price, http="pricing", path="/prices/search", deadline_s=5, empty="fail", method="POST", body={"desk": "rates", "from": "{start}", "on": "{start:%Y-%m-%d}", "n": 3})
    assert fetched(fetch, service, tmp_path)["security_id"].tolist() == ["S1"]
    assert service.asked == [("POST", "/prices/search", {}, {"desk": "rates", "from": "2026-11-05T00:00:00+00:00", "on": "2026-11-05", "n": 3})]
    assert fetch.describe() == "pricing: POST /prices/search"


def test_a_fetchs_rows_may_be_a_list_under_a_key_of_the_reply(tmp_path: Path) -> None:
    fetch = etl.Fetch(Price, http="pricing", path="/prices", deadline_s=5, empty="fail", rows="result.items")
    assert fetched(fetch, Service({"result": {"items": [ROW, ROW | {"security_id": "S2"}], "total": 2}}), tmp_path)["security_id"].tolist() == ["S1", "S2"]
    with pytest.raises(BadReply, match=r"dataset prices \(pricing: GET /prices\) has no list of rows at result\.items"):
        fetched(fetch, Service({"result": {}}), tmp_path)


def test_a_fetch_follows_the_next_link_each_page_gives_until_there_is_none(tmp_path: Path) -> None:
    service = Service({"data": [ROW], "links": {"next": "/prices?page=2"}}, {"data": [ROW | {"security_id": "S2"}], "links": {"next": "http://pricing/prices?page=3"}}, {"data": [], "links": {}})
    fetch = etl.Fetch(Price, http="pricing", path="/prices", deadline_s=5, empty="fail", rows="data", pages=etl.NextLink("links.next"))
    assert fetched(fetch, service, tmp_path)["security_id"].tolist() == ["S1", "S2"]
    assert [(path, query) for _, path, query, _ in service.asked] == [("/prices", {}), ("/prices", {"page": ["2"]}), ("/prices", {"page": ["3"]})]


def test_a_next_link_to_another_origin_is_refused_so_the_login_goes_nowhere_else(tmp_path: Path) -> None:
    fetch = etl.Fetch(Price, http="pricing", path="/prices", deadline_s=5, empty="fail", rows="data", pages=etl.NextLink("next"))
    with pytest.raises(BadReply, match=r"pricing's next page is at https://elsewhere\.example/steal, which is not pricing's: a fetch follows its own service's links alone"):
        fetched(fetch, Service({"data": [ROW], "next": "https://elsewhere.example/steal"}), tmp_path)


def test_a_fetch_sends_the_cursor_each_page_gives_in_the_query_or_the_body(tmp_path: Path) -> None:
    service = Service({"rows": [ROW], "cursor": "c2"}, {"rows": [ROW], "cursor": None})
    fetch = etl.Fetch(Price, http="pricing", path="/prices?desk=rates", deadline_s=5, empty="fail", rows="rows", pages=etl.Cursor("cursor", param="after"))
    assert len(fetched(fetch, service, tmp_path)) == 2
    assert [query for _, _, query, _ in service.asked] == [{"desk": ["rates"]}, {"desk": ["rates"], "after": ["c2"]}]
    posted = Service({"rows": [ROW], "cursor": "c2"}, {"rows": [ROW]})
    fetch = etl.Fetch(Price, http="pricing", path="/search", deadline_s=5, empty="fail", method="POST", body={"desk": "rates"}, rows="rows", pages=etl.Cursor("cursor", param="after"))
    fetched(fetch, posted, tmp_path)
    assert [body for _, _, _, body in posted.asked] == [{"desk": "rates"}, {"desk": "rates", "after": "c2"}]


def test_a_fetch_pages_by_offset_until_a_page_is_short(tmp_path: Path) -> None:
    service = Service([ROW, ROW], [ROW, ROW], [ROW])
    fetch = etl.Fetch(Price, http="pricing", path="/prices", deadline_s=5, empty="fail", pages=etl.Offset(param="offset", size=2, size_param="limit"))
    assert len(fetched(fetch, service, tmp_path)) == 5
    assert [query for _, _, query, _ in service.asked] == [{"offset": [str(n)], "limit": ["2"]} for n in (0, 2, 4)]
    posted = Service([ROW, ROW], [])
    fetch = etl.Fetch(Price, http="pricing", path="/search", deadline_s=5, empty="fail", method="POST", pages=etl.Offset(param="skip", size=2))
    fetched(fetch, posted, tmp_path)
    assert [body for _, _, _, body in posted.asked] == [{"skip": 0}, {"skip": 2}]  # a POST's offset is a number in its body


def test_a_fetch_that_pages_past_its_most_pages_fails_rather_than_loop(tmp_path: Path) -> None:
    fetch = etl.Fetch(Price, http="pricing", path="/prices", deadline_s=5, empty="fail", rows="data", pages=etl.Cursor("next", param="c", max_pages=2))
    with pytest.raises(BadReply, match="pricing still names a next page after 2 pages of dataset prices: raise its max_pages, or narrow the interval"):
        fetched(fetch, Service({"data": [ROW], "next": "a"}, {"data": [ROW], "next": "b"}), tmp_path)


@pytest.mark.parametrize(
    ("declared", "message"),
    [
        (lambda: {"method": "PUT"}, "a fetch is a GET, or a POST that only reads, not PUT"),
        (lambda: {"body": {"a": 1}}, "a GET sends no body: declare method='POST'"),
        (lambda: {"method": "POST", "body": {"a": object()}}, "its body is JSON"),
        (lambda: {"pages": etl.Offset(param="offset", size=0)}, "an offset's page size is 1 or more"),
        (lambda: {"pages": etl.Cursor("next", param="c", max_pages=0)}, "max_pages is 1 or more"),
    ],
)
def test_a_fetchs_declaration_is_checked_where_it_is_made(declared: Callable[[], dict[str, Any]], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        etl.Fetch(Price, http="pricing", path="/prices", deadline_s=5, empty="fail", **declared())
