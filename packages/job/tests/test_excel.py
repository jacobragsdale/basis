"""A job that reads a workbook from a shared folder and writes one to it: only `basis run --publish` writes, once its
databases commit, each workbook whole, and `basis resume` writes one a run could not."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated

import pandas as pd
import pytest
from builders import DAY, params_file

import basis.job as etl
from basis.core import fakes
from basis.core.folders import Folder
from basis.core.settings import GivenSettings
from basis.core.storage import Root
from basis.job import runs
from basis.job.job import Job
from basis.job.testing import frame


def clock() -> datetime:
    return datetime(2026, 11, 6, 6, 0, tzinfo=UTC)


class Position(etl.Row):
    account: Annotated[str, etl.MaxLength(12)]
    day: date
    quantity: float
    price: float


class Holding(etl.Row):
    account: Annotated[str, etl.MaxLength(12)]
    value: float


class Params(etl.Params):
    pass


class Inputs(etl.Inputs):
    positions = etl.Excel(Position, folder="pm", path="in/{start:%Y-%m-%d}.xlsx", sheet="Positions", empty="fail")


class Outputs(etl.Outputs):
    holdings = etl.ExcelReport(Holding, folder="pm", path="out/holdings-{start:%Y-%m-%d}.xlsx", sheet="Holdings")


def transform(inputs: Inputs, params: Params) -> Outputs:
    del params  # the job has none that change what it writes
    positions = inputs.positions
    return Outputs(holdings=pd.DataFrame({"account": positions["account"], "value": positions["quantity"] * positions["price"]}))


JOB = Job("holdings", "tests.holdings", Params, Inputs, Outputs, transform, None, Path(__file__).parent)


@pytest.fixture
def shared(tmp_path: Path) -> Folder:
    """The PMs' folder, holding the day's positions workbook, as a portfolio manager saved it."""
    (tmp_path / "pm").mkdir()
    folder = Folder("pm", tmp_path / "pm")
    saved = frame(Position, {"account": "001234", "day": date(2026, 11, 5), "quantity": 100.0, "price": 101.25}, {"account": "009", "day": date(2026, 11, 5), "quantity": 3.0, "price": 2.5})
    folder.write_workbook("in/2026-11-05.xlsx", {"Positions": saved})
    return folder


def run(tmp_path: Path, shared: Folder, *, publish: bool) -> runs.Result:
    settings = GivenSettings.model_validate({"folders": {"pm": {"path": str(shared.root)}}})
    request = runs.Request(params_file(tmp_path), DAY, publish=publish)
    return runs.run(JOB, settings, request, snapshot_root=Root.at(tmp_path / "snapshots"), clock=clock)


def test_a_job_reads_a_workbook_from_a_folder_and_a_published_run_writes_one_to_it(tmp_path: Path, shared: Folder) -> None:
    result = run(tmp_path, shared, publish=True)
    assert result.record.status == "published", result.record
    assert (result.record.inputs["positions"].source, result.record.inputs["positions"].rows) == ("pm: in/2026-11-05.xlsx [Positions]", 2)
    assert result.record.written["holdings"].status == "written"
    assert shared.read_sheet("out/holdings-2026-11-05.xlsx", "Holdings").to_dict("records") == [{"account": "001234", "value": "10125"}, {"account": "009", "value": "7.5"}]


def test_a_run_without_publish_writes_no_workbook(tmp_path: Path, shared: Folder) -> None:
    result = run(tmp_path, shared, publish=False)
    assert (result.record.status, result.record.reports["holdings"].rows, result.record.written) == ("checked", 2, {})
    assert not (shared.root / "out").exists()


def test_a_workbook_that_cannot_be_written_leaves_the_run_partial_and_its_resume_writes_it(tmp_path: Path, shared: Folder) -> None:
    blocked = shared.root / "out" / "holdings-2026-11-05.xlsx"
    blocked.mkdir(parents=True)  # a folder where the workbook goes, so its rename into place fails
    result = run(tmp_path, shared, publish=True)
    assert (result.record.status, result.record.written["holdings"].status) == ("partial", "failed")
    reason, fix = runs.failure_of(result.record, publish=True) or ("", "")
    assert reason.startswith(f"run {result.record.run_id} of {result.record.job} published, and could not write the workbook holdings: ")
    assert fix == f"fix it, then `{runs.resume_command(result.record)}`, which writes what it has not"
    blocked.rmdir()
    assert result.snapshot is not None
    settings = GivenSettings.model_validate({"folders": {"pm": {"path": str(shared.root)}}})
    resumed = runs.resume(JOB, result.snapshot, settings, clock=clock)
    assert (resumed.status, resumed.written["holdings"].status) == ("published", "written")
    assert len(shared.read_sheet("out/holdings-2026-11-05.xlsx", "Holdings")) == 2


def test_a_publishing_run_whose_folder_is_not_configured_is_refused_before_it_reads(tmp_path: Path) -> None:
    request = runs.Request(params_file(tmp_path), DAY, publish=True)
    result = runs.run(JOB, GivenSettings(), request, snapshot_root=Root.at(tmp_path / "snapshots"), clock=clock)
    assert result.record.status == "failed"
    assert result.record.reasons == ["holdings: the folder pm is not configured, and the run reads or writes it: set BASIS_FOLDERS__PM__PATH, as .env.example shows"]


@pytest.mark.parametrize(
    ("make", "says"),
    [
        (lambda: etl.Excel(Position, folder="PM", path="x.xlsx", sheet="S", empty="fail"), "a folder's name is lower-case letters"),
        (lambda: etl.Excel(Position, folder="pm", path="x.csv", sheet="S", empty="fail"), "an Excel dataset is a workbook"),
        (lambda: etl.ExcelReport(Holding, folder="pm", path="x.xls", sheet="S"), "basis writes workbooks as .xlsx"),
        (lambda: etl.ExcelReport(Holding, folder="pm", path="x.xlsx", sheet="a/b"), "a sheet's name is 1 to 31 characters"),
    ],
)
def test_a_workbook_declared_wrong_is_refused_with_the_fix(make: Callable[[], object], says: str) -> None:
    with pytest.raises(ValueError, match=says):
        make()


class Summary(etl.Row):
    account: Annotated[str, etl.MaxLength(12)]
    value: float


class BothOutputs(etl.Outputs):
    holdings = etl.ExcelReport(Holding, folder="pm", path="out/holdings-{start:%Y-%m-%d}.xlsx", sheet="Holdings")
    summaries = etl.MongoUpsert(Summary, mongo="reference", collection="summaries", key=("account",))


def both(inputs: Inputs, params: Params) -> BothOutputs:
    del params  # the job has none that change what it writes
    positions = inputs.positions
    values = pd.DataFrame({"account": positions["account"], "value": positions["quantity"] * positions["price"]})
    return BothOutputs(holdings=values, summaries=values)


BOTH = Job("both", "tests.both", Params, Inputs, BothOutputs, both, None, Path(__file__).parent)


def test_a_resume_stops_at_the_workbook_it_cannot_write_as_the_run_did_and_upserts_nothing_after_it(tmp_path: Path, shared: Folder) -> None:
    reference = fakes.Mongo()
    reference.unique["summaries"] = [("account",)]
    blocked = shared.root / "out" / "holdings-2026-11-05.xlsx"
    blocked.mkdir(parents=True)  # a folder where the workbook goes, so its rename into place fails
    settings = GivenSettings.model_validate({"folders": {"pm": {"path": str(shared.root)}}})
    request = runs.Request(params_file(tmp_path), DAY, publish=True)
    result = runs.run(BOTH, settings, request, snapshot_root=Root.at(tmp_path / "snapshots"), clock=clock, faked={"reference": reference})
    assert (result.record.status, result.record.written["holdings"].status, result.record.upserted) == ("partial", "failed", {})
    assert result.snapshot is not None
    resumed = runs.resume(BOTH, result.snapshot, settings, clock=clock, faked={"reference": reference})
    assert (resumed.status, resumed.written["holdings"].status, resumed.upserted) == ("partial", "failed", {})
    assert "summaries" not in reference.collections
    blocked.rmdir()
    finished = runs.resume(BOTH, result.snapshot, settings, clock=clock, faked={"reference": reference})
    assert (finished.status, finished.written["holdings"].status, finished.upserted["summaries"].status) == ("published", "written", "upserted")
