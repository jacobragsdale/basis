"""Builders the unit tests share: a row, a frame of it from partial rows, and a job small enough to state one case."""

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import polars as pl

import basis.job as etl
from basis.job.job import Job
from basis.job.outputs import FrameWrite
from basis.job.testing import frame

DAY = etl.Interval.parse("2026-11-05..2026-11-06")


class Price(etl.Row):
    day: date
    security_id: Annotated[str, etl.MaxLength(8)]
    price: Annotated[Decimal, etl.Precision(18, 4)]
    note: str | None
    booked_at: Annotated[datetime, etl.Zone("America/New_York")] | None


PRICE: dict[str, object] = {"day": date(2026, 11, 5), "security_id": "S1", "price": Decimal(100), "note": None, "booked_at": None}


def prices(*rows: dict[str, object]) -> pl.DataFrame:
    """Price rows, each filled from a valid default row, so a test states only what it is about."""
    return frame(Price, *(PRICE | row for row in rows))


type Transform = Callable[..., object]


def make_job(*, write: FrameWrite, key: tuple[str, ...] | None = ("day", "security_id"), factor: int = 1, transform: Transform | None = None, check: Transform | None = None) -> Job:
    """A job that reads `prices` and writes them to `warehouse.prices` as `write` says; by default it copies them,
    times its params' `scale` and `factor`, which stands for a change to the job's code."""

    class Params(etl.Params):
        scale: int = 1

    class Inputs(etl.Inputs):
        prices = etl.File(Price, path="prices.parquet", empty="fail")

    class Outputs(etl.Outputs):
        prices = etl.Table(Price, database="warehouse", table="prices", write=write, key=key)

    def copy(inputs: Inputs, params: Params) -> Outputs:
        return Outputs(prices=inputs.prices.with_columns(pl.col("price") * params.scale * factor))

    return Job("prices", "tests.prices", Params, Inputs, Outputs, transform or copy, check, Path(__file__).parent)


def data_folder(root: Path, frame_: pl.DataFrame, name: str = "prices") -> Path:
    """A folder holding `frame_` as `NAME.parquet`, which a run on files reads instead of the dataset's source."""
    folder = root / "data"
    folder.mkdir(exist_ok=True)
    frame_.write_parquet(folder / f"{name}.parquet")
    return folder


def params_file(root: Path, text: str = "{}") -> Path:
    path = root / "params.json"
    path.write_text(text, encoding="utf-8")
    return path
