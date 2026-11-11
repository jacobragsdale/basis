"""The runs every release of basis saved, which the code of the day must read and replay, on what they read: a store outlives the code
that wrote it (`docs/surface/`, written by `scripts/surface.py`'s release step from each release's own code)."""

from pathlib import Path

import pytest

from basis.core import runs as core_runs
from basis.core import snapshots
from basis.core.storage import Root
from basis.job import job as jobs
from basis.job import replays
from basis.job.example import job as example

SURFACE = Path(__file__).resolve().parents[3] / "docs" / "surface"
STORES = sorted(path for path in SURFACE.glob("*/store") if path.is_dir())


@pytest.mark.parametrize("store", STORES, ids=lambda path: path.parent.name)
def test_a_run_every_release_saved_is_read_and_replays(store: Path) -> None:
    [manifest] = store.glob("runs/job/*/*.json")
    found = core_runs.find([Root.at(store)], manifest.stem, "basis.job.example.job")
    assert found.manifest.format <= snapshots.FORMAT
    result, _ = replays.replay(jobs.read(example), found)  # how its changes differ is the example's, which may change
    assert result.record.status == "checked", result.record.reasons
