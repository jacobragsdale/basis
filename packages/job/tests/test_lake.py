"""A job that archives its rows to a lake: only `basis run --publish` archives, once its databases commit, the run's
interval's partition written under a hidden name, read back, recorded in the ledger and renamed into place; a rerun
for the interval archives nothing unless told `--again`; and `basis resume` archives what a run could not. The lake
here is a folder, through pyarrow's local filesystem, whose rename is atomic as ADLS Gen2's is with its hierarchical
namespace; the integration tier runs the same calls against Azurite."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq
import pytest
from builders import DAY, Price, data_folder, params_file, prices
from pydantic import ValidationError

import basis.job as etl
from basis.core import fakes, lake
from basis.core.errors import Unavailable
from basis.core.interval import Interval
from basis.core.settings import GivenSettings, LakeSettings, SettingsError
from basis.core.storage import Root
from basis.job import runs
from basis.job.job import Job

WAREHOUSE = {
    "kind": "mssql", "connection_string": "Server=sql01;Database=Trading", "isolation": "snapshot", "timeout_s": 5, "max_rows": 5,
    "write": {"connection_string": "Server=sql01;UID=etl", "ledger": "etl.publishes"},
}  # fmt: skip
SETTINGS = GivenSettings.model_validate({"databases": {"warehouse": WAREHOUSE}})
PARTITION = "archive/prices/date=2026-11-05"


def clock() -> datetime:
    return datetime(2026, 11, 6, 6, 0, tzinfo=UTC)


class Params(etl.Params):
    scale: float = 1.0


class Inputs(etl.Inputs):
    prices = etl.File(Price, path="prices.parquet", empty="fail")


class Outputs(etl.Outputs):
    archived = etl.Lake(Price, lake="history", table="prices", partition="date", ledger="warehouse")


def transform(inputs: Inputs, params: Params) -> Outputs:
    return Outputs(archived=inputs.prices.assign(price=inputs.prices["price"] * params.scale))


JOB = Job("archive", "tests.archive", Params, Inputs, Outputs, transform, None, Path(__file__).parent)


@dataclass
class World:
    """What a run of the job reaches: the warehouse's ledger and the lake, a folder holding the container `archive`."""

    root: Path
    ledger: fakes.Database = field(default_factory=lambda: fakes.Database("warehouse"))
    reachable: bool = True

    def __post_init__(self) -> None:
        (self.root / "lake" / "archive").mkdir(parents=True)

    @property
    def store(self) -> lake.Lake:
        return lake.Lake("history", "archive", pafs.SubTreeFileSystem(str(self.root / "lake"), pafs.LocalFileSystem()))

    def open_lake(self, name: str) -> lake.Lake:
        assert name == "history"
        if not self.reachable:  # a lake whose container cannot be found, as one that cannot be reached
            return lake.Lake("history", "gone", self.store.fs)
        return self.store

    def run(self, *, publish: bool = True, again: bool = False, scale: float = 1.0, interval: Interval | None = DAY) -> runs.Result:
        folder = data_folder(self.root, prices({}, {"security_id": "S2", "price": 1134.5}))
        request = runs.Request(params_file(self.root, json.dumps({"scale": scale})), interval, folder, publish=publish, again=again)
        return runs.run(JOB, SETTINGS, request, snapshot_root=Root.at(self.root / "snapshots"), clock=clock, databases=lambda _: self.ledger, open_lake=self.open_lake)

    def partition(self) -> list[float]:
        return sorted(pq.read_table(self.root / "lake" / PARTITION).column("price").to_pylist())

    def hidden(self) -> list[str]:
        return sorted(path.name for path in (self.root / "lake" / "archive" / "prices").iterdir() if path.name.startswith("."))


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_a_published_run_archives_its_interval_s_partition_checked_and_recorded_in_the_ledger(world: World) -> None:
    result = world.run()
    archived = result.record.archived["archived"]
    assert (result.record.status, archived.status, archived.rows, archived.path) == ("published", "archived", 2, PARTITION)
    assert result.record.archives["archived"].partition == "date=2026-11-05"
    assert world.partition() == [100.0, 1134.5]
    assert world.hidden() == []
    [entry] = world.ledger.entries
    file = world.root / "lake" / PARTITION / "part-0.parquet"
    detail = json.loads(entry.detail or "{}")
    assert (entry.output, entry.table, entry.interval, entry.counts.inserted) == ("archived", "lake:history:prices", DAY, 2)
    assert detail == {"path": PARTITION, "rows": 2, "files": [{"name": "part-0.parquet", "sha256": hashlib.sha256(file.read_bytes()).hexdigest(), "rows": 2}]}
    assert [file.model_dump() for file in archived.files] == detail["files"]


def test_a_run_without_publish_archives_nothing(world: World) -> None:
    result = world.run(publish=False)
    assert (result.record.status, result.record.archived, world.ledger.entries) == ("checked", {}, [])
    assert result.record.archives["archived"].rows == 2
    assert not (world.root / "lake" / PARTITION).exists()


def test_a_rerun_for_the_same_interval_archives_nothing_unless_told_again(world: World) -> None:
    first = world.run()
    second = world.run(scale=2.0)
    assert (second.record.status, second.record.archived["archived"].status) == ("published", "skipped")
    assert f"runs {first.record.run_id} archived archived" in (second.record.archived["archived"].reason or "")
    assert world.partition() == [100.0, 1134.5]
    again = world.run(scale=2.0, again=True)
    assert (again.record.status, again.record.archived["archived"].status) == ("published", "archived")
    assert world.partition() == [200.0, 2269.0]
    assert (len(world.ledger.entries), world.hidden()) == (2, [])


def test_an_archive_that_fails_leaves_the_run_partial_and_its_resume_archives_it_once(world: World) -> None:
    world.reachable = False
    result = world.run()
    assert (result.record.status, result.record.archived["archived"].status) == ("partial", "failed")
    assert result.snapshot is not None
    reason, fix = runs.failure_of(result.record, publish=True) or ("", "")
    assert reason.startswith(f"run {result.record.run_id} of archive published, and could not archive archived: LakeError: the lake history has no container gone")
    assert fix == f"fix it, then `basis resume archive {result.record.run_id}`, which archives what it has not"
    world.reachable = True
    resumed = runs.resume(JOB, result.snapshot, SETTINGS, clock=clock, databases=lambda _: world.ledger, open_lake=world.open_lake)
    assert (resumed.status, resumed.archived["archived"].status, world.partition()) == ("published", "archived", [100.0, 1134.5])
    assert runs.resume(JOB, result.snapshot, SETTINGS, clock=clock, databases=lambda _: world.ledger, open_lake=world.open_lake).status == "published"
    assert len(world.ledger.entries) == 1


def test_a_partition_renamed_in_by_an_attempt_that_died_before_its_commit_is_recorded_by_the_retry(world: World) -> None:
    world.ledger.fails(Unavailable, on="commit", times=1)  # the connection dropped at the commit
    died = world.run()
    assert (died.record.status, world.ledger.entries, world.partition()) == ("partial", [], [100.0, 1134.5])
    assert died.snapshot is not None
    resumed = runs.resume(JOB, died.snapshot, SETTINGS, clock=clock, databases=lambda _: world.ledger, open_lake=world.open_lake)
    assert (resumed.status, resumed.archived["archived"].rows, len(world.ledger.entries)) == ("published", 2, 1)
    assert world.hidden() == []


def test_a_partition_no_run_recorded_with_other_rows_is_refused_and_left_alone(world: World) -> None:
    stranger = world.root / "lake" / PARTITION
    stranger.mkdir(parents=True)
    pq.write_table(pa.Table.from_pandas(prices({}), preserve_index=False), stranger / "part-0.parquet")
    result = world.run()
    archived = result.record.archived["archived"]
    assert (result.record.status, archived.status) == ("partial", "failed")
    assert f"the partition {PARTITION} is there already, and the ledger has no run that archived it" in (archived.error or "")
    assert (world.partition(), world.ledger.entries, world.hidden()) == ([100.0], [], [])


def test_a_container_that_is_not_there_is_named(world: World) -> None:
    (world.root / "lake" / "archive").rmdir()
    archived = world.run().record.archived["archived"]
    assert "the lake history has no container archive: create it, or correct BASIS_LAKES__HISTORY__CONTAINER" in (archived.error or "")


def test_an_archive_needs_its_run_s_interval_and_a_date_partition_one_day(world: World) -> None:
    assert world.run(interval=None).record.reasons == ["archived: an archive is its run's interval's partition of prices, and this run has none: give it one"]
    [week] = world.run(interval=Interval.parse("2026-11-02..2026-11-09")).record.reasons
    assert week == "archived: the interval 2026-11-02..2026-11-09 is not one UTC day, midnight to midnight, and the archive is partitioned by date: run it for one day, or declare partition='interval'"
    assert world.ledger.entries == []


def test_an_interval_partition_names_both_ends() -> None:
    assert lake.partition("interval", Interval.parse("2026-11-01..2026-12-01")) == "start=20261101T000000Z/end=20261201T000000Z"
    assert lake.partition("date", DAY) == "date=2026-11-05"


def test_a_publishing_run_whose_lake_or_ledger_is_not_configured_is_refused_before_it_reads(tmp_path: Path) -> None:
    request = runs.Request(params_file(tmp_path), DAY, data_folder(tmp_path, prices({})), publish=True)
    record = runs.run(JOB, GivenSettings(), request, snapshot_root=Root.at(tmp_path / "snapshots"), clock=clock).record
    assert record.status == "failed"
    assert record.inputs == {}
    assert record.reasons == [
        (
            "archived: the lake history is not configured, and the run archives to it: set BASIS_LAKES__HISTORY__ACCOUNT, BASIS_LAKES__HISTORY__CONTAINER, "
            "BASIS_LAKES__HISTORY__AUTH, as .env.example shows"
        ),
        "archived: database 'warehouse', whose ledger records it, is not configured: set BASIS_DATABASES__WAREHOUSE__*",
    ]


@pytest.mark.parametrize(
    ("given", "says"),
    [
        ({"auth": "key"}, "a lake signed in to by key needs secret"),
        ({"auth": "cli", "secret": "x"}, "a lake signed in to by cli takes no secret"),
        ({"auth": "workload_identity", "emulator": "127.0.0.1:10000"}, "an emulator is signed in to with key or sas alone"),
        ({"auth": "key", "secret": "x", "account": "Not-An-Account"}, "account"),
        ({"auth": "key", "secret": "x", "container": "-no"}, "container"),
    ],
)
def test_a_lake_s_settings_say_how_it_signs_in(given: dict[str, str], says: str) -> None:
    with pytest.raises(ValidationError, match=says):
        LakeSettings.model_validate({"account": "lakeacct", "container": "archive"} | given)


def test_a_lake_is_built_from_its_settings_and_never_azure_s_default_chain() -> None:
    sas = LakeSettings.model_validate({"account": "lakeacct", "container": "archive", "auth": "sas", "secret": "sv=2026&sig=x", "emulator": "127.0.0.1:1"})
    assert type(lake.filesystem(sas)).__name__ == "AzureFileSystem"
    with pytest.raises(SettingsError, match="the lake other is not configured"):
        lake.lake("other", GivenSettings())
