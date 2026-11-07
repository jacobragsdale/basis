import argparse
import os
import sys
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path

import pytest

from basis.core import command, sync
from basis.core.errors import BasisError, deliberate
from basis.core.settings import Settings
from basis.core.sync import AgentFiles
from basis.core.verbs import Part, Regress, Replay, Runs, Verb

ALPHA = '[project]\nname = "repo"\n\n[tool.basis]\nparts = ["alpha"]\n'


def says(text: str) -> Callable[[argparse.Namespace], int]:
    def handle(_: argparse.Namespace) -> int:
        sys.stdout.write(f"{text}\n")
        return 0

    return handle


def raises(error: Exception) -> Callable[[argparse.Namespace], int]:
    def handle(_: argparse.Namespace) -> int:
        raise error

    return handle


def verb(name: str, handle: Callable[[argparse.Namespace], int] | None = None) -> Verb:
    return Verb(name, f"the {name} verb", lambda _: None, handle or says(name))


def runs(prefix: str, *names: str) -> Runs:
    def replay(args: Replay) -> int:
        sys.stdout.write(f"{prefix} replays {args.name} {args.run_id}\n")
        return 0

    def regress(args: Regress) -> int:
        sys.stdout.write(f"{prefix} regresses {args.name}\n")
        return 0

    return Runs(lambda: names, replay, regress)


INSTALLED: dict[str, Callable[[], Part]] = {"alpha": lambda: Part(verbs=(verb("serve"),)), "beta": lambda: Part(verbs=(verb("sweep"),), runs=runs("beta"))}


@pytest.fixture(autouse=True)
def no_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [name for name in os.environ if name.startswith("BASIS_")]:
        monkeypatch.delenv(name)


def repo(tmp_path: Path, pyproject: str = ALPHA) -> Path:
    (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    (tmp_path / "jobs").mkdir()
    return tmp_path


def basis(folder: Path, *argv: str, installed: Mapping[str, Callable[[], Part]] | None = None) -> int:
    return command.run(argv, cwd=folder, installed=INSTALLED if installed is None else installed)


def error_line(capsys: pytest.CaptureFixture[str]) -> str:
    """The one line a failure prints, after any log records."""
    lines = capsys.readouterr().err.splitlines()
    assert lines
    assert lines[-1].startswith("ERROR: ")
    assert not any(line.startswith("ERROR") for line in lines[:-1])
    return lines[-1]


def test_help_lists_the_declared_parts_verbs_and_the_core_verbs_they_give_something_to_do(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo(tmp_path), "--help") == 0
    listed = [line.split()[0] for line in capsys.readouterr().out.splitlines() if line.startswith("  ") and not line.lstrip().startswith("-")]
    assert listed == ["check", "sync", "serve"]


def test_a_verb_runs_from_a_folder_inside_the_repo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo(tmp_path) / "jobs", "serve") == 0
    assert capsys.readouterr().out == "serve\n"


def test_a_verb_of_an_installed_part_the_repo_does_not_declare_says_how_to_declare_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo(tmp_path), "replay", "prices", "RUN") == 1
    assert error_line(capsys).endswith("comes with the beta part, which " + str(tmp_path / "pyproject.toml") + " does not declare: add 'beta' to its [tool.basis] parts")


def test_a_verb_no_installed_part_has_does_not_parse(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo(tmp_path), "nonsense") == 2
    assert "invalid choice: 'nonsense'" in error_line(capsys)


def test_a_declared_part_that_no_package_provides_names_the_package_to_add(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo(tmp_path, '[tool.basis]\nparts = ["job"]\n'), "check") == 1
    assert error_line(capsys).endswith("declares the part 'job', which no installed package provides: add basis-job to its [project] dependencies, and run `uv sync`")


@pytest.mark.parametrize(
    ("pyproject", "problem"),
    [
        ('[project]\nname = "repo"\n', 'declares no basis parts: add [tool.basis] parts = ["job"]'),
        ("[tool.basis]\nparts = []\n", "parts is not a list of part names"),
        ('[tool.basis]\nparts = "alpha"\n', "parts is not a list of part names"),
        ('[tool.basis]\nparts = ["alpha", "alpha"]\n', "names a part twice"),
        ('[tool.basis]\npart = ["alpha"]\n', "has part, which basis does not read"),
        ("[tool.basis\n", "is not TOML"),
    ],
)
def test_a_repo_declares_its_parts_in_pyproject_and_nowhere_else(tmp_path: Path, capsys: pytest.CaptureFixture[str], pyproject: str, problem: str) -> None:
    assert basis(repo(tmp_path, pyproject), "check") == 1
    assert problem in error_line(capsys)


def test_a_folder_in_no_repo_says_how_to_make_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(tmp_path, "check") == 1
    assert error_line(capsys).endswith("make one, as `uvx basis-job new repo NAME` does")


def test_a_command_line_that_does_not_parse_exits_2_with_one_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def port(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--port", type=int, required=True)

    installed = {"alpha": lambda: Part(verbs=(Verb("serve", "serve", port, says("served")),))}
    assert basis(repo(tmp_path), "serve", installed=installed) == 2
    assert error_line(capsys) == "ERROR: basis serve: the following arguments are required: --port: `basis serve --help` says what it takes"


def test_a_basis_error_prints_as_its_line_alone_with_its_notes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    error = BasisError("no run R1\nof prices: give another")
    error.add_note("while replaying")
    assert basis(repo(tmp_path), "serve", installed={"alpha": lambda: Part(verbs=(verb("serve", raises(error)),))}) == 1
    assert capsys.readouterr().err == "ERROR: while replaying: no run R1 of prices: give another\n"


def test_a_builtin_error_raised_on_purpose_prints_as_one_line_where_the_call_says_so(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def handle(_: argparse.Namespace) -> int:
        with deliberate(LookupError):
            msg = "no run R1: is it the right snapshot root?"
            raise LookupError(msg)

    assert basis(repo(tmp_path), "serve", installed={"alpha": lambda: Part(verbs=(verb("serve", handle),))}) == 1
    assert capsys.readouterr().err == "ERROR: no run R1: is it the right snapshot root?\n"


@pytest.mark.parametrize("error", [KeyError("prices"), IndexError("list index out of range"), TypeError("unsupported operand"), LookupError("no run R1"), ValueError("bad")])
def test_any_other_exception_is_a_defect_that_logs_its_traceback_before_its_line(tmp_path: Path, capsys: pytest.CaptureFixture[str], error: Exception) -> None:
    assert basis(repo(tmp_path), "serve", installed={"alpha": lambda: Part(verbs=(verb("serve", raises(error)),))}) == 1
    err = capsys.readouterr().err.splitlines()
    assert '"traceback":' in err[0]
    assert err[-1].startswith(f"ERROR: {type(error).__name__}: ")
    assert err[-1].endswith(": a defect; the log record above holds its traceback")


def test_variables_under_the_names_basis_read_before_are_refused_with_their_new_names(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ETL_SNAPSHOT_ROOT", "snapshots")
    monkeypatch.setenv("ETL_DATABASES__WAREHOUSE__KIND", "mssql")
    monkeypatch.setenv("ETL_HOME", "/opt/another-tool")  # another tool's, which basis never read
    assert basis(repo(tmp_path), "serve") == 1
    assert capsys.readouterr().err == (
        "ERROR: ETL_DATABASES__WAREHOUSE__KIND, ETL_SNAPSHOT_ROOT are set, and basis reads only BASIS_ variables now: rename ETL_DATABASES__WAREHOUSE__KIND"
        " to BASIS_DATABASES__WAREHOUSE__KIND, ETL_SNAPSHOT_ROOT to BASIS_SNAPSHOT_ROOT, where they are set and in each .env file\n"
    )


def test_version_names_basis_core_and_each_installed_part_anywhere(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(tmp_path, "--version") == 0
    assert capsys.readouterr().out == "basis-core 0.0.0, basis-job 0.0.0\n"


def test_settings_that_do_not_validate_name_the_variables_to_set(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASIS_DATABASES__WAREHOUSE__KIND", "mssql")
    assert basis(repo(tmp_path), "serve", installed={"alpha": lambda: Part(verbs=(verb("serve", lambda _: len(Settings().databases)),))}) == 1
    line = error_line(capsys)
    assert "BASIS_DATABASES__WAREHOUSE__CONNECTION_STRING: Field required" in line
    assert line.endswith("set each variable named, as .env.example shows")


def test_bad_log_settings_name_their_variable(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASIS_LOG_LEVEL", "LOUD")
    assert basis(repo(tmp_path), "serve") == 1
    assert "BASIS_LOG_LEVEL: Input should be" in error_line(capsys)


def test_two_declared_parts_cannot_add_the_same_verb(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    installed = {"alpha": lambda: Part(verbs=(verb("run"),)), "beta": lambda: Part(verbs=(verb("run"),))}
    assert basis(repo(tmp_path, '[tool.basis]\nparts = ["alpha", "beta"]\n'), "run", installed=installed) == 1
    assert error_line(capsys).endswith("the alpha and beta parts both add `basis run`, and a repo cannot have both: declare one of them")


@pytest.mark.parametrize(
    ("part", "problem"),
    [
        (Part(verbs=(verb("check"),)), "the alpha part adds `basis check`, which is core's: install every basis package from one release"),
        (Part(verbs=(verb("serve"), verb("serve"))), "the alpha part adds `basis serve` twice, a defect in its package"),
        (Part(verbs=(verb("serve"),), new={"job": lambda root, _: root}), ""),
    ],
    ids=["a-core-verb", "a-verb-twice", "no-clash"],
)
def test_a_part_cannot_add_a_verb_core_has_nor_one_twice(tmp_path: Path, capsys: pytest.CaptureFixture[str], part: Part, problem: str) -> None:
    code = basis(repo(tmp_path), "serve", installed={"alpha": lambda: part})
    if problem:
        assert code == 1
        assert problem in error_line(capsys)
    else:
        assert code == 0


def test_replay_and_regress_go_to_the_part_whose_names_hold_the_name(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    installed = {"alpha": lambda: Part(runs=runs("alpha", "prices")), "beta": lambda: Part(runs=runs("beta", "income"))}
    folder = repo(tmp_path, '[tool.basis]\nparts = ["alpha", "beta"]\n')
    assert basis(folder, "replay", "income", "R1", installed=installed) == 0
    assert basis(folder, "regress", "prices", installed=installed) == 0
    assert basis(folder, "regress", installed=installed) == 0
    assert capsys.readouterr().out.splitlines() == ["beta replays income R1", "alpha regresses prices", "alpha regresses None", "beta regresses None"]
    assert basis(folder, "replay", "rates", "R1", installed=installed) == 1
    assert error_line(capsys).endswith("nothing in this repo is named 'rates': give one of income, prices")
    assert basis(folder, "replay", "R1", installed=installed) == 1
    assert error_line(capsys).endswith("more than one part of this repo saves runs (alpha, beta): give the job's or model's name before the run's id")


def test_replay_finds_a_run_by_its_id_alone_where_one_part_saves_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo(tmp_path, '[tool.basis]\nparts = ["beta"]\n'), "replay", "R1") == 0
    assert capsys.readouterr().out == "beta replays None R1\n"


@pytest.mark.parametrize("last", ["0", "-3", "ten"])
def test_regress_replays_at_least_one_run_of_each(tmp_path: Path, capsys: pytest.CaptureFixture[str], last: str) -> None:
    folder = repo(tmp_path, '[tool.basis]\nparts = ["beta"]\n')
    assert basis(folder, "regress", "--last", last) == 2
    assert error_line(capsys) == f"ERROR: basis regress: argument --last: give a whole number of 1 or more, not {last!r}: `basis regress --help` says what it takes"


def test_new_adds_what_a_declared_part_makes(tmp_path: Path) -> None:
    def make(root: Path, name: str) -> Path:
        (root / "jobs" / name).mkdir()
        return root / "jobs" / name

    folder = repo(tmp_path)
    assert basis(folder, "new", "job", "rates", installed={"alpha": lambda: Part(new={"job": make})}) == 0
    assert (folder / "jobs/rates").is_dir()


def test_docs_lists_each_parts_pages_and_prints_one_by_its_name_where_one_part_has_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for part, pages in {"alpha": ["tutorial", "reference"], "beta": ["reference"]}.items():
        (tmp_path / "docs" / part).mkdir(parents=True)
        for page in pages:
            (tmp_path / "docs" / part / f"{page}.md").write_text(f"# {part} {page}\n", encoding="utf-8")
    installed = {name: partial(Part, docs=tmp_path / "docs" / name) for name in ("alpha", "beta")}
    folder = repo(tmp_path, '[tool.basis]\nparts = ["alpha", "beta"]\n')
    assert basis(folder, "docs", installed=installed) == 0
    assert capsys.readouterr().out.splitlines() == ["alpha/reference", "alpha/tutorial", "beta/reference"]
    assert basis(folder, "docs", "tutorial", installed=installed) == 0
    assert basis(folder, "docs", "beta/reference", installed=installed) == 0
    assert capsys.readouterr().out == "# alpha tutorial\n# beta reference\n"
    assert basis(folder, "docs", "reference", installed=installed) == 1
    assert error_line(capsys).endswith("more than one part has a page 'reference': give one of alpha/reference, beta/reference")


def test_sync_writes_each_declared_parts_agent_files(tmp_path: Path) -> None:
    files = AgentFiles("basis-alpha", "the alpha block\n", {"alpha-new": "# Make one\n"})
    folder = repo(tmp_path)
    assert basis(folder, "sync", installed={"alpha": lambda: Part(files=lambda: files)}) == 0
    assert "the alpha block" in (folder / "AGENTS.md").read_text(encoding="utf-8")
    assert (folder / ".agents/skills/alpha-new/SKILL.md").read_text(encoding="utf-8") == "# Make one\n"


@pytest.mark.parametrize("argv", [["--help"], ["serve"], ["sync", "--help"]], ids=["help", "a-parts-verb", "a-verbs-help"])
def test_a_verb_a_container_or_scheduler_runs_leaves_git_alone(tmp_path: Path, git: Callable[..., str], argv: list[str]) -> None:
    folder = repo(tmp_path)
    sync.write(folder, [])
    git(folder, "init", "--quiet")
    assert basis(folder, *argv) == 0
    assert git(folder, "config", "--default", "unset", "core.hooksPath") == "unset\n"


def test_new_points_a_clone_that_names_no_hooks_folder_at_the_repos_hooks(tmp_path: Path, git: Callable[..., str]) -> None:
    folder = repo(tmp_path)
    sync.write(folder, [])
    git(folder, "init", "--quiet")
    assert basis(folder, "new", "job", "rates", installed={"alpha": lambda: Part(new={"job": lambda root, name: root / name})}) == 0
    assert git(folder, "config", "core.hooksPath") == ".githooks\n"


def test_sync_refuses_a_file_of_the_repos_own_and_adopt_replaces_it_after_saving_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = repo(tmp_path)
    (folder / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    assert basis(folder, "sync") == 1
    assert error_line(capsys).startswith("ERROR: Dockerfile is the repo's own, at a path basis writes, so basis leaves it alone: run `uv run basis sync --adopt`")
    assert (folder / "Dockerfile").read_text(encoding="utf-8") == "FROM scratch\n"
    assert basis(folder, "sync", "--adopt") == 0
    assert (folder / "Dockerfile.before-basis").read_text(encoding="utf-8") == "FROM scratch\n"
    assert basis(folder, "sync") == 0


def test_a_repo_may_name_the_apt_packages_its_image_needs(tmp_path: Path) -> None:
    folder = repo(tmp_path, ALPHA + 'apt-packages = ["libpq5"]\n')
    assert basis(folder, "sync") == 0
    assert " tini libpq5 " in (folder / "Dockerfile").read_text(encoding="utf-8")


def test_sync_points_the_clone_at_the_hooks_it_writes(tmp_path: Path, git: Callable[..., str]) -> None:
    folder = repo(tmp_path)
    git(folder, "init", "--quiet")
    assert basis(folder, "sync") == 0
    assert git(folder, "config", "core.hooksPath") == ".githooks\n"


def test_a_standalone_command_fails_as_basis_does(capsys: pytest.CaptureFixture[str]) -> None:
    assert command.standalone("basis-alpha", "Make an alpha repo.", [verb("new", raises(BasisError("pricing already exists: give a new folder")))], ["new"]) == 1
    assert capsys.readouterr().err == "ERROR: pricing already exists: give a new folder\n"
