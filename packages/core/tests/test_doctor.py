import os
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pyarrow.fs as pafs
import pytest

from basis.core import command, doctor, hooks, lake, sync
from basis.core.databases import Database, Statements
from basis.core.errors import Denied
from basis.core.settings import Settings
from basis.core.verbs import Part

type Git = Callable[..., str]
"""conftest's `git` fixture."""

FEED = "https://pkgs.example.com/firm/_packaging/python/pypi/simple/"
TOOLS = {"uv": ("/home/dev/.local/bin/uv", "uv 0.13.7 (abc 2026-10-01)"), "git": ("/usr/bin/git", "git version 2.47.3")}


@pytest.fixture(autouse=True)
def no_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [name for name in os.environ if name.startswith("BASIS_")]:
        monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def own_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test's own configuration folder, never the machine's: a CI runner sets XDG_CONFIG_HOME, which the user's
    files for each environment are found under."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)


def desktop(home: Path, *, environ: Mapping[str, str] | None = None, system: str = "Linux", tools: Mapping[str, tuple[str, str]] = TOOLS) -> doctor.Desktop:
    """A desktop whose home is `home`, with `tools`, each its path and what its `--version` prints."""
    printed = dict(tools.values())

    def run(argv: Sequence[str]) -> str | None:
        return printed.get(argv[0])

    return doctor.Desktop(environ or {}, system, lambda name: tools[name][0] if name in tools else None, run, home)


def feed(home: Path, text: str = f'[[index]]\nname = "firm"\nurl = "{FEED}"\ndefault = true\n') -> None:
    (home / ".config/uv").mkdir(parents=True, exist_ok=True)
    (home / ".config/uv/uv.toml").write_text(text, encoding="utf-8")


def diagnosed(repo: Path | None, found: doctor.Desktop, capsys: pytest.CaptureFixture[str], parts: Mapping[str, Part] | None = None) -> tuple[int, list[str], list[str]]:
    """Doctor's exit code, the checks it passed, and its problems, each checked to be one line ending in its fix."""
    code = doctor.doctor(repo, parts or {}, found, describe=command.one_line)
    captured = capsys.readouterr()
    problems = captured.err.splitlines()
    assert all(line.startswith("ERROR: ") and ": " in line.removeprefix("ERROR: ") for line in problems)
    return code, captured.out.splitlines(), problems


@pytest.fixture
def repo(tmp_path: Path, git: Git) -> Path:
    folder = tmp_path / "repo"
    folder.mkdir()
    (folder / "pyproject.toml").write_text('[tool.basis]\nparts = ["alpha"]\n', encoding="utf-8")
    sync.write(folder, [])
    git(folder, "init", "--quiet")
    hooks.ensure(folder)
    return folder


def test_a_desktop_set_up_for_basis_passes_every_check(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    feed(tmp_path)
    code, passed, problems = diagnosed(repo, desktop(tmp_path), capsys)
    assert (code, problems) == (0, [])
    assert passed[1:5] == [
        "ok: uv 0.13.7 at /home/dev/.local/bin/uv",
        "ok: git 2.47.3 at /usr/bin/git",
        f"ok: the feed: {FEED}, the default index in {tmp_path / '.config/uv/uv.toml'}",
        "ok: the clone runs the repo's hooks from .githooks/",
    ]


def test_a_broken_desktop_gets_one_line_and_its_fix_for_each_problem(repo: Path, tmp_path: Path, git: Git, capsys: pytest.CaptureFixture[str]) -> None:
    git(repo, "config", "core.hooksPath", "/dev/null")
    old = {"uv": ("/usr/bin/uv", "uv 0.9.2"), "git": TOOLS["git"]}
    code, _, problems = diagnosed(repo, desktop(tmp_path, tools=old), capsys)
    assert code == 1
    assert problems == [
        "ERROR: uv is 0.9.2, and basis needs 0.13.6 or later: run `uv self update`, or update it as your desktop installed it",
        (
            f"ERROR: no uv.toml names a default index (uv reads {tmp_path / '.config/uv/uv.toml'}, /etc/uv/uv.toml), so uv takes packages from public PyPI, where anyone may"
            f" publish a name the firm's feed holds: add the firm's feed to {tmp_path / '.config/uv/uv.toml'} as [[index]] with url = FEED and default = true, as the feed's setup says"
        ),
        "ERROR: git runs this clone's hooks from /dev/null, not .githooks/, so the repo's commit and push hooks never run: run `git config core.hooksPath .githooks`",
    ]


def test_a_desktop_without_uv_or_git_says_how_to_install_each(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    feed(tmp_path)
    _, _, problems = diagnosed(None, desktop(tmp_path, tools={}), capsys)
    assert [line.split(":")[1].strip() for line in problems] == [
        "uv is not on the PATH, and basis runs every check through it",
        "git is not on the PATH, so the repo's commit and push hooks cannot run",
    ]


@pytest.mark.parametrize(
    ("toml", "environ", "problem"),
    [
        (f'[[index]]\nname = "firm"\nurl = "{FEED}"\n', {}, "no uv.toml names a default index"),
        ('index-url = "https://pypi.org/simple"\n', {}, "makes public PyPI the default index, not the firm's feed"),
        ("[[index]\n", {}, "is not TOML"),
        (None, {"UV_NO_CONFIG": "1"}, "UV_NO_CONFIG is set, so uv reads no uv.toml"),
        (None, {"UV_DEFAULT_INDEX": "https://pypi.org/simple/"}, "UV_DEFAULT_INDEX, which overrides uv.toml's default index makes public PyPI the default index"),
    ],
    ids=["an-extra-index", "pypi", "not-toml", "no-config", "an-override"],
)
def test_a_feed_that_is_not_the_default_index_is_a_problem(tmp_path: Path, capsys: pytest.CaptureFixture[str], toml: str | None, environ: dict[str, str], problem: str) -> None:
    if toml is not None:
        feed(tmp_path, toml)
    _, _, problems = diagnosed(None, desktop(tmp_path, environ=environ), capsys)
    assert len(problems) == 1
    assert problem in problems[0]


def test_the_users_default_index_must_be_the_one_the_repo_names(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    feed(tmp_path)
    with (repo / "pyproject.toml").open("a", encoding="utf-8") as file:
        file.write('\n[[tool.uv.index]]\nname = "internal"\nurl = "https://pkgs.example.com/other/simple/"\ndefault = true\n')
    _, _, problems = diagnosed(repo, desktop(tmp_path), capsys)
    assert problems == [
        f"ERROR: {tmp_path / '.config/uv/uv.toml'} makes {FEED} the default index, and the repo's pyproject.toml names https://pkgs.example.com/other/simple/: make the repo's index the default there"
    ]


def test_on_windows_the_users_uv_toml_is_in_appdata(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "AppData/uv").mkdir(parents=True)
    (tmp_path / "AppData/uv/uv.toml").write_text(f'[[index]]\nurl = "{FEED}"\ndefault = true\n', encoding="utf-8")
    code, passed, _ = diagnosed(None, desktop(tmp_path, environ={"APPDATA": str(tmp_path / "AppData")}, system="Windows"), capsys)
    assert code == 0
    assert f"ok: the feed: {FEED}, the default index in {tmp_path / 'AppData/uv/uv.toml'}" in passed


def test_in_wsl_a_repo_on_a_windows_drive_is_a_problem() -> None:
    found = doctor.Desktop({}, "WSL", lambda _: None, lambda _: None, Path("/home/dev"))
    [entry] = doctor._where(Path("/mnt/c/Users/dev/repos/pricing"), found)  # noqa: SLF001 -- a path this machine has no folder at
    assert entry.problem is not None
    assert entry.problem[1] == "clone the repo again in WSL's own filesystem, such as ~/repos, and work on it there"


QA = '\n[tool.basis.qa]\nmarker = "qa"\nstores = ["runs-qa"]\nwrites = {}\n'


def test_settings_that_do_not_validate_and_qa_settings_that_point_outside_qa_are_problems(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    feed(tmp_path)
    parts = {"alpha": Part(qa=lambda _: pytest.fail("doctor plans no run"))}
    with (repo / "pyproject.toml").open("a", encoding="utf-8") as file:
        file.write(QA)
    _, _, problems = diagnosed(repo, desktop(tmp_path), capsys, parts)
    user = tmp_path / ".config" / "basis" / "qa.env"
    assert problems == [f"ERROR: the repo declares a QA tier, and this machine has no {user}, so the push hook skips it: write it with QA's databases and store, as the repo's .env.example lists"]
    user.parent.mkdir(parents=True)
    user.write_text("BASIS_SNAPSHOT_ROOT=//files/runs\n", encoding="utf-8")
    monkeypatch.setenv("BASIS_DATABASES__WAREHOUSE__KIND", "mssql")
    _, _, problems = diagnosed(repo, desktop(tmp_path), capsys, parts)
    assert [line.split(" ")[1] for line in problems] == ["the", "BASIS_SNAPSHOT_ROOT"]
    assert "BASIS_DATABASES__WAREHOUSE__CONNECTION_STRING: Field required" in problems[0]


def test_basis_doctor_runs_outside_a_repo_and_checks_the_desktop_alone(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    command.run(["doctor"], cwd=tmp_path, installed={})
    printed = capsys.readouterr().out
    assert "hooks" not in printed
    assert printed.startswith("ok: ")


def test_in_an_ordinary_python_project_doctor_checks_the_desktop_alone(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "notebook-tools"\n', encoding="utf-8")
    command.run(["doctor"], cwd=tmp_path, installed={})
    captured = capsys.readouterr()
    assert "pyproject.toml" not in captured.err
    assert "hooks" not in captured.out


def test_a_basis_table_that_is_not_valid_is_a_problem_doctor_names(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.basis]\nparts = "alpha"\n', encoding="utf-8")
    assert command.run(["doctor"], cwd=tmp_path, installed={}) == 1
    assert f"ERROR: {tmp_path / 'pyproject.toml'}'s [tool.basis] parts is not a list of part names: give [tool.basis] parts" in capsys.readouterr().err


def test_doctor_in_a_repo_checks_its_hooks_and_settings_too(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    command.run(["doctor"], cwd=repo, installed={"alpha": Part})
    printed = capsys.readouterr().out.splitlines()
    assert "ok: the clone runs the repo's hooks from .githooks/" in printed
    assert "note: the repo declares no QA tier, so the push hook runs none" in printed


class Logins:
    """Databases whose logins answer, but for those `refused` names, as `(database, writes)`, which a database refuses."""

    def __init__(self, *refused: tuple[str, bool]) -> None:
        self.refused = set(refused)
        self.asked: list[tuple[str, bool, str]] = []

    def __call__(self, _: Settings, name: str) -> Database:
        logins = self

        class Answering:
            @contextmanager
            def statements(self, *, writes: bool) -> Generator[Statements]:
                if (name, writes) in logins.refused:
                    detail = "login failed"
                    raise Denied(detail, resource=name, reason="Login failed for user 'etl'")
                yield cast("Statements", self)

            def query(self, sql: str, binds: Mapping[str, object], *, timeout_s: float) -> object:
                logins.asked.append((name, bool(binds), sql))
                assert timeout_s == 5
                return None

        return cast("Database", Answering())


def env_file(home: Path, env: str, text: str) -> Path:
    path = home / ".config" / "basis" / f"{env}.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


MSSQL = "BASIS_DATABASES__WAREHOUSE__KIND=mssql\nBASIS_DATABASES__WAREHOUSE__CONNECTION_STRING=Server=sqlqa;Database=Trading\nBASIS_DATABASES__WAREHOUSE__ISOLATION=snapshot\n"
MSSQL += "BASIS_DATABASES__WAREHOUSE__TIMEOUT_S=5\nBASIS_DATABASES__WAREHOUSE__MAX_ROWS=5\nBASIS_DATABASES__WAREHOUSE__WRITE__CONNECTION_STRING=Server=sqlqa;UID=w\n"
ORACLE = "BASIS_DATABASES__MARKETS__KIND=oracle\nBASIS_DATABASES__MARKETS__DSN=oraqa:1521/M\nBASIS_DATABASES__MARKETS__USER=u\nBASIS_DATABASES__MARKETS__PASSWORD=p\n"
ORACLE += "BASIS_DATABASES__MARKETS__MODE=thin\nBASIS_DATABASES__MARKETS__ISOLATION=read_only\nBASIS_DATABASES__MARKETS__TIMEOUT_S=5\nBASIS_DATABASES__MARKETS__MAX_ROWS=5\n"


def test_doctor_with_an_environment_checks_each_login_of_each_database_and_its_run_store(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    feed(tmp_path)
    (tmp_path / "runs-qa").mkdir()
    path = env_file(tmp_path, "qa", f"{MSSQL}{ORACLE}BASIS_SNAPSHOT_ROOT={tmp_path / 'runs-qa'}\n")
    logins = Logins(("warehouse", True))
    code = doctor.doctor(None, {}, desktop(tmp_path), describe=command.one_line, env="qa", databases=logins)
    captured = capsys.readouterr()
    assert code == 1
    assert [line for line in captured.out.splitlines() if "qa's" in line] == [
        f"ok: qa's settings, from {path}",
        "ok: qa's database markets: its read login answers",
        "ok: qa's database warehouse: its read login answers",
        f"ok: qa's run store: {tmp_path / 'runs-qa'}",
    ]
    reason = "qa's database warehouse: its write login does not answer (Denied: login failed: Login failed for user 'etl')"
    assert captured.err.splitlines() == [f"ERROR: {reason}: check BASIS_DATABASES__WAREHOUSE__* in {path}, and that this machine may reach the database"]
    assert logins.asked == [("markets", False, "SELECT 1 AS answered FROM DUAL"), ("warehouse", False, "SELECT 1 AS answered")]


def test_doctor_with_an_environment_nothing_configures_names_the_file_to_write(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    feed(tmp_path)
    code = doctor.doctor(None, {}, desktop(tmp_path), describe=command.one_line, env="dev", databases=Logins())
    assert code == 1
    assert capsys.readouterr().err.startswith(f"ERROR: there is no {tmp_path / '.config' / 'basis' / 'dev.env'}, and the process's environment sets no BASIS_ settings")


def test_doctor_in_prod_says_whether_this_machine_may_write_and_whether_its_store_is_reachable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    feed(tmp_path)
    path = env_file(tmp_path, "prod", f"BASIS_SNAPSHOT_ROOT={tmp_path / 'nowhere'}\n")
    code = doctor.doctor(None, {}, desktop(tmp_path), describe=command.one_line, env="prod", databases=Logins())
    captured = capsys.readouterr()
    assert code == 1
    assert "note: prod is read-only here, since the process does not set BASIS_PROD_WRITES=1, as only a deployment that writes does" in captured.out.splitlines()
    assert captured.err.splitlines() == [f"ERROR: prod's run store, BASIS_SNAPSHOT_ROOT, is {tmp_path / 'nowhere'}, which this machine cannot reach: correct it in {path}, or reach it from here"]


def test_doctor_takes_only_an_environment_basis_knows(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert command.run(["doctor", "--env", "production"], cwd=tmp_path, installed={}) == 2
    assert "invalid choice: 'production'" in capsys.readouterr().err


def test_doctor_with_an_environment_checks_each_folder_is_reachable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    feed(tmp_path)
    (tmp_path / "pm").mkdir()
    path = env_file(tmp_path, "dev", f"BASIS_FOLDERS__PM__PATH={tmp_path / 'pm'}\nBASIS_FOLDERS__RISK__PATH={tmp_path / 'risk'}\n")
    assert doctor.doctor(None, {}, desktop(tmp_path), describe=command.one_line, env="dev", databases=Logins()) == 1
    captured = capsys.readouterr()
    assert f"ok: dev's folder pm: {tmp_path / 'pm'}" in captured.out.splitlines()
    assert captured.err.splitlines() == [f"ERROR: dev's folder risk, at {tmp_path / 'risk'}, is not there or not reachable from here: mount it, or correct BASIS_FOLDERS__RISK__PATH in {path}"]


def test_doctor_with_an_environment_checks_each_lake_s_container_and_that_a_folder_in_it_renames(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    feed(tmp_path)
    (tmp_path / "lake" / "archive").mkdir(parents=True)
    lakes_named = (("HISTORY", "archive"), ("GONE", "gone"))
    lines = [f"BASIS_LAKES__{name}__{key}={value}" for name, box in lakes_named for key, value in (("ACCOUNT", "lakedev"), ("CONTAINER", box), ("AUTH", "cli"))]
    path = env_file(tmp_path, "dev", "\n".join(lines) + "\n")

    def lakes(name: str, configured: Settings) -> lake.Lake:
        found = configured.lakes[name]
        return lake.Lake(name, found.container, pafs.SubTreeFileSystem(str(tmp_path / "lake"), pafs.LocalFileSystem()), found.account)

    assert doctor.doctor(None, {}, desktop(tmp_path), describe=command.one_line, env="dev", databases=Logins(), lakes=lakes) == 1
    captured = capsys.readouterr()
    assert "ok: dev's lake history: lakedev/archive is reachable, and a folder in it renames" in captured.out.splitlines()
    assert captured.err.splitlines() == [
        f"ERROR: dev's lake gone cannot be archived to (the lake gone has no container lakedev/gone: create it, or correct BASIS_LAKES__GONE__CONTAINER): check BASIS_LAKES__GONE__* in {path}"
    ]
    assert sorted(child.name for child in (tmp_path / "lake" / "archive").iterdir()) == []
