import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from basis.core import command, doctor, hooks, sync
from basis.core.verbs import Part

type Git = Callable[..., str]
"""conftest's `git` fixture."""

FEED = "https://pkgs.example.com/firm/_packaging/python/pypi/simple/"
TOOLS = {"uv": ("/home/dev/.local/bin/uv", "uv 0.13.7 (abc 2026-10-01)"), "git": ("/usr/bin/git", "git version 2.47.3")}


@pytest.fixture(autouse=True)
def no_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [name for name in os.environ if name.startswith("BASIS_")]:
        monkeypatch.delenv(name)


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
