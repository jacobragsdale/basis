import re
from pathlib import Path

import pytest

from basis.core import runs
from basis.core.snapshots import RunRecord, Snapshot
from basis.core.storage import Root


def never(_snapshot: Snapshot) -> tuple[RunRecord, list[str]]:
    raise AssertionError


@pytest.mark.parametrize("run_id", ["../prices/01-run", "prices/01-run", ".01-run"])
def test_an_id_that_cannot_name_a_run_is_not_found(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(LookupError, match=f"no run {re.escape(run_id)} of prices under"):
        runs.find([Root.at(tmp_path)], run_id, "prices")


@pytest.mark.parametrize("last", [0, -1])
def test_regress_needs_at_least_one_run(tmp_path: Path, last: int) -> None:
    with pytest.raises(ValueError, match=f"N must be at least 1, not {last}"):
        runs.regress(Root.at(tmp_path), "prices", package="job", last=last, labels={}, replay=never)
