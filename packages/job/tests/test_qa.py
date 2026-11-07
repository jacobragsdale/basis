from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from builders import make_job

import basis.job as etl
from basis.core.databases import open_database
from basis.core.settings import GivenSettings
from basis.core.snapshots import RunRecord
from basis.core.storage import Root
from basis.core.verbs import Qa
from basis.job import qa
from basis.job.job import Job

WAREHOUSE: dict[str, object] = {
    "kind": "mssql",
    "connection_string": "Server=sqlqa01;Database=Warehouse",
    "isolation": "read_committed",
    "timeout_s": 5,
    "max_rows": 5,
    "write": {"connection_string": "Server=sqlqa01;Database=Warehouse", "ledger": "etl.publishes"},
}
PRICES = make_job(write=etl.Upsert())


def context(repo: Path, declared: Mapping[str, object], *, writes: Mapping[str, frozenset[str]] | None = None, databases: Mapping[str, object] | None = None) -> Qa:
    settings = GivenSettings.model_validate({"snapshot_root": "runs-qa", "databases": databases if databases is not None else {"warehouse": WAREHOUSE}})
    allowed = writes if writes is not None else {"warehouse": frozenset({"prices", "etl.publishes"})}
    return Qa(repo, "C1", settings, Root.at(repo / "runs-qa"), allowed, declared, lambda name: open_database(settings, name))


def problems(found: Qa, jobs: Mapping[str, Job] | None = None) -> list[str]:
    listed = jobs if jobs is not None else {"prices": PRICES}
    return [reason for reason, _ in qa.plan(found, listed=lambda: {name: job.module for name, job in listed.items()}, load=lambda name: listed[name]).problems]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "params.json").write_text("{}", encoding="utf-8")
    return tmp_path


DECLARED = {"prices": {"params": "params.json", "interval": "2026-11-05..2026-11-06"}}


def test_a_job_declared_with_its_tables_allowed_plans_its_run(repo: Path) -> None:
    assert problems(context(repo, DECLARED)) == []


NO_WRITE = {"warehouse": {key: value for key, value in WAREHOUSE.items() if key != "write"}}


@pytest.mark.parametrize(
    ("declared", "writes", "databases", "problem"),
    [
        ({}, None, None, "the QA tier runs each job, and [tool.basis.qa.job] declares no run of prices"),
        (DECLARED | {"rates": DECLARED["prices"]}, None, None, "[tool.basis.qa.job] declares a QA run of rates, which the repo does not list"),
        ({"prices": {"params": "params.json", "interval": "today"}}, None, None, "[tool.basis.qa.job] prices's interval is not valid"),
        ({"prices": {"params": "missing.json"}}, None, None, "[tool.basis.qa.job] prices's params, missing.json, cannot be read"),
        ({"prices": {"params": "params.json", "publish": True}}, None, None, "[tool.basis.qa.job] prices is not valid: publish: Extra inputs are not permitted"),
        (DECLARED, {"warehouse": frozenset({"etl.publishes"})}, None, "prices writes prices to prices in warehouse, which [tool.basis.qa] writes does not list"),
        (DECLARED, None, {}, "prices writes prices to database 'warehouse', which the QA settings do not configure"),
        (DECLARED, None, NO_WRITE, "prices writes prices to database 'warehouse', which the QA settings give no write block"),
    ],
    ids=["undeclared", "unknown", "a-bad-interval", "no-params-file", "a-key-basis-run-does-not-take", "a-table-not-allowed", "no-database", "no-write-block"],
)
def test_a_run_that_is_not_declared_or_not_safe_is_refused(
    repo: Path, declared: Mapping[str, object], writes: Mapping[str, frozenset[str]] | None, databases: Mapping[str, object] | None, problem: str
) -> None:
    found = problems(context(repo, declared, writes=writes, databases=databases))
    assert any(reason.startswith(problem) for reason in found), found


def test_a_published_run_says_what_it_wrote_to_each_table() -> None:
    record = RunRecord.model_validate(
        {
            "run_id": "R1", "job": "prices", "module": "tests.prices", "version": "0.0.0", "started_at": datetime(2026, 11, 7, tzinfo=UTC), "params_path": "p",
            "params_sha256": "0", "status": "published", "outputs": {"prices": {"database": "warehouse", "table": "dbo.prices", "write": "Upsert"}},
            "publishes": {"warehouse": {"status": "committed", "at": datetime(2026, 11, 7, tzinfo=UTC), "counts": {"prices": {"deleted": 0, "upserted": 3, "inserted_new": 0, "inserted": 0}}}},
        }
    )  # fmt: skip
    assert qa.summary(record) == "published: dbo.prices (3 upserted)"
