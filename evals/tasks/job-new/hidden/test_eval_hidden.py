"""The `security_master` job: listed, reading the whole table from `source`, replacing `security_master` in the
warehouse with the active securities, and refusing to write when two active securities share an ISIN."""

from datetime import date
from importlib.metadata import entry_points

import basis.job as etl
from basis.job.testing import frame, inputs

from jobs.security_master import job

SECURITY = {"security_id": "S1", "isin": "US0000000001", "name": "One", "currency": "USD", "coupon": 0.05, "maturity_date": date(2030, 1, 15), "active": True}


def securities(*rows: dict[str, object]) -> job.Inputs:
    row = job.Inputs.declared()["securities"].row
    return inputs(job.Inputs, securities=frame(row, *rows))


def test_it_is_listed_as_a_job() -> None:
    listed = {point.name: point.value for point in entry_points(group="basis.jobs")}
    assert listed.get("security_master") == "jobs.security_master.job"


def test_it_reads_the_whole_table_from_the_source() -> None:
    query = job.Inputs.declared()["securities"]
    assert isinstance(query, etl.Query)
    assert query.database == "source"
    sql = query.text().lower()
    assert "securities" in sql
    assert "interval_start" not in sql
    assert set(query.row.names()) == {"security_id", "isin", "name", "currency", "coupon", "maturity_date", "active"}


def test_it_replaces_the_master_in_the_warehouse() -> None:
    target = job.Outputs.declared()["master"].target
    assert target.database == "warehouse"
    assert target.table.removeprefix("dbo.") == "security_master"
    assert isinstance(target.write, etl.Replace)
    assert tuple(target.key or ()) == ("security_id",)
    assert set(target.row.names()) == {"security_id", "isin", "name", "currency", "coupon", "maturity_date"}


def test_it_writes_only_the_active_securities_and_keeps_a_null_coupon() -> None:
    given = securities(
        SECURITY, SECURITY | {"security_id": "S2", "isin": "US0000000002", "active": False}, SECURITY | {"security_id": "S3", "isin": "US0000000003", "coupon": None, "maturity_date": None}
    )
    master = job.transform(given, job.Params()).master.sort_values("security_id")
    assert master["security_id"].tolist() == ["S1", "S3"]
    assert master["coupon"].isna().tolist() == [False, True]
    assert job.check(given, job.transform(given, job.Params()), job.Params()) == []


def test_it_refuses_two_active_securities_with_one_isin() -> None:
    given = securities(SECURITY, SECURITY | {"security_id": "S2"})
    assert job.check(given, job.transform(given, job.Params()), job.Params()) != []


def test_an_inactive_security_may_share_an_isin() -> None:
    given = securities(SECURITY, SECURITY | {"security_id": "S2", "active": False})
    assert job.check(given, job.transform(given, job.Params()), job.Params()) == []
