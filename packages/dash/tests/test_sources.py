"""More sources than a database: the JSON rows another service answers a GET with, Parquet in a lake, and a
workbook's sheet in a folder, each read off the event loop, conformed to its row, and cached as any dataset is; a
file's path naming its params. Each runs on basis-core's fakes: an ASGI app for the service, and a folder for the lake."""

from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from dash import html
from fastapi import FastAPI

import basis.dash as bd
from basis.core import fakes
from basis.core.errors import BadReply, Unavailable
from basis.core.settings import SettingsError
from basis.dash import testing


class Price(bd.Row):
    cusip: str
    price: float
    as_of: date


PRICING = bd.http("pricing", deadline_s=2)
DESK = bd.folder("desk")
ARCHIVE = bd.lake("archive")
PRICES = bd.fetch("prices", PRICING, "/prices", Price, binds={"as_of": date}, rows_at="result.items", ttl_s=60, stale_s=0)
FLAT = bd.fetch("flat_prices", PRICING, "/flat", Price, ttl_s=60, stale_s=0)
MARKS = bd.file("marks", DESK, "marks/{as_of}.csv", Price, binds={"as_of": date}, ttl_s=60, stale_s=0)
BOOK = bd.file("book", DESK, "book.xlsx", Price, sheet="Prices", ttl_s=60, stale_s=0)
HISTORY = bd.file("history", ARCHIVE, "prices/{as_of}", Price, binds={"as_of": date}, ttl_s=60, stale_s=0)
APP = bd.App(name="sources", title="Sources", settings=bd.Settings, resources=[PRICING, DESK, ARCHIVE], pages=[bd.page("/", "Sources", layout=html.Div())])
DAY = date(2026, 11, 13)


def _service() -> FastAPI:
    service = FastAPI()

    @service.get("/prices")
    async def prices(as_of: str) -> dict[str, object]:
        return {"result": {"items": [{"cusip": "001234", "price": 100.5, "as_of": as_of}, {"cusip": "912828X1", "price": "99.25", "as_of": as_of}]}}

    @service.get("/flat")
    async def flat() -> list[dict[str, object]]:
        return [{"cusip": "001234", "price": 1, "as_of": "2026-11-13", "extra": {"nested": True}}]

    return service


@pytest.fixture
def folders(tmp_path: Path) -> tuple[Path, Path]:
    desk, archive = tmp_path / "desk", tmp_path / "archive"
    (desk / "marks").mkdir(parents=True)
    (desk / "marks" / "2026-11-13.csv").write_text("cusip,price,as_of\n001234,100.5,2026-11-13\n", encoding="utf-8")
    pd.DataFrame({"cusip": ["001234"], "price": ["101"], "as_of": ["2026-11-13"]}).to_excel(desk / "book.xlsx", sheet_name="Prices", index=False)
    (archive / "prices" / "2026-11-13").mkdir(parents=True)
    table = pa.table({"cusip": ["001234", "912828X1"], "price": [100.0, 99.5], "as_of": pa.array([DAY, DAY], pa.date32())})
    pq.write_table(table, archive / "prices" / "2026-11-13" / "part-0.parquet")
    return desk, archive


@pytest.fixture
def pricing() -> fakes.Http:
    return fakes.Http(_service())


@pytest.fixture
def client(folders: tuple[Path, Path], pricing: fakes.Http) -> Iterator[testing.Client]:
    desk, archive = folders
    with testing.client(APP, fakes={PRICING: pricing, DESK: desk, ARCHIVE: archive}) as running:
        yield running


def _load(client: testing.Client, dataset: bd.Dataset, /, **params: object) -> pd.DataFrame:
    async def loading() -> pd.DataFrame:
        return await bd.load(dataset, **params)

    portal = client.http.portal
    assert portal is not None  # inside the client's block
    return portal.call(loading)


def test_a_fetch_reads_the_rows_a_get_answers_its_params_in_the_query(client: testing.Client) -> None:
    frame = _load(client, PRICES, as_of=DAY)
    assert frame.to_dict("list") == {"cusip": ["001234", "912828X1"], "price": [100.5, 99.25], "as_of": [DAY, DAY]}


def test_a_fetch_whose_reply_is_not_its_rows_is_a_bad_reply(client: testing.Client, pricing: fakes.Http) -> None:
    with pytest.raises(BadReply, match="answered GET /flat with a body that is not rows of Price: row 0's extra is a JSON dict"):
        _load(client, FLAT)
    pricing.fails("/prices", Unavailable)  # every attempt: a GET is made again while its deadline allows
    with pytest.raises(Unavailable):
        _load(client, PRICES, as_of=date(2026, 11, 12))


def test_a_files_path_names_its_params_and_a_sheet_and_a_lake_read_as_any_file(client: testing.Client) -> None:
    assert _load(client, MARKS, as_of=DAY)["price"].tolist() == [100.5]
    assert _load(client, BOOK)[["cusip", "price"]].to_dict("list") == {"cusip": ["001234"], "price": [101.0]}
    assert _load(client, HISTORY, as_of=DAY)["cusip"].tolist() == ["001234", "912828X1"]
    with pytest.raises(Unavailable, match="lake archive could not give"):
        _load(client, HISTORY, as_of=date(2026, 11, 1))


@pytest.mark.parametrize(
    ("declare", "message"),
    [
        (lambda: bd.file("marks", DESK, "marks/{as_of}.csv", Price, ttl_s=1, stale_s=0), "path names as_of, and it binds none"),
        (lambda: bd.file("marks", DESK, "marks.xlsx", Price, ttl_s=1, stale_s=0), "name the sheet of a workbook it reads"),
        (lambda: bd.file("marks", DESK, "marks.csv", Price, sheet="Prices", ttl_s=1, stale_s=0), "and no sheet of any other file"),
        (lambda: bd.file("marks", ARCHIVE, "marks.csv", Price, ttl_s=1, stale_s=0), "give a Parquet file, or a folder of them"),
        (lambda: bd.fetch("prices", PRICING, "prices", Price, ttl_s=1, stale_s=0), "give the service's own path, after its base URL"),
        (lambda: bd.fetch("prices", PRICING, "/prices?as_of=x", Price, ttl_s=1, stale_s=0), "with no query"),
        (lambda: bd.fetch("prices", PRICING, "/prices", Price, rows_at="a-b", ttl_s=1, stale_s=0), "give the dotted path to the list of rows"),
        (lambda: bd.http("pricing", deadline_s=0), "give it deadline_s"),
    ],
)
def test_a_source_states_what_it_reads(declare: Callable[[], object], message: str) -> None:
    with pytest.raises(bd.DeclarationError, match=message):
        declare()


def test_a_service_reads_its_settings_and_a_fake_of_another_kind_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match=r"set BASIS_HTTP__PRICING__URL"):
        bd.build(APP, settings=bd.Settings.given())
    with pytest.raises(bd.DeclarationError, match=r"give basis-core's fakes\.Http"):
        bd.build(APP, settings=bd.Settings.given(), fakes={PRICING: object(), DESK: tmp_path, ARCHIVE: tmp_path})
