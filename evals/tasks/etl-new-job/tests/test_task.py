"""The `security_master` job from the task's spec: listed, declared as asked, with contract tests, and run by the
framework on fixtures of its own that it writes as asked and refuses when two active securities share an ISIN."""

import importlib.util
import json
from importlib.metadata import entry_points
from pathlib import Path

import etl_framework as etl
import pytest
from etl_framework import job as jobs
from etl_framework.testing import ContractError, ContractTests, check_fixture
from jobs.security_master import job

HEADER = "security_id,isin,name,currency,coupon,maturity_date,active"
ALPHA = "XS0001,XS0000000011,Alpha 4.25% 2031,USD,4.250000,2031-06-15,true"
BETA = "XS0002,XS0000000029,Beta FRN 2029,EUR,,2029-03-01,true"
GAMMA = "XS0003,XS0000000037,Gamma Perpetual,GBP,6.125000,,true"
OLD = "XS0004,XS0000000045,Delta 2.5% 2026,USD,2.500000,2026-10-01,false"


def run(tmp_path: Path, *lines: str) -> etl.Changes:
    """The job run by the framework on a fixture of `lines`, with its default params; ContractError if it refuses."""
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "securities.csv").write_text("\n".join([HEADER, *lines]) + "\n", encoding="utf-8")
    (fixture / "fixture.json").write_text(json.dumps({"interval": None}), encoding="utf-8")
    contract = jobs.read(job)
    result = check_fixture(contract, fixture, contract.directory / "params/default.json", tmp_path / "snapshots")
    changes = result.changes("master")
    assert changes is not None
    return changes


def test_it_is_listed_as_a_job() -> None:
    listed = {point.name: point.value for point in entry_points(group="etl_framework.jobs")}
    assert listed.get("security_master") == "jobs.security_master.job"


def test_it_reads_securities_from_source() -> None:
    query = job.Inputs.declared()["securities"]
    assert isinstance(query, etl.Query)
    assert query.database == "source"


def test_it_replaces_the_security_master_table_keyed_by_security_id() -> None:
    target = job.Outputs.declared()["master"].target
    assert (target.database, target.table.removeprefix("dbo.")) == ("warehouse", "security_master")
    assert isinstance(target.write, etl.Replace)
    assert target.key == ("security_id",)


def test_it_writes_every_column_but_active_with_the_spec_types() -> None:
    row = job.Outputs.declared()["master"].target.row
    assert set(row.names()) == {"security_id", "isin", "name", "currency", "coupon", "maturity_date"}
    coupon = row.column("coupon")
    assert (coupon.kind, coupon.precision, coupon.scale, coupon.nullable) == ("decimal", 9, 6, True)
    assert row.column("maturity_date").nullable
    assert not row.column("isin").nullable


def test_it_has_contract_tests() -> None:
    found = []
    for path in (Path(job.__file__).parent / "tests").glob("test_*.py"):
        spec = importlib.util.spec_from_file_location(f"eval_{path.stem}", path)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        found += [value for value in vars(module).values() if isinstance(value, type) and issubclass(value, ContractTests) and getattr(value, "job", None) is job]
    assert found


def test_a_run_writes_only_the_active_securities(tmp_path: Path) -> None:
    changes = run(tmp_path, GAMMA, OLD, ALPHA, BETA)
    assert changes.insert is not None
    written = changes.insert.sort("security_id")
    assert written.get_column("security_id").to_list() == ["XS0001", "XS0002", "XS0003"]
    assert written.get_column("coupon").is_null().to_list() == [False, True, False]
    assert written.get_column("maturity_date").is_null().to_list() == [False, False, True]


def test_a_run_replaces_the_whole_table(tmp_path: Path) -> None:
    changes = run(tmp_path, ALPHA)
    assert changes.delete is not None
    assert changes.delete.within is None
    assert changes.delete.matching is None


def test_two_active_securities_with_one_isin_stop_the_run(tmp_path: Path) -> None:
    with pytest.raises(ContractError):
        run(tmp_path, ALPHA, BETA.replace("XS0000000029", "XS0000000011"))


def test_an_inactive_security_may_share_an_active_ones_isin(tmp_path: Path) -> None:
    run(tmp_path, ALPHA, OLD.replace("XS0000000045", "XS0000000011"))
