"""`basis backfill JOB --from DATE --to DATE` (step 12.4): one run per business day of the job's calendar, in order,
stopping at the first that fails, and, run again, carrying on from it: a day already done is skipped, and a day whose
run stopped partway through publishing is resumed rather than run again."""

import dataclasses
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from builders import prices
from test_publishing import database, settings, two_databases

import basis.job as etl
from basis.core.databases import Database
from basis.core.errors import BasisError, Rejected
from basis.core.storage import Root
from basis.job import backfill
from basis.job.job import Job

SIFMA = etl.calendar("sifma-us")
FRIDAY, MONDAY, TUESDAY = date(2026, 11, 6), date(2026, 11, 9), date(2026, 11, 10)


def job() -> Job:
    return dataclasses.replace(two_databases(), calendar=SIFMA)


def fakes() -> Callable[[str], Database]:
    found = {"alpha": database("alpha", ("dbo.prices", None)), "beta": database("beta", ("dbo.prices", None))}
    return found.__getitem__


def filled(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    prices({}).to_parquet(data / "prices.parquet", index=False)
    params = tmp_path / "params.json"
    params.write_text("{}", encoding="utf-8")
    return params, data


def test_a_backfill_runs_each_business_day_in_order_and_skips_the_weekend_and_the_holidays() -> None:
    assert backfill.days(SIFMA, FRIDAY, TUESDAY) == [FRIDAY, MONDAY, TUESDAY]
    assert backfill.days(SIFMA, TUESDAY, date(2026, 11, 12)) == [TUESDAY, date(2026, 11, 12)]  # Veterans Day, which SIFMA closes
    assert backfill.days(SIFMA, date(2026, 11, 7), date(2026, 11, 8)) == []


def test_a_backfill_stops_at_the_first_failure_and_run_again_resumes_it_and_carries_on(tmp_path: Path) -> None:
    alpha, beta = database("alpha", ("dbo.prices", None)), database("beta", ("dbo.prices", None))
    found = {"alpha": alpha, "beta": beta}
    params, data = filled(tmp_path)
    root = Root.at(tmp_path / "snapshots")

    def go() -> tuple[list[backfill.Step], tuple[str, str] | None]:
        return backfill.backfill(job(), settings("alpha", "beta"), params=params, first=FRIDAY, last=TUESDAY, publish=True, snapshot_root=root, data=data, databases=found.__getitem__)

    assert go()[1] is None
    beta.fails(Rejected, on="apply", times=1)  # beta refuses the next day's publish, after alpha commits it
    steps, failure = backfill.backfill(job(), settings("alpha", "beta"), params=params, first=FRIDAY, last=date(2026, 11, 13), publish=True, snapshot_root=root, data=data, databases=found.__getitem__)
    assert [(step.day, step.did, step.status) for step in steps] == [
        (FRIDAY, "skipped", "published"),
        (MONDAY, "skipped", "published"),
        (TUESDAY, "skipped", "published"),
        (date(2026, 11, 12), "ran", "partial"),
    ]
    assert failure is not None
    assert failure[0].startswith("the backfill of two_databases stopped at 2026-11-12: run ")
    assert failure[1] == "fix what it says, then run the same backfill again, which carries on from 2026-11-12"
    partial = steps[-1].run_id
    steps, failure = backfill.backfill(job(), settings("alpha", "beta"), params=params, first=FRIDAY, last=date(2026, 11, 13), publish=True, snapshot_root=root, data=data, databases=found.__getitem__)
    assert failure is None
    assert [(step.day, step.did, step.status) for step in steps][-2:] == [(date(2026, 11, 12), "resumed", "published"), (date(2026, 11, 13), "ran", "published")]
    assert steps[-2].run_id == partial
    assert (len(alpha.rows("dbo.prices")), len(beta.rows("dbo.prices"))) == (5, 5)  # one day's row in each, each day once


def test_a_backfill_without_publish_checks_each_day_and_skips_one_already_checked(tmp_path: Path) -> None:
    params, data = filled(tmp_path)
    root = Root.at(tmp_path / "snapshots")
    steps, failure = backfill.backfill(job(), settings("alpha", "beta"), params=params, first=FRIDAY, last=MONDAY, publish=False, snapshot_root=root, data=data, databases=fakes())
    assert (failure, [step.status for step in steps]) == (None, ["checked", "checked"])
    steps, _ = backfill.backfill(job(), settings("alpha", "beta"), params=params, first=FRIDAY, last=TUESDAY, publish=False, snapshot_root=root, data=data, databases=fakes())
    assert [step.did for step in steps] == ["skipped", "skipped", "ran"]


def test_each_backfill_run_is_labelled_with_the_backfill(tmp_path: Path) -> None:
    params, data = filled(tmp_path)
    root = Root.at(tmp_path / "snapshots")
    steps, _ = backfill.backfill(job(), settings("alpha", "beta"), params=params, first=FRIDAY, last=FRIDAY, publish=False, snapshot_root=root, data=data, labels={"ticket": "T-1"}, databases=fakes())
    assert steps[0].labels == {"ticket": "T-1", "backfill": "2026-11-06..2026-11-06"}


@pytest.mark.parametrize(
    ("first", "last", "calendar", "says"),
    [
        (TUESDAY, FRIDAY, SIFMA, "--from 2026-11-10 is after --to 2026-11-06: give the first day, then the last"),
        (date(2026, 11, 7), date(2026, 11, 8), SIFMA, "no business day of two_databases's calendar falls from 2026-11-07 to 2026-11-08: give days that hold one"),
        (FRIDAY, MONDAY, None, "two_databases declares no calendar, which a backfill's days are its business days of"),
    ],
)
def test_a_backfill_that_names_no_business_day_is_refused(tmp_path: Path, first: date, last: date, calendar: etl.Calendar | None, says: str) -> None:
    params, data = filled(tmp_path)
    with pytest.raises(BasisError, match=says.replace("(", r"\(").replace(")", r"\)")):
        backfill.backfill(dataclasses.replace(two_databases(), calendar=calendar), settings(), params=params, first=first, last=last, publish=False, snapshot_root=Root.at(tmp_path / "s"), data=data)
