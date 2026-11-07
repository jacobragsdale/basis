from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import polars as pl
import pytest

from basis.core import snapshots
from basis.core.snapshots import RunRecord, Status
from basis.core.storage import Root

PRICES = pl.DataFrame({"security_id": ["S1"], "price": [Decimal("100.5")]})


def record(run_id: str = "01-run", status: Status = "checked") -> RunRecord:
    started = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
    return RunRecord(run_id=run_id, job="prices", module="jobs.prices.job", version="0.0.0", started_at=started, params_path="params.json", params_sha256="0" * 64, status=status)


def test_a_record_saved_again_keeps_the_params_and_datasets_the_run_saved(tmp_path: Path) -> None:
    snapshot = snapshots.write(Root.at(tmp_path), record(), '{"scale": 1}', {"prices": PRICES}, {}, package="job")
    snapshot.save_record(record(status="published"))
    assert snapshots.find(Root.at(tmp_path), "01-run").record() == record(status="published")
    assert snapshot.params_json() == '{"scale": 1}'
    assert snapshot.inputs()["prices"].equals(PRICES)


def test_a_record_cannot_replace_another_runs(tmp_path: Path) -> None:
    snapshot = snapshots.write(Root.at(tmp_path), record(), "{}", {}, {}, package="job")
    with pytest.raises(ValueError, match="cannot replace run 01-run"):
        snapshot.save_record(record(run_id="02-other"))


def test_a_new_run_cannot_take_an_id_the_store_holds(tmp_path: Path) -> None:
    snapshots.write(Root.at(tmp_path), record(), '{"scale": 1}', {"prices": PRICES}, {}, package="job")
    with pytest.raises(FileExistsError, match="run 01-run is in the store already"):
        snapshots.write(Root.at(tmp_path), record(status="failed"), '{"scale": 2}', {}, {}, package="job")
    saved = snapshots.find(Root.at(tmp_path), "01-run")
    assert (saved.record().status, saved.params_json(), list(saved.manifest.datasets)) == ("checked", '{"scale": 1}', ["inputs/prices"])


def test_a_record_keeps_its_clocks_times_and_its_ids_in_order(tmp_path: Path) -> None:
    made = record().model_copy(update={"clocks": {"clock": [datetime(2026, 11, 6, 6, 0, 1, tzinfo=UTC), datetime(2026, 11, 6, 6, 0, tzinfo=UTC)]}, "ids": {"ids": [UUID(int=2), UUID(int=1)]}})
    snapshots.write(Root.at(tmp_path), made, "{}", {}, {}, package="job")
    saved = snapshots.find(Root.at(tmp_path), "01-run").record()
    assert (saved.clocks, saved.ids) == (made.clocks, made.ids)


def test_a_record_from_before_clocks_and_ids_still_loads() -> None:
    older = record().model_dump(mode="json", exclude={"clocks", "ids"})
    assert (RunRecord.model_validate(older).clocks, RunRecord.model_validate(older).ids) == ({}, {})
