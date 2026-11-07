"""The QA tier against a real SQL Server standing in for QA: a job's QA run publishes, through the tier's guarded
databases, to the tables `[tool.basis.qa] writes` lists, and a write to any other table is refused inside the
transaction, before it writes, so nothing commits."""

import uuid
from collections.abc import Iterator
from decimal import Decimal
from functools import partial
from pathlib import Path

import pytest
from builders import Price
from integration.scratch import Scratch, configured, mssql

import basis.job as etl
from basis.core import qa, snapshots
from basis.core.settings import read
from basis.core.storage import Root
from basis.core.verbs import Part
from basis.job import ddl, runs
from basis.job import qa as job_qa
from basis.job.job import Job
from basis.job.outputs import Target

pytestmark = pytest.mark.integration


@pytest.fixture
def scratch() -> Iterator[Scratch]:
    suffix = uuid.uuid4().hex[:8]
    found = mssql(configured(), suffix).with_ledger()
    yield found
    found.drop(found.ledger(), f"prices_{suffix}", f"other_{suffix}", f"source_{suffix}")


def price_job(sql: Path, table: str) -> Job:
    """A job that copies the scratch source table's prices to `table`."""

    class Inputs(etl.Inputs):
        prices = etl.Query(Price, database="warehouse", sql=str(sql), empty="fail")

    class Outputs(etl.Outputs):
        prices = etl.Table(Price, database="warehouse", table=table, write=etl.Upsert(), key=("day", "security_id"))

    def transform(inputs: Inputs, _params: etl.Params) -> Outputs:
        return Outputs(prices=inputs.prices)

    return Job("prices", "tests.prices_qa", etl.Params, Inputs, Outputs, transform, None, Path(__file__).parent)


@pytest.fixture
def repo(tmp_path: Path, scratch: Scratch) -> Path:
    """A repo that declares a QA tier whose marker is in the scratch database's name, with its settings file and a
    source table of two prices."""
    suffix = scratch.suffix
    for table in (f"prices_{suffix}", f"other_{suffix}", f"source_{suffix}"):
        scratch.execute(ddl.create_table(Target(Price, "warehouse", table, etl.Append(), ("day", "security_id")), "mssql"))
    scratch.execute(
        f"INSERT INTO source_{suffix} (day, security_id, price) VALUES ('2026-11-05', 'S1', 100.5)", f"INSERT INTO source_{suffix} (day, security_id, price) VALUES ('2026-11-05', 'S2', 7.25)"
    )
    (tmp_path / "source.sql").write_text(f"SELECT day, security_id, price, note, booked_at FROM source_{suffix}", encoding="utf-8")
    (tmp_path / "params.json").write_text("{}", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.basis]\nparts = ["job"]\n\n[tool.basis.qa]\nsettings = ".env.qa"\nmarker = "test"\nstores = ["runs-test"]\n'
        f'writes = {{ warehouse = ["prices_{suffix}", "{scratch.ledger()}"] }}\n\n'
        f'[tool.basis.qa.job]\nprices = {{ params = "params.json", interval = "2026-11-05..2026-11-06" }}\n',
        encoding="utf-8",
    )
    database = scratch.settings
    write = database["write"]
    assert isinstance(write, dict)
    lines = [f"BASIS_DATABASES__WAREHOUSE__{key.upper()}='{value}'" for key, value in database.items() if key != "write"]
    lines += [f"BASIS_DATABASES__WAREHOUSE__WRITE__{str(key).upper()}='{value}'" for key, value in write.items()]
    (tmp_path / ".env.qa").write_text("\n".join([*lines, "BASIS_SNAPSHOT_ROOT=runs-test", ""]), encoding="utf-8")
    return tmp_path


def test_a_qa_run_publishes_to_the_tables_the_repo_allows_and_saves_in_the_qa_store(repo: Path, scratch: Scratch) -> None:
    job = price_job(repo / "source.sql", f"prices_{scratch.suffix}")
    plan = partial(job_qa.plan, listed=lambda: {"prices": job.module}, load=lambda _: job)
    outcome = qa.tier(repo, {"job": Part(qa=plan)}, commit="C1", folder=repo)
    assert outcome.failures == []
    [(part, run)] = outcome.runs
    assert (part, run.summary) == ("job", f"published: prices_{scratch.suffix} (2 upserted)")
    assert sorted(scratch.rows(f"SELECT security_id, price FROM prices_{scratch.suffix}")) == [("S1", Decimal("100.5000")), ("S2", Decimal("7.2500"))]
    assert run.run_id is not None
    record = snapshots.find(Root.at(repo / "runs-test"), run.run_id).record()
    assert (record.labels, record.git_sha, record.interval) == ({"purpose": "qa"}, "C1", "2026-11-05..2026-11-06")


def test_a_write_to_a_table_the_repo_does_not_allow_is_refused_before_it_writes(repo: Path, scratch: Scratch) -> None:
    declared = qa.declared(repo / "pyproject.toml", {"job": Part(qa=job_qa.plan)})
    assert declared is not None
    context = qa.context(declared, read(repo / ".env.qa"), repo=repo, folder=repo, commit="C1", part="job")
    job = price_job(repo / "source.sql", f"other_{scratch.suffix}")  # a table the plan would refuse, written past it
    request = runs.Request(repo / "params.json", etl.Interval.parse("2026-11-05..2026-11-06"), publish=True)
    result = runs.run(job, context.settings, request, snapshot_root=context.store, databases=context.databases)
    assert result.record.status == "failed"
    [publish] = result.record.publishes.values()
    assert publish.error is not None
    assert publish.error.startswith(f"QaError: the QA tier refused to write other_{scratch.suffix} in warehouse")
    assert scratch.rows(f"SELECT COUNT(*) FROM other_{scratch.suffix}") == [(0,)]
    assert scratch.rows(f"SELECT COUNT(*) FROM {scratch.ledger()}") == [(0,)]
