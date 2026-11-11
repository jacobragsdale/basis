"""Orders: only an accepted portfolio's, saved in every run's snapshot, and published only by a run asked to, through
basis-core's publishing: one insert and a ledger row in one transaction, after the snapshot is saved. A publish that
fails writes nothing and `resume` finishes it; neither a resume nor a retry writes a run's orders twice, and a second
run of a model for a date is refused unless it is given `--again`."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.fs as pafs
import pytest
from fakes import LEDGER, ORDERS_SPEC, ORDERS_TABLE, Basis, Clock, ReadsRecorded, Rows, loader, publishing_to, saved, trading
from pydantic import ValidationError

from basis.core import fakes
from basis.core.ddl import ledger
from basis.core.destination import DestColumn, TableSpec
from basis.core.errors import BasisError
from basis.core.interval import Interval
from basis.core.snapshots import PublishRecord
from basis.core.storage import Root
from optim import models
from optim.datasets import HOLDINGS, SECURITIES
from optim.orders import Orders, OrdersRefusedError, OrdersSettings, Table
from optim.runs import Run, resume_run, run_model
from optim.testing import AS_OF, CASH, frame

TOY = models.load("toys.basic")
MARKET = frame(SECURITIES, CASH, {"security_id": "A"})


def toy_run(tmp_path: Path, orders: Orders | None, *portfolios: str, again: bool = False, as_of: datetime | None = None, **values: object) -> Run:
    """A run of the toy, which buys 1,000 A in every portfolio it decides, publishing to `orders` if given."""
    (tmp_path / "params.json").write_text(json.dumps(values))
    source = Rows({HOLDINGS: frame(HOLDINGS, *({"portfolio_id": portfolio} for portfolio in portfolios)), SECURITIES: MARKET})
    as_of_date = as_of.date() if as_of is not None else AS_OF
    return run_model(loader(source), Root.at(tmp_path / "snapshots"), TOY, tmp_path / "params.json", as_of_date=as_of_date, portfolio_ids=list(portfolios), orders=orders, again=again, clock=Clock())


def written(database: fakes.Database) -> list[tuple[object, ...]]:
    return [(order["run_id"], order["portfolio_id"], order["security_id"], order["side"], order["quantity"]) for order in database.rows(ORDERS_TABLE)]


def test_a_run_publishes_its_accepted_portfolios_orders_with_a_ledger_row(tmp_path: Path) -> None:
    books = trading()

    ran = toy_run(tmp_path, publishing_to(books), "P1", "P2", unsolved=["P2"])

    assert written(books) == [(ran.run_id, "P1", "A", "buy", 1000.0)]  # P2 was rejected
    [entry] = books.entries
    assert (entry.run_id, entry.job, entry.output, entry.table, entry.interval, entry.rerun_safe, entry.counts.inserted) == (
        ran.run_id, "optim.toys.basic", "orders", "trading.orders", Interval(datetime(2026, 10, 16, tzinfo=UTC), datetime(2026, 10, 17, tzinfo=UTC)), False, 1,
    )  # fmt: skip
    info = saved(Root.at(tmp_path / "snapshots"), ran.run_id).info
    assert info.publishing is not None
    assert (info.publishing.database, info.publishing.table, info.publishing.again) == ("trading", "trading.orders", False)
    assert {name: (record.status, record.counts) for name, record in info.publishes.items()} == {"trading": ("committed", {"orders": {"deleted": 0, "upserted": 0, "inserted_new": 0, "inserted": 1}})}
    assert ran.committed


def test_a_publish_that_fails_partway_writes_nothing_and_resume_writes_each_order_once(tmp_path: Path) -> None:
    books = trading(fails=True)
    orders = publishing_to(books)

    ran = toy_run(tmp_path, orders, "P1", "P2", "P3")
    assert (ran.ok, ran.committed) == (True, False)
    assert (books.rows(ORDERS_TABLE), books.entries) == ([], [])
    run = saved(Root.at(tmp_path / "snapshots"), ran.run_id)
    assert {name: record.status for name, record in run.info.publishes.items()} == {"trading": "failed"}
    assert "refused the statement for a constraint" in str(run.info.publishes["trading"].error)

    first = resume_run(run, orders, clock=Clock())
    again = resume_run(saved(Root.at(tmp_path / "snapshots"), ran.run_id), orders, clock=Clock())

    assert ([record.status for record in first.records.values()], first.earlier) == (["committed"], False)
    assert (again.records, again.earlier) == (first.records, True)
    assert written(books) == [(ran.run_id, portfolio, "A", "buy", 1000.0) for portfolio in ("P1", "P2", "P3")]
    assert len(books.entries) == 1
    assert saved(Root.at(tmp_path / "snapshots"), ran.run_id).info.publishes == first.records


def test_a_run_that_committed_before_its_record_was_saved_resumes_with_what_that_commit_counted(tmp_path: Path) -> None:
    """As when the process is stopped between the commit and saving the manifest: the ledger holds the run, and what
    the database counted when it committed it, which the resume reports and saves rather than counts of nothing."""
    books = trading()
    orders = publishing_to(books)
    ran = toy_run(tmp_path, orders, "P1", "P2")
    stopped = saved(Root.at(tmp_path / "snapshots"), ran.run_id).save_publishes({})

    resumed = resume_run(stopped, orders, clock=Clock())

    assert resumed.earlier
    assert resumed.records == {"trading": PublishRecord(status="committed", at=books.entries[0].published_at, counts={"orders": {"deleted": 0, "upserted": 0, "inserted_new": 0, "inserted": 2}})}
    assert saved(Root.at(tmp_path / "snapshots"), ran.run_id).info.publishes == resumed.records
    assert written(books) == [(ran.run_id, portfolio, "A", "buy", 1000.0) for portfolio in ("P1", "P2")]
    assert len(books.entries) == 1


def test_a_run_publishes_the_orders_it_decided_without_reading_its_snapshot_back(tmp_path: Path) -> None:
    books, store = trading(), ReadsRecorded()
    (tmp_path / "params.json").write_text("{}")
    root = Root(pafs.PyFileSystem(store), (tmp_path / "snapshots").as_posix(), "snapshots")
    source = Rows({HOLDINGS: frame(HOLDINGS, {"portfolio_id": "P1"}), SECURITIES: MARKET})

    ran = run_model(loader(source), root, TOY, tmp_path / "params.json", as_of_date=AS_OF, portfolio_ids=["P1"], orders=publishing_to(books), clock=Clock())

    assert written(books) == [(ran.run_id, "P1", "A", "buy", 1000.0)]
    assert store.read == []


def test_a_second_run_of_a_model_for_a_date_is_refused_before_it_loads_unless_told_again(tmp_path: Path) -> None:
    books = trading()
    orders = publishing_to(books)
    first = toy_run(tmp_path, orders, "P1")

    with pytest.raises(OrdersRefusedError, match=rf"runs {first.run_id} of optim.toys.basic already published orders for 2026-10-16: pass --again, with --portfolios naming only the portfolios that"):
        toy_run(tmp_path, orders, "P1", "P2")
    with pytest.raises(OrdersRefusedError, match=rf"portfolios \['P1'\] already have orders for 2026-10-16 from runs {first.run_id} of optim.toys.basic: leave them out of --portfolios"):
        toy_run(tmp_path, orders, "P1", "P2", again=True)
    again = toy_run(tmp_path, orders, "P2", again=True)
    next_day = toy_run(tmp_path, orders, "P1", as_of=datetime(2026, 10, 17, tzinfo=UTC))

    assert [order[:2] for order in written(books)] == [(first.run_id, "P1"), (again.run_id, "P2"), (next_day.run_id, "P1")]
    assert len(list((tmp_path / "snapshots" / "runs" / "optim" / "optim.toys.basic").iterdir())) == 3, "the refused runs saved nothing"


def test_two_repos_models_of_one_name_share_neither_a_ledger_identity_nor_a_store_folder(tmp_path: Path) -> None:
    """The repos alpha and beta each have a model at the same module path, as two repos' `models.income.model` are: a
    run of beta's for a date alpha's published is not refused, and each keeps its runs in its own folder."""
    (tmp_path / "params.json").write_text("{}")
    source = Rows({HOLDINGS: frame(HOLDINGS, {"portfolio_id": "P1"}), SECURITIES: MARKET})
    books, store = trading(), Root.at(tmp_path / "snapshots")
    twins = []
    for repo in ("alpha", "beta"):
        (tmp_path / repo).mkdir()
        (tmp_path / repo / "pyproject.toml").write_text(f'[project]\nname = "{repo}"\n')
        twins.append(replace(TOY, directory=tmp_path / repo / "models" / "income"))

    runs = [run_model(loader(source), store, twin, tmp_path / "params.json", as_of_date=AS_OF, portfolio_ids=["P1"], orders=publishing_to(books), clock=Clock()) for twin in twins]

    assert [run.committed for run in runs] == [True, True]
    assert sorted(path.name for path in (tmp_path / "snapshots" / "runs" / "optim").iterdir()) == ["alpha.toys.basic", "beta.toys.basic"]
    assert [entry.job for entry in books.entries] == ["alpha.toys.basic", "beta.toys.basic"]


def test_again_needs_the_portfolios_it_publishes(tmp_path: Path) -> None:
    """`--again` without `--portfolios` would publish every portfolio a second time."""
    (tmp_path / "params.json").write_text("{}")
    source = Rows({HOLDINGS: frame(HOLDINGS, {"portfolio_id": "P1"}), SECURITIES: MARKET})

    with pytest.raises(OrdersRefusedError, match=r"--again publishes beside another run's orders, and the run names no portfolios"):
        run_model(loader(source), Root.at(tmp_path / "snapshots"), TOY, tmp_path / "params.json", as_of_date=AS_OF, orders=publishing_to(trading()), again=True, clock=Clock())


def test_a_resume_told_again_still_refuses_a_portfolio_another_run_ordered(tmp_path: Path) -> None:
    """A run's publish fails; another run of the date then orders P1; resuming the first with --again, as the refusal
    of its overlap says, would order P1 twice."""
    books = trading(fails=True)
    orders = publishing_to(books)
    stopped = toy_run(tmp_path, orders, "P1")
    other = toy_run(tmp_path, orders, "P1")

    with pytest.raises(OrdersRefusedError, match=rf"portfolios \['P1'\] already have orders for 2026-10-16 from runs {other.run_id} of optim.toys.basic"):
        resume_run(saved(Root.at(tmp_path / "snapshots"), stopped.run_id), orders, again=True, clock=Clock())
    assert [order[0] for order in written(books)] == [other.run_id]


def test_a_run_with_no_orders_publishes_nothing_so_its_retry_is_not_refused(tmp_path: Path) -> None:
    books = trading()
    orders = publishing_to(books)

    rejected = toy_run(tmp_path, orders, "P1", unsolved=["P1"])
    retried = toy_run(tmp_path, orders, "P1")

    assert (rejected.publishes, books.entries[0].run_id) == ({}, retried.run_id)
    assert written(books) == [(retried.run_id, "P1", "A", "buy", 1000.0)]


@pytest.mark.parametrize(
    ("spec", "reason"),
    [
        (None, "trading.orders does not exist in database trading: `basis ddl MODEL --dialect DIALECT` prints a CREATE TABLE for it"),
        (
            TableSpec("mssql", "trading.orders", ORDERS_SPEC.columns | {"quantity": DestColumn("quantity", "NVARCHAR", 20, None, None, nullable=False, generated=False, defaulted=False)}),
            "quantity: a NVARCHAR column cannot hold a row's float values exactly",
        ),
    ],
    ids=["missing", "drifted"],
)
def test_an_orders_table_that_cannot_take_the_orders_stops_the_run_before_it_loads(tmp_path: Path, spec: TableSpec | None, reason: str) -> None:
    with pytest.raises(OrdersRefusedError, match=f"cannot publish its orders to trading.orders in database trading, so it stops before it loads: {reason}"):
        toy_run(tmp_path, publishing_to(trading(spec=spec)), "P1")
    assert not (tmp_path / "snapshots" / "runs").exists()


def test_a_run_that_does_not_publish_saves_its_orders_and_has_none_to_resume(tmp_path: Path) -> None:
    ran = toy_run(tmp_path, None, "P1", "P2", unsolved=["P2"])
    run = saved(Root.at(tmp_path / "snapshots"), ran.run_id)

    assert run.orders().to_dict("records") == [{"portfolio_id": "P1", "security_id": "A", "side": "buy", "quantity": 1000.0}]
    assert (run.info.publishing, run.info.publishes) == (None, {})
    with pytest.raises(BasisError, match=f"run {ran.run_id} was not asked to publish its orders"):
        resume_run(run, publishing_to(trading()))


def test_a_resume_to_another_table_than_the_runs_is_refused(tmp_path: Path) -> None:
    ran = toy_run(tmp_path, publishing_to(trading(fails=True)), "P1")
    elsewhere = Orders(Table("trading", "trading.other_orders"), lambda _name: trading(), "mssql", LEDGER, timeout_s=30)

    with pytest.raises(BasisError, match=r"publishes its orders to trading.orders in database trading, and the settings name trading.other_orders in trading"):
        resume_run(saved(Root.at(tmp_path / "snapshots"), ran.run_id), elsewhere)


def test_a_run_saved_before_its_record_had_publishing_still_reads(tmp_path: Path) -> None:
    ran = toy_run(tmp_path, publishing_to(trading()), "P1")
    run = saved(Root.at(tmp_path / "snapshots"), ran.run_id)
    older = json.loads(run.path.read_bytes())
    del older["publishing"], older["publishes"]
    run.path.write_bytes(json.dumps(older).encode())

    info = saved(Root.at(tmp_path / "snapshots"), ran.run_id).info
    assert (info.publishing, info.publishes) == (None, {})


def test_the_orders_table_names_the_columns_it_has_for_one_it_has_not() -> None:
    with pytest.raises(KeyError, match=r"the orders table has no column 'price'; it has \['run_id', 'portfolio_id', 'security_id', 'side', 'quantity'\]"):
        Table("trading", "trading.orders").column("price")


def test_an_orders_table_that_could_inject_sql_is_refused() -> None:
    with pytest.raises(ValidationError):
        OrdersSettings.model_validate({"database": "trading", "table": "orders; DROP TABLE orders"})


TRADING = {
    "BASIS_DATABASES__TRADING__KIND": "mssql",
    "BASIS_DATABASES__TRADING__CONNECTION_STRING": "Server=nowhere.invalid",
    "BASIS_DATABASES__TRADING__ISOLATION": "snapshot",
    "BASIS_DATABASES__TRADING__TIMEOUT_S": "1",
    "BASIS_DATABASES__TRADING__MAX_ROWS": "1",
    "BASIS_DATABASES__TRADING__WRITE__CONNECTION_STRING": "Server=nowhere.invalid",
    "BASIS_DATABASES__TRADING__WRITE__LEDGER": "trading.publishes",
    "BASIS_OPTIM__ORDERS__DATABASE": "trading",
    "BASIS_OPTIM__ORDERS__TABLE": "trading.orders",
}


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        (
            "mssql",
            (
                "CREATE TABLE trading.orders (\n"
                "    run_id NVARCHAR(36) NOT NULL,\n"
                "    portfolio_id NVARCHAR(64) NOT NULL,\n"
                "    security_id NVARCHAR(64) NOT NULL,\n"
                "    side NVARCHAR(4) NOT NULL,\n"
                "    quantity FLOAT NOT NULL,\n"
                "    CONSTRAINT pk_orders PRIMARY KEY (run_id, portfolio_id, security_id, side)\n"
                ");\n"
            ),
        ),
        (
            "oracle",
            (
                "CREATE TABLE trading.orders (\n"
                "    run_id VARCHAR2(36 CHAR) NOT NULL,\n"
                "    portfolio_id VARCHAR2(64 CHAR) NOT NULL,\n"
                "    security_id VARCHAR2(64 CHAR) NOT NULL,\n"
                "    side VARCHAR2(4 CHAR) NOT NULL,\n"
                "    quantity BINARY_DOUBLE NOT NULL,\n"
                "    CONSTRAINT pk_orders PRIMARY KEY (run_id, portfolio_id, security_id, side)\n"
                ");\n"
            ),
        ),
    ],
)
@pytest.mark.usefixtures("listed")
def test_ddl_prints_the_orders_table_the_settings_name_without_importing_the_model(
    basis: Basis, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], dialect: str, expected: str
) -> None:
    for name, value in TRADING.items():
        monkeypatch.setenv(name, value)

    assert basis("ddl", "ghost", "--dialect", dialect) == 0
    assert capsys.readouterr().out == expected


@pytest.fixture
def listed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The environment lists one model, `ghost`, as an entry point to a module that does not exist, so a command that
    imported it would fail."""
    info = tmp_path / "listed" / "ghost_models-1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: ghost-models\nVersion: 1.0\n")
    (info / "entry_points.txt").write_text("[optim.models]\nghost = nowhere.ghost.model\n")
    monkeypatch.syspath_prepend(tmp_path / "listed")


def error_line(capsys: pytest.CaptureFixture[str]) -> str:
    """The one ERROR line a failure prints: the last, after the record that says the command finished."""
    lines = capsys.readouterr().err.splitlines()
    assert [line for line in lines if line.startswith("ERROR")] == lines[-1:]
    assert json.loads(lines[-2])["message"].startswith("finished ")
    return lines[-1]


@pytest.mark.usefixtures("listed")
def test_ddl_refuses_a_model_the_environment_does_not_list(basis: Basis, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    for name, value in TRADING.items():
        monkeypatch.setenv(name, value)

    assert basis("ddl", "toys.basic", "--dialect", "mssql") == 1
    assert error_line(capsys) == "ERROR: no model 'toys.basic' is an entry point in optim.models: give one of ['ghost'], or its module path"


@pytest.mark.usefixtures("listed")
def test_ddl_without_an_orders_table_names_the_settings_to_set(basis: Basis, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis("ddl", "ghost", "--dialect", "mssql") == 1
    assert error_line(capsys).startswith("ERROR: no orders table is configured, which publishing and `basis ddl MODEL` need: set BASIS_OPTIM__ORDERS__DATABASE")


def test_ddl_prints_the_ledger_for_the_table_given(basis: Basis, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis("ddl", "--ledger", "trading.publishes", "--dialect", "mssql") == 0
    assert capsys.readouterr().out == ledger("trading.publishes", "mssql")


def fixture_files(tmp_path: Path) -> Path:
    """The toy's datasets as files, for a run of the command line on them."""
    (data := tmp_path / "data").mkdir()
    frame(HOLDINGS, {"portfolio_id": "P1"}).to_parquet(data / "holdings.parquet")
    MARKET.to_parquet(data / "securities.parquet")
    (tmp_path / "params.json").write_text("{}")
    return data


def test_a_run_not_asked_to_publish_where_an_orders_table_is_configured_says_it_wrote_none(basis: Basis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    for name, value in TRADING.items() | {"BASIS_SNAPSHOT_ROOT": str(tmp_path / "snapshots")}.items():
        monkeypatch.setenv(name, value)
    data = fixture_files(tmp_path)

    assert basis("run", "toys.basic", "--params", str(tmp_path / "params.json"), "--portfolios", "P1", "--data", str(data), "--as-of", AS_OF.isoformat()) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "orders: none written to trading.orders, since the run was not given --publish: run it with --publish to write them"


def test_again_without_publish_is_refused_before_anything_runs(basis: Basis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("BASIS_SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    data = fixture_files(tmp_path)

    assert basis("run", "toys.basic", "--params", str(tmp_path / "params.json"), "--portfolios", "P1", "--data", str(data), "--again") == 1
    assert error_line(capsys) == ("ERROR: --again publishes a run's orders even though another run published the date's, and the run was not given --publish: add --publish, or leave --again out")
    assert not (tmp_path / "snapshots").exists()


def test_again_without_portfolios_is_refused_before_anything_runs(basis: Basis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("BASIS_SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    data = fixture_files(tmp_path)

    assert basis("run", "toys.basic", "--params", str(tmp_path / "params.json"), "--data", str(data), "--publish", "--again") == 1
    assert error_line(capsys) == (
        "ERROR: --again publishes beside another run's orders for the date, and the run names no portfolios, so it would order every one again: name those that still need orders with --portfolios"
    )
    assert not (tmp_path / "snapshots").exists()


def test_a_run_asked_to_publish_where_no_orders_table_is_configured_exits_1_before_it_loads(basis: Basis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("BASIS_SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    (tmp_path / "params.json").write_text("{}")

    assert basis("run", "toys.basic", "--params", str(tmp_path / "params.json"), "--portfolios", "P1", "--publish") == 1
    assert error_line(capsys).startswith("ERROR: no orders table is configured")
    assert not (tmp_path / "snapshots").exists()
