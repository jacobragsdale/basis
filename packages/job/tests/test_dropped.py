"""A CSV or Parquet file dropped on a shared folder, as a job reads it (step 12.3): `etl.File(..., folder=)`, its
delimiter and encoding declared, and a file name that may hold `*` and `?`, every match read in name order."""

from collections.abc import Callable
from pathlib import Path

import httpx2
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from builders import DAY
from opentelemetry.trace import NoOpTracer

import basis.job as etl
from basis.core.databases import Database, open_database
from basis.core.folders import Folder, FolderError
from basis.core.opened import Waits
from basis.core.settings import GivenSettings
from basis.core.snapshots import InputRecord
from basis.job.loader import EmptyInputError, load


class Trade(etl.Row):
    account: str
    price: float


def no_databases(name: str) -> Database:
    return open_database(GivenSettings(), name)


def no_services(name: str) -> tuple[httpx2.AsyncClient, Waits]:
    raise LookupError(name)


@pytest.fixture
def shared(tmp_path: Path) -> Folder:
    (tmp_path / "drops").mkdir()
    return Folder("drops", tmp_path / "drops")


def read(file: etl.File, shared: Folder | None) -> tuple[pd.DataFrame, InputRecord]:
    class Inputs(etl.Inputs):
        trades = file

    opener: Callable[[str], Folder] | None = (lambda name: shared if name == "drops" else Folder(name, Path("/nowhere"))) if shared is not None else None
    frames, records = load(Inputs.declared(), DAY, open_database=no_databases, reach=no_services, tracer=NoOpTracer(), data=None, open_folder=opener)
    return frames["trades"], records["trades"]


def test_a_csv_on_a_share_is_read_in_its_declared_delimiter_and_encoding(shared: Folder) -> None:
    (shared.root / "trades").mkdir()
    (shared.root / "trades" / "2026-11-05.csv").write_bytes("account;price\nCafé;100,5\n".replace("100,5", "100.5").encode("cp1252"))
    frame, record = read(etl.File(Trade, folder="drops", path="trades/{start:%Y-%m-%d}.csv", delimiter=";", encoding="cp1252", empty="fail"), shared)
    assert frame.to_dict("records") == [{"account": "Café", "price": 100.5}]
    assert record.source == "drops: trades/2026-11-05.csv"


def test_every_file_a_pattern_matches_is_read_in_name_order_and_the_record_names_each(shared: Folder) -> None:
    for stamp, account in (("20261105_0930", "B"), ("20261105_0815", "A"), ("20261106_0800", "C")):
        (shared.root / f"trades_{stamp}.csv").write_text(f"account,price\n{account},1\n", encoding="utf-8")
    (shared.root / ".trades_20261105_1000.csv.basis-tmp").write_text("account,price\nX,1\n", encoding="utf-8")  # a file still being written
    frame, record = read(etl.File(Trade, folder="drops", path="trades_{start:%Y%m%d}_*.csv", empty="fail"), shared)
    assert frame["account"].tolist() == ["A", "B"]
    assert record.source == "drops: trades_20261105_*.csv (trades_20261105_0815.csv, trades_20261105_0930.csv)"


def test_a_parquet_file_on_a_share_is_read_too(shared: Folder) -> None:
    pq.write_table(pa.table({"account": ["A"], "price": [2.5]}), shared.root / "trades.parquet")
    frame, _ = read(etl.File(Trade, folder="drops", path="trades.parquet", empty="fail"), shared)
    assert frame["price"].tolist() == [2.5]


def test_a_file_that_has_not_landed_fails_asking_whether_it_has_even_where_empty_is_allowed(shared: Folder) -> None:
    with pytest.raises(FolderError, match=r"drops has no file matching trades_20261105_\*\.csv, at .*: has it landed, or is the path wrong\?"):
        read(etl.File(Trade, folder="drops", path="trades_{start:%Y%m%d}_*.csv", empty="allow"), shared)


def test_a_file_with_only_its_header_is_empty_which_its_dataset_may_allow(shared: Folder) -> None:
    (shared.root / "trades.csv").write_text("account,price\n", encoding="utf-8")
    assert len(read(etl.File(Trade, folder="drops", path="trades.csv", empty="allow"), shared)[0]) == 0
    with pytest.raises(EmptyInputError):
        read(etl.File(Trade, folder="drops", path="trades.csv", empty="fail"), shared)


def test_a_file_in_a_folder_needs_the_runs_folders(shared: Folder) -> None:
    del shared
    with pytest.raises(TypeError, match="trades reads a file in the folder drops, and the run was given no folders"):
        read(etl.File(Trade, folder="drops", path="trades.csv", empty="fail"), None)


@pytest.mark.parametrize(
    ("declared", "says"),
    [
        ({"folder": "drops", "path": "../elsewhere/trades.csv"}, "is no path inside the folder"),
        ({"folder": "drops", "path": "trades/*/a.csv"}, r"only a file's name may hold \* or \?"),
        ({"folder": "Drops", "path": "trades.csv"}, "a folder's name is lower-case letters"),
        ({"path": "trades.csv", "delimiter": ";;"}, "a CSV's delimiter is one character"),
        ({"path": "trades.csv", "encoding": "klingon"}, "klingon is no encoding Python knows"),
        ({"path": "trades.parquet", "delimiter": ";"}, "a Parquet file has no delimiter or encoding"),
        ({"path": "trades_*.csv"}, r"only a file in a folder may hold \* or \?"),
    ],
)
def test_a_file_declared_wrong_is_refused_with_the_fix(declared: dict[str, str], says: str) -> None:
    with pytest.raises(ValueError, match=says):
        etl.File(Trade, empty="fail", **declared)
