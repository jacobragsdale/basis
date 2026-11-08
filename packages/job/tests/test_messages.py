"""A job's messages: a Slack channel's and an email, which only `basis run --publish` sends, once its databases have
committed, through the fakes in a test, each recorded in its ledger; and which a run without `--publish`, a replay, a
rerun for the same interval and the QA tier never send."""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from builders import DAY, Price, data_folder, params_file, prices

import basis.job as etl
from basis.core import fakes
from basis.core.databases import LedgerEntry
from basis.core.display import text
from basis.core.errors import Conflict, Unavailable
from basis.core.interval import Interval
from basis.core.settings import GivenSettings
from basis.core.storage import Root
from basis.core.verbs import Qa
from basis.job import qa, runs
from basis.job.job import Job

WAREHOUSE = {
    "kind": "mssql", "connection_string": "Server=sql01;Database=Trading", "isolation": "snapshot", "timeout_s": 5, "max_rows": 5,
    "write": {"connection_string": "Server=sql01;UID=etl", "ledger": "etl.publishes"},
}  # fmt: skip
SETTINGS = GivenSettings.model_validate({"databases": {"warehouse": WAREHOUSE}})


def clock() -> datetime:
    return datetime(2026, 11, 6, 6, 0, tzinfo=UTC)


class Params(etl.Params):
    quiet: bool = False
    """Send no Slack message."""


class Inputs(etl.Inputs):
    prices = etl.File(Price, path="prices.parquet", empty="fail")


class Outputs(etl.Outputs):
    alert = etl.Slack("desk", ledger="warehouse", deadline_s=5)
    report = etl.Email("risk", ledger="warehouse", deadline_s=5)


def transform(inputs: Inputs, params: Params) -> Outputs:
    total = text(float(inputs.prices["price"].sum()), {"kind": "money", "currency": "USD"})
    report = etl.Mail(subject="prices for 2026-11-05", text=f"{len(inputs.prices)} prices, worth {total}")
    return Outputs(alert=None if params.quiet else f"*prices*: {total}", report=report)


JOB = Job("alerts", "tests.alerts", Params, Inputs, Outputs, transform, None, Path(__file__).parent)


@dataclass
class Ledger:
    """The warehouse, as far as a message reaches it: its ledger, keyed by run and output as the real one is."""

    name: str = "warehouse"
    rows: list[LedgerEntry] = field(default_factory=list)

    @contextmanager
    def writing(self) -> Generator[Ledger.Writer]:
        yield Ledger.Writer(self)

    @dataclass
    class Writer:
        ledger: Ledger
        pending: list[LedgerEntry] = field(default_factory=list)

        def overlapping(self, job: str, output: str, interval: Interval) -> list[str]:
            found = [(row.run_id, row.interval) for row in self.ledger.rows if (row.job, row.output) == (job, output)]
            return sorted({run for run, at in found if at is not None and at.start < interval.end and at.end > interval.start})

        def record(self, entry: LedgerEntry) -> None:
            if any((row.run_id, row.output) == (entry.run_id, entry.output) for row in self.ledger.rows):
                detail = "the ledger has the run's output"
                raise Conflict(detail, resource="warehouse")
            self.pending.append(entry)

        def commit(self) -> None:
            self.ledger.rows += self.pending


@dataclass
class World:
    """What a run of the job reaches: the warehouse's ledger, the desk's channel and the risk team's relay."""

    root: Path
    ledger: Ledger = field(default_factory=Ledger)
    desk: fakes.Slack = field(default_factory=fakes.Slack)
    risk: fakes.Email = field(default_factory=fakes.Email)

    def run(self, *, publish: bool = True, again: bool = False, params: str = "{}") -> runs.Result:
        folder = data_folder(self.root, prices({}, {"security_id": "S2", "price": 1134.5}))
        request = runs.Request(params_file(self.root, params), DAY, folder, publish=publish, again=again)
        faked: dict[str, fakes.Fake] = {"desk": self.desk, "risk": self.risk}
        return runs.run(JOB, SETTINGS, request, snapshot_root=Root.at(self.root / "snapshots"), clock=clock, databases=lambda _: self.ledger, faked=faked)  # ty: ignore[invalid-argument-type] -- the ledger, all a message reaches of a database


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_a_published_run_sends_each_message_through_its_fake_and_records_each_in_the_ledger(world: World) -> None:
    result = world.run()
    assert result.record.status == "published"
    assert world.desk.posted == ["*prices*: $1,234.50"]
    [mail] = world.risk.sent
    assert (mail["Subject"], mail.get_content().strip()) == ("prices for 2026-11-05", "2 prices, worth $1,234.50")
    assert {name: entry.status for name, entry in result.record.sent.items()} == {"alert": "sent", "report": "sent"}
    assert sorted((row.output, row.table) for row in world.ledger.rows) == [("alert", "slack:desk"), ("report", "email:risk")]
    assert result.record.messages["alert"].text == "*prices*: $1,234.50"


def test_a_run_without_publish_and_its_replay_send_nothing(world: World) -> None:
    result = world.run(publish=False)
    assert (result.record.status, sorted(result.record.messages), result.record.sent) == ("checked", ["alert", "report"], {})
    assert result.snapshot is not None
    replayed, _ = runs.replay(JOB, result.snapshot, snapshot_root=Root.at(world.root / "snapshots"), clock=clock)
    assert replayed.record.status == "checked"
    assert (world.desk.posted, world.risk.sent, world.ledger.rows) == ([], [], [])


def test_a_rerun_for_the_same_interval_sends_nothing_again_unless_told_again(world: World) -> None:
    world.run()
    rerun = world.run()
    assert {name: entry.status for name, entry in rerun.record.sent.items()} == {"alert": "skipped", "report": "skipped"}
    assert rerun.record.status == "published"
    assert len(world.desk.posted) == 1
    world.run(again=True)
    assert len(world.desk.posted) == 2


def test_a_message_the_transform_gives_none_is_not_sent(world: World) -> None:
    result = world.run(params='{"quiet": true}')
    assert (sorted(result.record.messages), world.desk.posted, len(world.risk.sent)) == (["report"], [], 1)


def test_a_message_that_fails_leaves_the_run_partial_and_its_resume_sends_it_once(world: World) -> None:
    world.desk.fails(Unavailable, times=1)
    result = world.run()
    assert (result.record.status, result.record.sent["alert"].status, result.record.sent["report"].status) == ("partial", "failed", "sent")
    reason, fix = runs.failure_of(result.record, publish=True) or ("", "")
    assert reason.startswith(f"run {result.record.run_id} of alerts published, and could not send alert: Unavailable: desk's webhook answered 503")
    assert fix == f"fix it, then `basis resume alerts {result.record.run_id}`, which sends what it has not"
    assert result.snapshot is not None
    faked: dict[str, fakes.Fake] = {"desk": world.desk, "risk": world.risk}
    resumed = runs.resume(JOB, result.snapshot, SETTINGS, clock=clock, databases=lambda _: world.ledger, faked=faked)  # ty: ignore[invalid-argument-type] -- the ledger alone
    assert (resumed.status, world.desk.posted, len(world.risk.sent)) == ("published", ["*prices*: $1,234.50"], 1)
    assert runs.resume(JOB, result.snapshot, SETTINGS, clock=clock, databases=lambda _: world.ledger, faked=faked).status == "published"  # ty: ignore[invalid-argument-type] -- the ledger alone
    assert len(world.desk.posted) == 1


def test_a_publishing_run_whose_messages_go_nowhere_configured_is_refused_before_it_reads(tmp_path: Path) -> None:
    folder = data_folder(tmp_path, prices({}))
    request = runs.Request(params_file(tmp_path), DAY, folder, publish=True)
    result = runs.run(JOB, GivenSettings(), request, snapshot_root=Root.at(tmp_path / "snapshots"), clock=clock)
    assert result.record.status == "failed"
    assert result.record.reasons == [
        "alert: database 'warehouse', whose ledger records it, is not configured: set BASIS_DATABASES__WAREHOUSE__*",
        "alert: the slack 'desk' it sends to is not configured: set BASIS_SLACK__DESK__WEBHOOK, as .env.example shows",
        "report: database 'warehouse', whose ledger records it, is not configured: set BASIS_DATABASES__WAREHOUSE__*",
        "report: the email 'risk' it sends to is not configured: set BASIS_EMAIL__RISK__*, as .env.example shows",
    ]


@pytest.mark.parametrize(
    ("value", "says"),
    [({"alert": 3, "report": None}, "alert posts to Slack, so its value is its text or None, not a int"), ({"alert": None, "report": "text"}, "report is an email, so its value is an etl.Mail")],
)
def test_a_message_s_value_must_be_its_kind(value: dict[str, object], says: str) -> None:
    with pytest.raises(TypeError, match=says):
        Outputs(**value)  # ty: ignore[invalid-argument-type] -- each case's value is the wrong kind on purpose


def test_the_qa_tier_runs_a_job_s_messages_through_fakes_so_it_sends_nothing(tmp_path: Path) -> None:
    """QA's settings configure no channel and no email, which a publishing run refuses unless it is given fakes."""
    data_folder(tmp_path, prices({}))

    class QaInputs(etl.Inputs):
        prices = etl.File(Price, path=str(tmp_path / "data" / "prices.parquet"), empty="fail")

    job = Job("alerts", "tests.alerts", Params, QaInputs, Outputs, transform, None, Path(__file__).parent)
    (tmp_path / "params.json").write_text("{}", encoding="utf-8")
    ledger = Ledger()
    settings = GivenSettings.model_validate({"snapshot_root": "runs-qa", "databases": {"warehouse": WAREHOUSE}})
    declared = {"alerts": {"params": "params.json", "interval": "2026-11-05..2026-11-06"}}
    opened = lambda _: ledger  # noqa: E731 -- the QA tier's databases, which a message reaches only the ledger of
    context = Qa(tmp_path, "C1", settings, Root.at(tmp_path / "runs-qa"), {"warehouse": frozenset({"etl.publishes"})}, declared, opened)  # ty: ignore[invalid-argument-type] -- the ledger alone
    planned = qa.plan(context, listed=lambda: {"alerts": job.module}, load=lambda _: job)
    assert planned.problems == []
    [ran] = planned.run()
    assert ran.failure is None, ran.failure
    assert sorted(row.output for row in ledger.rows) == ["alert", "report"]  # recorded in QA's ledger, and sent to fakes
