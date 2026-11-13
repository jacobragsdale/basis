"""A dataset route that answers `api.Table[Row]`: a pandas frame or an Arrow table, checked once against the row's
columns, answered as Arrow or as JSON written from it (step 2.12), and `Reader.fetch_table`, a query's Arrow checked the
same way."""

import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from http import HTTPStatus
from typing import Annotated, Literal
from uuid import UUID

import pandas as pd
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from pydantic import AwareDatetime, BaseModel, Field

from basis import api
from basis.api import arrow, fakes

ARROW = "application/vnd.apache.arrow.stream"
AT = datetime(2026, 11, 6, 14, 30, tzinfo=UTC)
WAREHOUSE = api.database("warehouse", deadline_s=5, max_concurrent=2)
POSITIONS = "SELECT book, quantity, as_of, at, note FROM positions"
INSERT = "INSERT INTO marks (at) VALUES (%(at)s)"


class Book(StrEnum):
    RATES = "rates"
    CREDIT = "credit"


class Position(BaseModel):
    book: str
    quantity: float
    as_of: date
    at: AwareDatetime
    note: str | None = None


def a_frame() -> pd.DataFrame:
    return pd.DataFrame({"book": ["RATES", "CREDIT"], "quantity": [1.5, float("nan")], "as_of": [date(2026, 11, 6)] * 2, "at": [AT, AT], "note": [None, "watch"]})


def from_a_frame() -> api.Table[Position]:
    return a_frame()


def from_arrow() -> api.Table[Position]:
    return pa.Table.from_pandas(a_frame(), preserve_index=False)


async def from_the_warehouse(warehouse: Annotated[api.Reader, api.given(WAREHOUSE)]) -> api.Table[Position]:
    return await warehouse.fetch_table(Position, POSITIONS)


async def marked_then_wrong(warehouse: Annotated[api.Writer, api.given(WAREHOUSE)]) -> api.Table[Position]:
    await warehouse.execute(INSERT, at=AT)
    return a_frame().drop(columns=["book"])


def service(*handlers: Callable[..., object]) -> api.Service:
    routes = [
        api.post(f"/{getattr(handler, '__name__', '')}", handler, deadline_s=5, response=api.Table[Position], errors=[], anonymous=True)
        if handler is marked_then_wrong
        else api.get(f"/{getattr(handler, '__name__', '')}", handler, deadline_s=5, response=api.Table[Position], errors=[])  # ty: ignore[invalid-argument-type] -- each a named function
        for handler in handlers
    ]
    return api.Service(name="positions", settings=api.Settings, resources=[WAREHOUSE], routes=routes)


ROWS = [{"book": "RATES", "quantity": Decimal("1.50"), "as_of": date(2026, 11, 6), "at": AT, "note": None}, {"book": "CREDIT", "quantity": None, "as_of": date(2026, 11, 6), "at": AT, "note": "watch"}]
"""The warehouse's rows, as a driver gives them: a decimal for the quantity, which a float reads through its text."""


@pytest.fixture
def warehouse() -> fakes.Database:
    return fakes.Database(answers={POSITIONS: ROWS, INSERT: 1})


@pytest.fixture
def client(warehouse: fakes.Database) -> TestClient:
    app = api.build(service(from_a_frame, from_arrow, from_the_warehouse, marked_then_wrong), settings=api.Settings.given(), fakes={WAREHOUSE: warehouse})
    return TestClient(app.asgi, raise_server_exceptions=False)


EXPECTED = [
    {"book": "RATES", "quantity": 1.5, "as_of": "2026-11-06", "at": "2026-11-06T14:30:00Z", "note": None},
    {"book": "CREDIT", "quantity": None, "as_of": "2026-11-06", "at": "2026-11-06T14:30:00Z", "note": "watch"},
]


@pytest.mark.parametrize("path", ["/from_a_frame", "/from_arrow", "/from_the_warehouse"], ids=["a-frame", "an-arrow-table", "fetch-table"])
def test_a_table_answers_json_and_arrow_with_the_same_rows(client: TestClient, path: str) -> None:
    with client:
        as_json = client.get(path)
        as_arrow = client.get(path, headers={"accept": ARROW})
    assert (as_json.status_code, as_json.headers["content-type"], as_json.json()) == (HTTPStatus.OK, "application/json", EXPECTED)
    table = pa.ipc.open_stream(as_arrow.content).read_all()
    assert (as_arrow.headers["content-type"], table.column_names) == (ARROW, ["book", "quantity", "as_of", "at", "note"])
    assert table.schema.field("quantity").type == pa.float64()
    assert table.column("quantity").to_pylist() == [1.5, None]


def many(count: int) -> pa.Table:
    return pa.table({"book": ["RATES"] * count, "quantity": [float(row) for row in range(count)], "as_of": [date(2026, 11, 6)] * count, "at": [AT] * count, "note": pa.nulls(count, pa.string())})


def test_a_tables_stream_holds_its_rows_in_order_in_batches_of_at_most_65536() -> None:
    """Issue 37: a table of one chunk went out as one batch, which a grid can show only once all of it has arrived. A
    chunk that already holds fewer rows is a batch as it is, and a table of no rows is its schema alone."""
    count = arrow.BATCH_ROWS + 1
    whole = arrow.write(many(count))
    assert [batch.num_rows for batch in pa.ipc.open_stream(whole)] == [arrow.BATCH_ROWS, 1]
    assert pa.ipc.open_stream(whole).read_all().column("quantity").to_pylist() == [float(row) for row in range(count)]
    chunked = pa.concat_tables([many(3), many(count)])
    assert [batch.num_rows for batch in pa.ipc.open_stream(arrow.write(chunked))] == [3, arrow.BATCH_ROWS, 1]
    assert list(pa.ipc.open_stream(arrow.write(many(0)))) == []


def test_a_table_route_answers_its_stream_in_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(arrow, "BATCH_ROWS", 2)

    def five() -> api.Table[Position]:
        return many(5)

    app = api.build(service(five), settings=api.Settings.given(), fakes={WAREHOUSE: fakes.Database(answers={})})
    with TestClient(app.asgi) as client:
        answered = client.get("/five", headers={"accept": ARROW}).content
    assert [batch.column("quantity").to_pylist() for batch in pa.ipc.open_stream(answered)] == [[0.0, 1.0], [2.0, 3.0], [4.0]]


def test_a_table_that_lacks_a_column_is_a_defect_and_its_writes_roll_back(client: TestClient, warehouse: fakes.Database) -> None:
    with client:
        response = client.post("/marked_then_wrong")
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert (warehouse.committed, len(warehouse.rolled_back)) == ([], 1)


class Bounded(BaseModel):
    quantity: Annotated[float, Field(gt=0)]


def bounded() -> api.Table[Bounded]:
    return pd.DataFrame({"quantity": [1.0]})


def test_a_table_row_with_a_value_constraint_is_refused_when_its_route_is_declared() -> None:
    with pytest.raises(api.DeclarationError, match=r"an api\.Table is checked by its columns' types, never row by row"):
        api.get("/bounded", bounded, deadline_s=1, response=api.Table[Bounded], errors=[])


def test_the_document_gives_a_tables_json_as_its_rows_and_lists_arrow_beside_it() -> None:
    from basis.api import openapi  # noqa: PLC0415 -- the document, made without settings

    document = openapi.document(service(from_a_frame))
    content = openapi.mapping(
        openapi.mapping(openapi.mapping(openapi.mapping(openapi.mapping(openapi.mapping(document.get("paths")).get("/from_a_frame")).get("get")).get("responses")).get("200")).get("content")
    )
    assert openapi.mapping(content["application/json"])["schema"] == {"type": "array", "items": {"$ref": "#/components/schemas/Position"}, "title": "Response From A Frame"}
    assert openapi.mapping(content[ARROW]) == {"schema": {"type": "string", "format": "binary"}}


@pytest.mark.parametrize(
    ("changed", "message"),
    [
        ({"quantity": ["one", "two"]}, "column quantity is (large_)?string, and the row declares a float"),
        ({"at": [datetime(2026, 11, 6, 14, 30)] * 2}, "column at holds times without their zone"),
        ({"book": ["RATES", None]}, "column book holds 1 nulls, and the row's field may not be None"),
        ({"desk": ["a", "b"]}, "has columns Position does not declare: desk"),
    ],
    ids=["a-number-as-text", "a-naive-time", "a-null-where-none-may-be", "an-extra-column"],
)
def test_a_table_unlike_its_rows_is_refused_naming_the_column(changed: dict[str, list[object]], message: str) -> None:
    from basis.api import arrow, dataset  # noqa: PLC0415 -- the checker itself

    found = a_frame().assign(**changed)  # ty: ignore[invalid-argument-type] -- each a column's values
    with pytest.raises(arrow.TableError, match=message):
        arrow.conform(dataset.rows(list[Position], "t", table=True), found, "the table t returned")


class Holding(BaseModel):
    side: Literal["buy", "sell"]
    book: Book
    trade_id: UUID
    hedged: bool


def _holding(**changed: list[object]) -> pa.Table:
    columns: dict[str, list[object]] = {"side": ["buy", "sell"], "book": ["rates", "credit"], "trade_id": [str(UUID(int=1)), str(UUID(int=0xABC)).upper()], "hedged": [True, False]}
    return pa.table(columns | changed)


@pytest.mark.parametrize(
    ("changed", "message"),
    [
        ({"side": ["buy", "hold"]}, "column side holds 'hold', which is not one of its values: buy, sell"),
        ({"book": ["rates", "fx"]}, "column book holds 'fx', which is not one of its values: credit, rates"),
        ({"trade_id": ["not-a-uuid", str(UUID(int=3))]}, "column trade_id holds 'not-a-uuid', which is not a UUID"),
        ({"hedged": [7, 0]}, "column hedged holds 7, and a bool column takes true and false, or 1 and 0"),
    ],
    ids=["a-literal-of-another-value", "an-enum-of-another-value", "a-uuid-of-any-text", "a-bool-of-seven"],
)
def test_a_tables_text_and_bool_columns_hold_only_their_values(changed: dict[str, list[object]], message: str) -> None:
    from basis.api import arrow, dataset  # noqa: PLC0415 -- the checker itself

    with pytest.raises(arrow.TableError, match=re.escape(message)):
        arrow.conform(dataset.rows(list[Holding], "t", table=True), _holding(**changed), "the table t returned")


def test_a_tables_uuids_are_given_as_json_gives_them_and_its_ones_and_zeros_as_bools() -> None:
    from basis.api import arrow, dataset  # noqa: PLC0415 -- the checker itself

    found = arrow.conform(dataset.rows(list[Holding], "t", table=True), _holding(hedged=[1, 0]), "the table t returned")
    assert found.column("trade_id").to_pylist() == [str(UUID(int=1)), str(UUID(int=0xABC))]
    assert found.column("hedged").to_pylist() == [True, False]


def test_a_route_is_called_and_a_service_served_on_its_fakes_from_python_too() -> None:
    """For a caller in Python, such as ctzd's own commands, with settings given in code and no BASIS_* variable."""
    routes = [api.get("/from_a_frame", from_a_frame, deadline_s=5, response=api.Table[Position], errors=[])]
    called = api.call_route(api.Service(name="positions", settings=api.Settings, routes=routes), "from_a_frame", {}, settings=api.Settings.given())
    assert (called.status, called.body) == (HTTPStatus.OK, EXPECTED)
    assert callable(api.serve_on_fakes)


def plainly_a_frame() -> pd.DataFrame:
    return a_frame()


def a_frame_or_a_table() -> pd.DataFrame | pa.Table:
    return a_frame()


@pytest.mark.parametrize("handler", [plainly_a_frame, a_frame_or_a_table], ids=["a-frame", "a-frame-or-a-table"])
def test_a_tables_handler_may_say_it_returns_a_frame_or_a_table(handler: Callable[..., object]) -> None:
    """As a citizen developer's code annotates it, `-> pd.DataFrame`, for ctzd's apps."""
    route = api.get("/plain", handler, deadline_s=1, response=api.Table[Position], errors=[])  # ty: ignore[invalid-argument-type] -- a named function
    assert route.table


def not_a_frame() -> list[Position]:
    return []


def test_a_tables_handler_that_returns_anything_else_is_refused() -> None:
    with pytest.raises(api.DeclarationError, match=r"returns list\[.*Position\], and its route declares response="):
        api.get("/not", not_a_frame, deadline_s=1, response=api.Table[Position], errors=[])
