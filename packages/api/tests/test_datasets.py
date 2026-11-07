"""A dataset route: what it declares, its columns, and its answer as Arrow when a request asks for it, or JSON."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import IntEnum, StrEnum
from http import HTTPStatus
from typing import Annotated, Literal
from uuid import UUID

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, RootModel, computed_field

from basis import api
from basis.api import arrow, dataset, fakes

ARROW = "application/vnd.apache.arrow.stream"
AT = datetime(2026, 11, 6, 14, 30, tzinfo=UTC)


class Desk(StrEnum):
    RATES = "rates"
    CREDIT = "credit"


class Tier(IntEnum):
    ONE = 1
    TWO = 2


class Exposure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    book: str
    desk: Desk
    tier: Tier
    side: Literal["long", "short"]
    lots: int
    weight: float
    hedged: bool
    exposure: Annotated[Decimal, Field(max_digits=18, decimal_places=2)]
    margin: Annotated[Decimal, Field(max_digits=10, decimal_places=4)] | None = None
    as_of: date
    at: AwareDatetime
    position_id: UUID
    note: str | None = Field(default=None, serialization_alias="comment")


ROW = Exposure(
    book="RATES-1", desk=Desk.RATES, tier=Tier.TWO, side="long", lots=7, weight=0.25, hedged=True, exposure=Decimal("1692406.25"),
    as_of=date(2026, 11, 6), at=AT, position_id=UUID("019a5c3e-0000-7000-8000-000000000001"),
)  # fmt: skip


def exposures() -> list[Exposure]:
    """Sync, as a handler that calls blocking code is: FastAPI runs it in its threadpool."""
    return [ROW, ROW.model_copy(update={"book": "RATES-2", "margin": Decimal("0.0125"), "note": "watch"})]


async def none_yet() -> tuple[Exposure, ...]:
    return ()


class Lot(BaseModel):
    lots: int


@dataclass(frozen=True)
class Held:
    """What a handler may return in a row's place, as FastAPI reads a response: an object whose attributes are its
    fields."""

    lots: object


def plain() -> list[Lot]:
    return [Lot(lots=7)]


def held() -> list[Lot]:
    return [Held(lots=7), Held(lots=8)]  # ty: ignore[invalid-return-type] -- objects whose attributes the rows read from


WAREHOUSE = api.database("warehouse", deadline_s=1, max_concurrent=2)
INSERT = "INSERT INTO marks (at) VALUES (%(at)s)"


async def mark(warehouse: Annotated[api.Writer, api.given(WAREHOUSE)]) -> list[Lot]:
    await warehouse.execute(INSERT, at=AT)
    return [Held(lots="seven")]  # ty: ignore[invalid-return-type] -- a row that does not validate, after a write


SERVICE = api.Service(
    name="exposures",
    settings=api.Settings,
    resources=[WAREHOUSE],
    routes=[
        api.get("/exposures", exposures, deadline_s=1, response=list[Exposure], errors=[], dataset=True),
        api.get("/none-yet", none_yet, deadline_s=1, response=tuple[Exposure, ...], errors=[], dataset=True),
        api.get("/held", held, deadline_s=1, response=list[Lot], errors=[], dataset=True),
        api.post("/marks", mark, deadline_s=1, response=list[Lot], errors=[], dataset=True),
        api.get("/plain", plain, deadline_s=1, response=list[Lot], errors=[]),
    ],
)


@pytest.fixture
def warehouse() -> fakes.Database:
    return fakes.Database(answers={INSERT: 1})


@pytest.fixture
def client(warehouse: fakes.Database) -> Iterator[TestClient]:
    with TestClient(api.build(SERVICE, settings=api.Settings.given(), fakes={WAREHOUSE: warehouse}).asgi, raise_server_exceptions=False) as started:
        yield started


def test_each_field_is_a_column_of_its_type_nullable_only_where_it_may_be_none() -> None:
    rows = dataset.rows(list[Exposure], "exposures")
    assert arrow.schema(rows) == pa.schema([
        pa.field("book", pa.string(), nullable=False),
        pa.field("desk", pa.string(), nullable=False),
        pa.field("tier", pa.int64(), nullable=False),
        pa.field("side", pa.string(), nullable=False),
        pa.field("lots", pa.int64(), nullable=False),
        pa.field("weight", pa.float64(), nullable=False),
        pa.field("hedged", pa.bool_(), nullable=False),
        pa.field("exposure", pa.decimal128(18, 2), nullable=False),
        pa.field("margin", pa.decimal128(10, 4), nullable=True),
        pa.field("as_of", pa.date32(), nullable=False),
        pa.field("at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("position_id", pa.string(), nullable=False),
        pa.field("comment", pa.string(), nullable=True),
    ])  # fmt: skip


def test_a_request_that_asks_for_arrow_gets_the_rows_it_would_get_as_json(client: TestClient) -> None:
    response = client.get("/exposures", headers={"accept": ARROW})
    assert (response.status_code, response.headers["content-type"], response.headers["vary"]) == (HTTPStatus.OK, ARROW, "Accept")
    table = pa.ipc.open_stream(response.content).read_all()
    assert table.to_pylist()[1] == {
        "book": "RATES-2", "desk": "rates", "tier": 2, "side": "long", "lots": 7, "weight": 0.25, "hedged": True, "exposure": Decimal("1692406.25"),
        "margin": Decimal("0.0125"), "as_of": date(2026, 11, 6), "at": AT, "position_id": "019a5c3e-0000-7000-8000-000000000001", "comment": "watch",
    }  # fmt: skip
    as_json = client.get("/exposures")
    assert (as_json.headers["content-type"], as_json.headers["vary"]) == ("application/json", "Accept")
    assert [row["book"] for row in as_json.json()] == table.column("book").to_pylist()


def test_no_rows_are_a_stream_of_the_schema_alone(client: TestClient) -> None:
    response = client.get("/none-yet", headers={"accept": ARROW})
    table = pa.ipc.open_stream(response.content).read_all()
    assert (table.num_rows, table.schema.names[:2]) == (0, ["book", "desk"])


def test_rows_read_from_objects_attributes_as_arrow_and_as_json_alike(client: TestClient) -> None:
    as_json = client.get("/held").json()
    table = pa.ipc.open_stream(client.get("/held", headers={"accept": ARROW}).content).read_all()
    assert as_json == table.to_pylist() == [{"lots": 7}, {"lots": 8}]


@pytest.mark.parametrize("accept", [ARROW, "application/json"], ids=["arrow", "json"])
def test_a_row_that_does_not_validate_is_a_defect_and_its_writes_roll_back(client: TestClient, warehouse: fakes.Database, accept: str) -> None:
    assert client.post("/marks", headers={"accept": accept}).status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert (warehouse.committed, warehouse.rolled_back) == ([], [fakes.Statement(INSERT, {"at": AT})])


def test_a_route_that_declares_no_dataset_answers_json_whatever_is_asked(client: TestClient) -> None:
    response = client.get("/plain", headers={"accept": ARROW})
    assert (response.headers["content-type"], "vary" in response.headers) == ("application/json", False)


@pytest.mark.parametrize(
    ("accept", "wanted"),
    [
        (ARROW, True),
        (f"{ARROW}, application/json;q=0.5", True),
        (f"application/json, {ARROW}", True),
        (f"application/json, {ARROW};q=0.9", False),
        (f"{ARROW};q=0", False),
        (f"{ARROW};q=2", False),
        (f"{ARROW};q=high", False),
        ("APPLICATION/VND.APACHE.ARROW.STREAM", True),
        ("*/*", False),
        ("application/*", False),
        (None, False),
        ("", False),
    ],
    ids=["alone", "above-json", "level-with-json", "below-json", "refused", "weight-too-high", "weight-not-a-number", "any-case", "anything", "any-json", "no-header", "empty"],
)
def test_arrow_is_the_answer_when_accept_ranks_it_at_least_as_high_as_json(accept: str | None, wanted: bool) -> None:  # noqa: FBT001 -- a table's column
    assert arrow.wanted(accept) is wanted


class Nested(BaseModel):
    exposure: Exposure


class Listed(BaseModel):
    lots: list[int]


class Undigited(BaseModel):
    exposure: Decimal


class TooPrecise(BaseModel):
    exposure: Annotated[Decimal, Field(max_digits=40, decimal_places=2)]


class Mixed(BaseModel):
    code: Literal["a", 1]


class Naive(BaseModel):
    at: datetime


class Open(BaseModel):
    model_config = ConfigDict(extra="allow")

    book: str


class Computed(BaseModel):
    lots: int

    @computed_field
    @property
    def doubled(self) -> int:
        return self.lots * 2


class Books(RootModel[list[str]]):
    pass


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (Exposure, "a dataset is rows, so answer list[Row]"),
        (dict[str, Exposure], "a dataset is rows, so answer list[Row]"),
        (list[int], "a dataset is rows, so answer list[Row]"),
        (list[Books], "a dataset is rows, so answer list[Row]"),
        (list[Nested], "Nested.exposure is Exposure, which Arrow's columns do not hold"),
        (list[Listed], "Listed.lots is list[int], which Arrow's columns do not hold"),
        (list[Undigited], "Undigited.exposure is a decimal, which a column holds with one precision and one scale"),
        (list[TooPrecise], "with at most 38 digits"),
        (list[Mixed], "Mixed.code is typing.Literal['a', 1], which Arrow's columns do not hold"),
        (list[Open], "allow keys beyond their fields"),
        (list[Computed], "compute doubled, which no column holds"),
        (list[Naive], "Naive.at, a datetime, which takes a time without its offset"),
    ],
    ids=["one-row", "a-mapping", "not-models", "root-models", "a-nested-model", "a-list", "a-decimal-without-its-digits", "too-many-digits", "a-mixed-literal", "open", "computed", "a-naive-time"],
)
def test_a_dataset_that_is_not_flat_rows_is_refused_when_its_route_is_declared(response: object, message: str) -> None:
    def handler() -> None: ...

    handler.__annotations__["return"] = response
    with pytest.raises(api.ServiceError, match=message.replace("[", r"\[").replace("]", r"\]")):
        api.get("/rows", handler, deadline_s=1, response=response, errors=[], dataset=True)
