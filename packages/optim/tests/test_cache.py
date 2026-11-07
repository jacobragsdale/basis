"""A cache entry keeps its frame exactly, and when its source was read; a stale or unreadable entry is read live, and
no reader sees half an entry."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from builders import Clock, Rows
from pandas.testing import assert_frame_equal

from optim.data.cache import FileCache
from optim.data.loader import Loader
from optim.datasets import SECURITIES
from optim.storage import Root
from optim.testing import CASH, frame

FETCHED_AT = datetime(2026, 10, 16, 13, 5, tzinfo=UTC)


def test_an_entry_gives_back_its_frame_and_fetch_time(tmp_path: Path) -> None:
    cache, securities = FileCache(Root.at(tmp_path)), frame(SECURITIES, {})

    cache.put("securities/abc", securities, FETCHED_AT)
    cached, when = cache.get("securities/abc") or pytest.fail("no entry")

    assert_frame_equal(cached, securities)  # dtypes included: the rating's ordered category, the maturity's ms dates
    assert when == FETCHED_AT
    assert [path.name for path in (tmp_path / "securities").iterdir()] == ["abc.parquet"]  # no temporary file is left


def test_a_file_that_is_not_an_entry_is_refused(tmp_path: Path) -> None:
    (tmp_path / "securities").mkdir()
    frame(SECURITIES, {}).to_parquet(tmp_path / "securities" / "abc.parquet", index=False)

    with pytest.raises(ValueError, match="no fetch time"):
        FileCache(Root.at(tmp_path)).get("securities/abc")


@pytest.mark.parametrize(("ttl", "reads"), [(timedelta(hours=1), ["miss", "hit"]), (timedelta(seconds=1), ["miss", "miss"])], ids=["fresh", "stale"])
def test_an_entry_older_than_its_ttl_is_read_live_again(tmp_path: Path, ttl: timedelta, reads: list[str]) -> None:
    """The loader's clock moves a second at each reading, so a one-second ttl has passed by the second load."""
    dataset = replace(SECURITIES, cache=ttl)
    source = Rows({dataset: frame(SECURITIES, {}, CASH)})
    loader = Loader({"oracle": source}, FileCache(Root.at(tmp_path)), clock=Clock())

    seen = []
    for _ in reads:
        loader.load(dataset, as_of_date="2026-10-16")
        seen.append("hit" if len(source.fetches) == len(seen) else "miss")

    assert seen == reads


def test_an_unreadable_entry_is_read_live(tmp_path: Path) -> None:
    dataset = replace(SECURITIES, cache=timedelta(hours=1))
    source = Rows({dataset: frame(SECURITIES, {}, CASH)})
    loader = Loader({"oracle": source}, FileCache(Root.at(tmp_path)), clock=Clock())
    loader.load(dataset, as_of_date="2026-10-16")
    [entry] = (tmp_path / "securities").iterdir()
    entry.write_bytes(b"not parquet")

    assert len(loader.load(dataset, as_of_date="2026-10-16")) == 2
    assert len(source.fetches) == 2
