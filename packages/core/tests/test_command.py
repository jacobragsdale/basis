import argparse
import os
import sys
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path

import pytest

from basis.core import command
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
    assert listed == ["check", "serve"]


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


def test_an_error_with_its_fix_prints_as_its_line_alone_with_its_notes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    error = LookupError("no run R1\nof prices: give another")
    error.add_note("while replaying")
    assert basis(repo(tmp_path), "serve", installed={"alpha": lambda: Part(verbs=(verb("serve", raises(error)),))}) == 1
    captured = capsys.readouterr()
    assert captured.err == "ERROR: while replaying: no run R1 of prices: give another\n"


def test_a_defect_logs_its_traceback_before_its_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo(tmp_path), "serve", installed={"alpha": lambda: Part(verbs=(verb("serve", raises(KeyError("prices"))),))}) == 1
    err = capsys.readouterr().err
    assert '"traceback":' in err.splitlines()[0]
    assert err.splitlines()[-1] == "ERROR: KeyError: 'prices': a defect; the log record above holds its traceback"


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


def test_a_part_cannot_add_a_verb_core_has(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo(tmp_path), "check", installed={"alpha": lambda: Part(verbs=(verb("check"),))}) == 1
    assert "the alpha part adds `basis check`, which is a verb of core's" in error_line(capsys)


def test_replay_and_regress_go_to_the_part_whose_names_hold_the_name(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    installed = {"alpha": lambda: Part(runs=runs("alpha", "prices")), "beta": lambda: Part(runs=runs("beta", "income"))}
    folder = repo(tmp_path, '[tool.basis]\nparts = ["alpha", "beta"]\n')
    assert basis(folder, "replay", "income", "R1", installed=installed) == 0
    assert basis(folder, "regress", "prices", installed=installed) == 0
    assert basis(folder, "regress", installed=installed) == 0
    assert capsys.readouterr().out.splitlines() == ["beta replays income R1", "alpha regresses prices", "alpha regresses None", "beta regresses None"]
    assert basis(folder, "replay", "rates", "R1", installed=installed) == 1
    assert error_line(capsys).endswith("nothing in this repo is named 'rates': give one of income, prices")


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
    assert basis(folder, "sync", installed={"alpha": lambda: Part(files=files)}) == 0
    assert "the alpha block" in (folder / "AGENTS.md").read_text(encoding="utf-8")
    assert (folder / ".agents/skills/alpha-new/SKILL.md").read_text(encoding="utf-8") == "# Make one\n"


def test_a_standalone_command_fails_as_basis_does(capsys: pytest.CaptureFixture[str]) -> None:
    assert command.standalone("basis-alpha", "Make an alpha repo.", [verb("new", raises(FileExistsError("pricing already exists: give a new folder")))], ["new"]) == 1
    assert capsys.readouterr().err == "ERROR: pricing already exists: give a new folder\n"
