import hashlib
import struct
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import cache
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from basis.core.storage import Root, Store, digest

DECIMAL = pa.decimal128(18, 4)
STAMP = pa.timestamp("us", tz="UTC")


def table(**overrides: pa.Array) -> pa.Table:
    """Three rows of every kind of column a part stores; `quantity` and `note` each hold a null."""
    columns = {
        "day": pa.array([date(2026, 11, 5)] * 3, pa.date32()),
        "security_id": pa.array(["S1", "S2", "S3"], pa.large_string()),
        "price": pa.array([Decimal("100.5"), Decimal(101), Decimal("99.25")], DECIMAL),
        "quantity": pa.array([10, None, 30], pa.int64()),
        "note": pa.array(["first", None, "third"], pa.large_string()),
        "closed": pa.array([False, True, False]),
        "booked_at": pa.array([datetime(2026, 11, 5, 14, 30, tzinfo=UTC)] * 3, STAMP),
    }
    return pa.table(columns | overrides)


def chunked() -> pa.Table:
    return pa.concat_tables([table().slice(0, 1), table().slice(1, 2)])


def sliced() -> pa.Table:
    """The middle third of a larger table, sharing its buffers."""
    return pa.concat_tables([table()] * 3).combine_chunks().slice(3, 3)


def garbage_under_nulls() -> pa.Table:
    """The same values, with other bytes under each null and in the unused bits of each null bitmap."""
    validity = pa.py_buffer(bytes([0b11111101]))
    quantity = pa.Array.from_buffers(pa.int64(), 3, [validity, pa.py_buffer(struct.pack("<3q", 10, 99, 30))], null_count=1)
    offsets = pa.py_buffer(struct.pack("<4q", 0, 5, 12, 17))
    note = pa.Array.from_buffers(pa.large_string(), 3, [validity, offsets, pa.py_buffer(b"firstGARBAGEthird")], null_count=1)
    return table(quantity=quantity, note=note)


def written_with(**settings: object) -> Callable[[], pa.Table]:
    def read_back() -> pa.Table:
        sink = pa.BufferOutputStream()
        pq.write_table(table(), sink, **settings)
        return pq.read_table(pa.BufferReader(sink.getvalue()))

    return read_back


@pytest.mark.parametrize(
    "layout",
    [chunked, sliced, garbage_under_nulls, written_with(compression="zstd"), written_with(compression="snappy", use_dictionary=True, row_group_size=1)],
    ids=["chunked", "sliced", "garbage under nulls", "read from zstd", "read from snappy, dictionaries and a row group per row"],
)
def test_equal_data_gets_one_hash_however_it_is_laid_out_or_was_stored(layout: Callable[[], pa.Table]) -> None:
    assert layout().equals(table())
    assert digest(layout()) == digest(table())


ROWS = 300_000


@cache
def many() -> pa.Table:
    """Rows enough for several pages, and a column whose dictionary would outgrow its page, as a large input's would."""
    return pa.table(
        {
            "id": pa.array(range(ROWS), pa.int64()),
            "security_id": pa.array([f"SEC{row * 7919 % 200_003:07d}" for row in range(ROWS)], pa.large_string()),
            "quantity": pa.array([None if row % 11 == 0 else row * 31 % 1_000_003 for row in range(ROWS)], pa.int64()),
        }
    )


@pytest.mark.parametrize(
    "layout",
    [
        lambda: pa.concat_tables([many().slice(start, 777) for start in range(0, ROWS, 777)]),
        lambda: pa.concat_tables([many().slice(0, 1), many().slice(1, 99_998), many().slice(99_999)]),
        lambda: pa.concat_tables([many()] * 2).combine_chunks().slice(ROWS, ROWS),
    ],
    ids=["777-row chunks", "uneven chunks", "sliced"],
)
def test_a_dataset_of_many_pages_gets_one_hash_however_it_is_chunked(layout: Callable[[], pa.Table]) -> None:
    assert layout().equals(many())
    assert digest(layout()) == digest(many())


@pytest.mark.parametrize(
    "other",
    [
        table(quantity=pa.array([10, 20, 30], pa.int64())),
        table(note=pa.array(["first", "", "third"], pa.large_string())),
        table(price=pa.array([Decimal("100.5"), Decimal(101), Decimal("99.25")], pa.decimal128(18, 2))),
        table(booked_at=pa.array([datetime(2026, 11, 5, 14, 30, tzinfo=UTC)] * 3, pa.timestamp("us", tz="America/New_York"))),
        table().rename_columns([*table().column_names[:-1], "booked"]),
        table().select([*table().column_names[1:], "day"]),
        table().slice(0, 2),
    ],
    ids=["a value", "a null made empty", "a decimal's scale", "a datetime's zone", "a column's name", "the columns' order", "a row fewer"],
)
def test_a_change_to_the_data_or_its_types_changes_the_hash(other: pa.Table) -> None:
    assert digest(other) != digest(table())


def test_a_dataset_is_stored_once_under_its_hash_and_never_written_again(tmp_path: Path) -> None:
    store = Store(Root.at(tmp_path))
    name = store.put(table())
    assert store.object(name).read_table().equals(table())
    assert digest(store.object(name).read_table()) == name
    assert hashlib.sha256((tmp_path / "objects" / f"{name}.parquet").read_bytes()).hexdigest() == name
    (tmp_path / "objects" / f"{name}.parquet").write_bytes(b"left alone")
    assert store.put(chunked()) == name
    assert [path.name for path in (tmp_path / "objects").iterdir()] == [f"{name}.parquet"]
    assert (tmp_path / "objects" / f"{name}.parquet").read_bytes() == b"left alone"


def test_a_run_is_found_by_its_id_and_a_parts_runs_are_listed_newest_first(tmp_path: Path) -> None:
    store = Store(Root.at(tmp_path))
    for package, name, run_id in [("job", "prices", "01-older"), ("job", "prices", "02-newer"), ("report", "prices", "03-report"), ("job", "rates", "04-other")]:
        store.manifest(package, name, run_id).write_bytes(b"{}")
    (tmp_path / "runs" / "job" / "prices" / ".05-unfinished.json.tmp").write_bytes(b"{")
    assert [path.name for path in store.manifests("job", "prices")] == ["02-newer.json", "01-older.json"]
    assert store.find("03-report").path == store.manifest("report", "prices", "03-report").path
    assert store.find("04-other", "rates").path == store.manifest("job", "rates", "04-other").path
    with pytest.raises(LookupError, match="no run 04-other under"):
        store.find("04-other", "prices")
    with pytest.raises(LookupError, match=r"no run \.\./prices/02-newer under"):
        store.find("../prices/02-newer")


@pytest.mark.parametrize("name", ["", "..", ".hidden", "jobs/prices", "jobs\\prices"])
def test_a_name_that_could_leave_its_folder_is_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="cannot name a folder or file in the run store"):
        Store(Root.at(tmp_path)).manifest("job", name, "01-run")


def test_an_object_is_named_only_by_a_hash(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="is not a dataset's hash"):
        Store(Root.at(tmp_path)).object("../runs/job/prices/01-run")


def test_a_file_written_through_a_stream_appears_whole_when_the_block_ends_and_never_when_it_fails(tmp_path: Path) -> None:
    target = Root.at(tmp_path / "entries" / "a.parquet")

    def write_half() -> None:
        with target.writing() as stream:
            stream.write(b"half of a file")
            msg = "stopped"
            raise RuntimeError(msg)

    with target.writing() as stream:
        pq.write_table(table(), stream)
        assert not target.exists()  # no reader sees half of it
    with pytest.raises(RuntimeError, match="stopped"):
        write_half()

    assert target.read_table().equals(table())  # the failed write left the file as it was
    assert [path.name for path in (tmp_path / "entries").iterdir()] == ["a.parquet"]  # and no temporary file
