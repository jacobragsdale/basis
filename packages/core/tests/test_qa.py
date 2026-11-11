import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest

from basis.core import environments, fakes, frames, qa
from basis.core.databases import Counts
from basis.core.errors import BasisError
from basis.core.settings import GivenSettings, locate
from basis.core.tables import Changes, Column
from basis.core.verbs import Part, Qa, QaPlan, QaRun

QA = "Server=sqlqa01.example.com;Database=Trading;Encrypt=yes"
PROD = "Server=sql01.example.com;Database=Trading;Encrypt=yes"
DECLARED = """
[tool.basis]
parts = ["alpha"]

[tool.basis.qa]
marker = "qa"
stores = ["runs-qa", "//files/runs-qa"]
writes = { warehouse = ["prices", "etl.Publishes"] }
"""


def mssql(connection_string: str = QA, *, write: str | None = QA, ledger: str = "etl.publishes") -> dict[str, object]:
    block: dict[str, object] = {"kind": "mssql", "connection_string": connection_string, "isolation": "snapshot", "timeout_s": 5, "max_rows": 5}
    return block | ({"write": {"connection_string": write, "ledger": ledger}} if write is not None else {})


def settings(snapshot_root: str | None = "runs-qa", **databases: dict[str, object]) -> GivenSettings:
    return GivenSettings.model_validate({"snapshot_root": snapshot_root, "databases": databases or {"warehouse": mssql()}})


@pytest.fixture
def declared(tmp_path: Path) -> qa.Declared:
    (tmp_path / "pyproject.toml").write_text(DECLARED, encoding="utf-8")
    found = qa.declared(tmp_path / "pyproject.toml", {"alpha": Part()})
    assert found is not None
    return found


def problems(found: GivenSettings, declared: qa.Declared) -> list[str]:
    return [reason for reason, _ in qa.problems(found, declared, source=Path(".env.qa"))]


@pytest.mark.parametrize(
    ("connection_string", "pairs"),
    [
        ("Server=a;Database=b", [("server", "a"), ("database", "b")]),
        (" Data Source = a ; Initial Catalog=b;", [("data source", "a"), ("initial catalog", "b")]),
        ("PWD={x;Server=sqlqa};Server=prod", [("pwd", "x;Server=sqlqa"), ("server", "prod")]),
        ('PWD="a""b";Server=prod', [("pwd", 'a"b'), ("server", "prod")]),
        ("PWD={a}}b};Server=prod", [("pwd", "a}b"), ("server", "prod")]),
        ("Server=a;;Server=b", [("server", "a"), ("server", "b")]),
        (";Server=a", [("server", "a")]),
        ("junk;Server=prod", [("server", "prod")]),
        (" ; ;Server = a ; ;Database= b;; ", [("server", "a"), ("database", "b")]),
        ("Server=;Database=b", [("server", ""), ("database", "b")]),
        ("Server=a;trailing", [("server", "a")]),
        ("", []),
    ],
    ids=[
        "plain",
        "spaces-and-synonyms",
        "a-brace-holds-a-semicolon",
        "a-quote-escaped",
        "a-brace-escaped",
        "an-empty-segment",
        "a-leading-semicolon",
        "a-segment-with-no-equals",
        "spaces-and-empty-segments",
        "an-empty-value",
        "trailing-junk",
        "empty",
    ],
)
def test_a_connection_strings_keywords_are_read_as_odbc_reads_them(connection_string: str, pairs: list[tuple[str, str]]) -> None:
    assert qa.keywords(connection_string) == pairs


def test_settings_whose_every_database_and_store_carry_the_marker_are_safe(declared: qa.Declared) -> None:
    oracle: dict[str, object] = {"kind": "oracle", "dsn": "oraqa:1521/MARKETS", "user": "u", "password": "p", "mode": "thin", "isolation": "read_only", "timeout_s": 5, "max_rows": 5}
    found = settings(warehouse=mssql("Server=sql01;Database=Trading_QA", write="SERVER=SQLQA01;DATABASE=Trading"), markets=oracle)
    assert problems(found, declared) == []


@pytest.mark.parametrize(
    ("found", "problem"),
    [
        (settings(warehouse=mssql(PROD)), "BASIS_DATABASES__WAREHOUSE__CONNECTION_STRING in .env.qa names server sql01.example.com and database Trading, and neither contains the QA marker 'qa'"),
        (
            settings(warehouse=mssql(write=PROD)),
            "BASIS_DATABASES__WAREHOUSE__WRITE__CONNECTION_STRING in .env.qa names server sql01.example.com and database Trading, and neither contains the QA marker 'qa'",
        ),
        (settings(warehouse=mssql("PWD={;Server=sqlqa};Server=prod")), "BASIS_DATABASES__WAREHOUSE__CONNECTION_STRING in .env.qa names server prod, and neither contains"),
        (settings(warehouse=mssql("Server=sqlqa;Server=prod")), "BASIS_DATABASES__WAREHOUSE__CONNECTION_STRING in .env.qa names 2 servers and 0 databases"),
        (settings(warehouse=mssql("Server=sqlqa;;Server=prod;Database=qa")), "BASIS_DATABASES__WAREHOUSE__CONNECTION_STRING in .env.qa names 2 servers and 1 databases"),
        (settings(warehouse=mssql("Database=Trading_QA")), "BASIS_DATABASES__WAREHOUSE__CONNECTION_STRING in .env.qa names 0 servers and 1 databases"),
        (
            settings(markets={"kind": "oracle", "dsn": "ora01:1521/MARKETS", "user": "u", "password": "p", "mode": "thin", "isolation": "read_only", "timeout_s": 5, "max_rows": 5}),
            "BASIS_DATABASES__MARKETS__DSN in .env.qa is ora01:1521/MARKETS, which does not contain the QA marker 'qa'",
        ),
        (settings(None), ".env.qa sets no BASIS_SNAPSHOT_ROOT, the QA store the tier saves its runs in"),
        (settings("//files/qa-elsewhere"), "BASIS_SNAPSHOT_ROOT in .env.qa is //files/qa-elsewhere, which [tool.basis.qa] stores does not list"),
        (settings("//files/runs"), "BASIS_SNAPSHOT_ROOT in .env.qa is //files/runs, which [tool.basis.qa] stores does not list"),
        (settings(warehouse=mssql(ledger="etl.ledger")), "BASIS_DATABASES__WAREHOUSE__WRITE__LEDGER in .env.qa is etl.ledger, which [tool.basis.qa] writes does not list for warehouse"),
    ],
    ids=[
        "a-server",
        "a-write-block-elsewhere",
        "a-brace-hides-no-server",
        "two-servers",
        "a-second-server-after-an-empty-segment",
        "no-server",
        "an-oracle-dsn",
        "no-store",
        "a-store-not-listed",
        "an-unmarked-store",
        "a-ledger",
    ],
)
def test_a_setting_that_does_not_point_at_qa_is_refused_by_name(declared: qa.Declared, found: GivenSettings, problem: str) -> None:
    assert any(reason.startswith(problem) for reason in problems(found, declared)), problems(found, declared)


@pytest.mark.parametrize(
    ("location", "found"),
    [("runs-qa", "/repo/runs-qa"), ("/srv/runs-qa", "/srv/runs-qa"), ("abfs://runs@qa.dfs.core.windows.net/x", "abfs://runs@qa.dfs.core.windows.net/x")],
    ids=["relative", "absolute", "a-url"],
)
def test_a_store_is_found_where_the_command_reads_it(location: str, found: str) -> None:
    assert locate(location, Path("/repo")) == found


def test_a_database_no_run_uses_must_carry_the_marker_too(declared: qa.Declared) -> None:
    assert len(problems(settings(warehouse=mssql(), unused=mssql(PROD, write=None)), declared)) == 1


@pytest.mark.parametrize(
    ("table", "problem"),
    [
        ('marker = "q"\nstores = ["qa"]\nwrites = {}\n', "marker: String should match pattern"),
        ('marker = "qa"\nstores = ["//files/runs"]\nwrites = {}\n', "stores has //files/runs, whose name does not contain the marker 'qa'"),
        ('marker = "qa"\nstores = ["qa"]\nwrites = {}\nbeta = {}\n', "has beta, which basis does not read"),
        ('settings = ".env.qa"\nmarker = "qa"\nstores = ["qa"]\nwrites = {}\n', "has settings, which basis does not read"),  # the user's qa.env took its place
        ('marker = "qa"\nstores = []\nwrites = {}\n', "stores: Tuple should have at least 1 item"),
    ],
    ids=["a-one-letter-marker", "an-unmarked-store", "an-unknown-key", "a-settings-file", "no-store"],
)
def test_a_declaration_that_is_not_explicit_and_safe_is_refused(tmp_path: Path, table: str, problem: str) -> None:
    (tmp_path / "pyproject.toml").write_text(f'[tool.basis]\nparts = ["alpha"]\n\n[tool.basis.qa]\n{table}', encoding="utf-8")
    with pytest.raises(BasisError, match=problem):
        qa.declared(tmp_path / "pyproject.toml", {"alpha": Part()})


def test_a_repo_that_declares_no_qa_tier_has_none(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.basis]\nparts = ["alpha"]\n', encoding="utf-8")
    assert qa.declared(tmp_path / "pyproject.toml", {"alpha": Part()}) is None


@dataclass(frozen=True)
class Table:
    """A table to write, as the guard sees it: by its name."""

    table: str
    database: str = "warehouse"
    key: tuple[str, ...] | None = None
    columns: tuple[Column, ...] = (Column("security_id", "str", nullable=False),)

    def column(self, name: str) -> Column:
        return next(column for column in self.columns if column.name == name)


SECURITIES = pd.DataFrame({"security_id": pd.Series(["A", "B"], dtype=frames.dtype(Column("security_id", "str", nullable=False)))})


def test_the_tiers_databases_refuse_a_table_writes_does_not_list_before_writing_it() -> None:
    database = fakes.Database("warehouse")
    for name in ("PRICES", "positions"):
        database.create(Table(name))
    guarded = qa._Guarded(database, frozenset({"prices"}))  # noqa: SLF001 -- what `context` hands a part, on basis-core's fake database
    with guarded.writing() as opened:
        spec = opened.describe("PRICES")
        assert spec is not None
        assert opened.apply(Table("PRICES"), Changes(insert=SECURITIES), spec) == Counts(inserted=2)
        with pytest.raises(qa.QaError, match=r"the QA tier refused to write positions in warehouse, which \[tool\.basis\.qa\] writes does not list"):
            opened.apply(Table("positions"), Changes(insert=SECURITIES), spec)
        opened.commit()
    assert (len(database.rows("PRICES")), database.rows("positions")) == (2, [])


def test_the_tiers_databases_refuse_statements_of_a_parts_own_through_the_write_login() -> None:
    database = fakes.Database("warehouse")
    database.answers["SELECT 1 AS one"] = pd.DataFrame({"one": [1]})
    guarded = qa._Guarded(database, frozenset({"prices"}))  # noqa: SLF001 -- as above
    with pytest.raises(qa.QaError, match="the QA tier refused statements of the part's own through warehouse's write login"):
        guarded.statements(writes=True)
    with guarded.statements(writes=False) as statements:
        assert statements.query("SELECT 1 AS one", {}, timeout_s=5).to_pylist() == [{"one": 1}]


def test_a_write_block_with_no_ledger_needs_none_listed(declared: qa.Declared) -> None:
    assert problems(settings(warehouse={key: value for key, value in mssql().items() if key != "write"} | {"write": {"connection_string": QA}}), declared) == []


def planned(*problems: tuple[str, str]) -> tuple[list[Qa], Part]:
    """A part whose plan records each context it is given and refuses with `problems`, and whose run says so."""
    given: list[Qa] = []

    def plan(context: Qa) -> QaPlan:
        given.append(context)

        def run() -> Sequence[QaRun]:
            return [QaRun("prices", "R1", "published")]

        return QaPlan(problems, run)

    return given, Part(qa=plan)


@pytest.fixture(autouse=True)
def user_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The user's own basis folder, which holds their qa.env: an empty one of the test's."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))  # where Windows keeps the same folder
    return tmp_path / "config" / "basis"


def qa_env(text: str) -> Path:
    path = environments.user_file("qa")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def repo(tmp_path: Path, env: str | None) -> Path:
    (tmp_path / "pyproject.toml").write_text(DECLARED, encoding="utf-8")
    if env is not None:
        qa_env(env)
    return tmp_path


SAFE = "\n".join(f"BASIS_DATABASES__WAREHOUSE__{key}={value}" for key, value in mssql().items() if key != "write")
SAFE += f"\nBASIS_DATABASES__WAREHOUSE__WRITE__CONNECTION_STRING={QA}\nBASIS_DATABASES__WAREHOUSE__WRITE__LEDGER=etl.publishes\nBASIS_SNAPSHOT_ROOT=runs-qa\nBASIS_LOG_LEVEL=DEBUG\n"


def tier(folder: Path, parts: Mapping[str, Part], repo_folder: Path | None = None) -> qa.Outcome:
    return qa.tier(repo_folder or folder, parts, commit="C1", folder=folder)


def test_the_tier_runs_each_part_against_the_user_s_qa_env_alone_and_the_store_it_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASIS_DATABASES__PROD__KIND", "mssql")  # the process's own, which the tier never reads
    given, part = planned()
    outcome = tier(repo(tmp_path, SAFE), {"alpha": part})
    assert outcome.failures == []
    assert [(name, run.run_id) for name, run in outcome.runs] == [("alpha", "R1")]
    [context] = given
    assert (sorted(context.settings.databases), context.store.location, context.commit) == (["warehouse"], (tmp_path / "runs-qa").as_posix(), "C1")
    assert context.may_write("warehouse", "ETL.PUBLISHES")
    assert not context.may_write("warehouse", "positions")


def test_a_check_in_another_worktree_reads_a_relative_store_from_the_clones_own_folder(tmp_path: Path) -> None:
    given, part = planned()
    own, worktree = tmp_path / "own", tmp_path / "worktree"
    own.mkdir()
    worktree.mkdir()
    repo(own, SAFE)
    (worktree / "pyproject.toml").write_text(DECLARED, encoding="utf-8")
    assert tier(own, {"alpha": part}, repo_folder=worktree).failures == []
    assert given[0].store.location == (own / "runs-qa").as_posix()
    assert given[0].repo == worktree


def test_a_machine_without_the_user_s_qa_env_skips_the_tier_and_says_why(tmp_path: Path, user_folder: Path) -> None:
    given, part = planned()
    (tmp_path / ".env.qa").write_text(SAFE, encoding="utf-8")  # a repo's own file, which the tier no longer reads
    outcome = tier(repo(tmp_path, None), {"alpha": part})
    assert outcome.skipped == f"this machine has no {user_folder / 'qa.env'}, the user's QA settings, which the tier reads: write it with QA's databases and store, as the repo's .env.example lists"
    assert given == []


def test_settings_that_point_outside_qa_run_nothing_and_never_reach_a_part(tmp_path: Path) -> None:
    given, part = planned()
    outcome = tier(repo(tmp_path, SAFE.replace("sqlqa01", "sql01")), {"alpha": part})
    assert [reason.split(" ")[0] for reason, _ in outcome.refused] == ["BASIS_DATABASES__WAREHOUSE__CONNECTION_STRING", "BASIS_DATABASES__WAREHOUSE__WRITE__CONNECTION_STRING"]
    assert (given, outcome.runs) == ([], ())


def test_one_parts_refusal_stops_every_parts_runs(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(DECLARED.replace('parts = ["alpha"]', 'parts = ["alpha", "beta"]'), encoding="utf-8")
    qa_env(SAFE)
    _, alpha = planned()
    _, beta = planned(("beta writes positions", "list it"))
    outcome = tier(tmp_path, {"alpha": alpha, "beta": beta})
    assert (outcome.refused, outcome.runs) == ((("beta writes positions", "list it"),), ())


def test_a_repo_with_no_declaration_or_no_part_with_qa_runs_skips_the_tier(tmp_path: Path) -> None:
    _, part = planned()
    (tmp_path / "pyproject.toml").write_text('[tool.basis]\nparts = ["alpha"]\n', encoding="utf-8")
    assert (tier(tmp_path, {"alpha": part}).skipped or "").startswith("pyproject.toml declares no [tool.basis.qa]")
    assert (tier(repo(tmp_path, SAFE), {"alpha": Part()}).skipped or "").startswith("no part of this repo (alpha) has QA runs")


def test_a_folder_whose_path_lacks_the_marker_is_refused_since_a_qa_run_writes_there(declared: qa.Declared) -> None:
    share = r"\\server\pm" if sys.platform == "win32" else "/mnt/pm"  # absolute on the machine the test runs on
    found = GivenSettings.model_validate({"snapshot_root": "runs-qa", "databases": {"warehouse": mssql()}, "folders": {"pm": {"path": share}, "pm_qa": {"path": f"{share}-qa"}}})
    assert problems(found, declared) == [f"BASIS_FOLDERS__PM__PATH in .env.qa is {share}, which does not contain the QA marker 'qa', and a QA run writes its workbooks there"]


def test_a_lake_whose_account_and_container_lack_the_marker_is_refused_since_a_qa_run_archives_there(declared: qa.Declared) -> None:
    lakes = {"history": {"account": "lakeprod", "container": "archive", "auth": "cli"}, "risk": {"account": "lakeprod", "container": "archive-qa", "auth": "cli"}}
    found = GivenSettings.model_validate({"snapshot_root": "runs-qa", "databases": {"warehouse": mssql()}, "lakes": lakes})
    assert problems(found, declared) == ["BASIS_LAKES__HISTORY__ACCOUNT and __CONTAINER in .env.qa are lakeprod/archive, which lacks the QA marker 'qa', and a QA run archives there"]


def test_a_mongo_database_whose_host_and_name_lack_the_marker_is_refused_since_a_qa_run_reads_and_upserts_there(declared: qa.Declared) -> None:
    mongo = {
        "reference": {"uri": "mongodb://mongo.prod:27017", "database": "reference", "timeout_s": 5, "max_documents": 10},
        "reference_qa": {"uri": "mongodb://mongo.prod:27017", "database": "reference_qa", "timeout_s": 5, "max_documents": 10},
        "hosted": {"uri": "mongodb://user:secret@mongo-qa.firm:27017/?tls=true", "database": "reference", "timeout_s": 5, "max_documents": 10},
    }
    found = GivenSettings.model_validate({"snapshot_root": "runs-qa", "databases": {"warehouse": mssql()}, "mongo": mongo})
    assert problems(found, declared) == [
        "BASIS_MONGO__REFERENCE__URI and __DATABASE in .env.qa name host mongo.prod and database reference, and neither contains the QA marker 'qa', and a QA run reads and upserts there"
    ]
