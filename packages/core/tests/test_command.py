import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping
from functools import partial
from importlib.metadata import entry_points, version
from pathlib import Path

import pytest

from basis.core import command, sync
from basis.core.ddl import ledger
from basis.core.errors import BasisError, deliberate
from basis.core.settings import Settings
from basis.core.sync import AgentFiles, stamped
from basis.core.verbs import Part, ProjectError, Runs, Shared, Verb, fail, table

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


LISTINGS: list[str] = []
"""Each part whose names were read, in turn: a test clears it, and counts."""


def runs(prefix: str, *names: str, noun: str = "job", option: str = "--interval") -> Runs:
    """What a part runs, `names`, each of whose shared verbs prints what it was handed, and whose `run`, `resume`,
    `replay` and `regress` take `option`, beside `--last`, which every part's `regress` takes. Its `regress` prints
    JSON, as a part's must."""

    def says(verb: str) -> Callable[[argparse.Namespace], int]:
        def handle(n: argparse.Namespace) -> int:
            given = {key: value for key, value in vars(n).items() if value is not None}
            sys.stdout.write(f"{prefix} {verb} {' '.join(f'{key}={value}' for key, value in given.items())}\n")
            return 0

        return handle

    def regresses(n: argparse.Namespace) -> int:
        sys.stdout.write(json.dumps({key: value for key, value in vars(n).items() if value is not None}) + "\n")
        return 0

    def options(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(option)

    def regress_options(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(option)
        parser.add_argument("--last")

    def listed() -> dict[str, str]:
        LISTINGS.append(prefix)
        return {name: f"{prefix}.{name}" for name in names}

    return Runs(
        noun,
        listed,
        run=Shared(f"run a {noun}", says("runs"), options),
        describe=Shared(f"describe a {noun}", says("describes")),
        resume=Shared(f"resume a {noun}'s run", says("resumes"), options),
        replay=Shared(f"replay a {noun}'s run", says("replays"), options),
        regress=Shared(f"regress {noun}s", regresses, regress_options),
        ddl=Shared(f"print a {noun}'s tables", says("prints")),
    )


INSTALLED: dict[str, Callable[[], Part]] = {"alpha": lambda: Part(verbs=(verb("serve"),)), "beta": lambda: Part(verbs=(verb("render"),), runs=runs("beta"))}
BOTH = '[tool.basis]\nparts = ["alpha", "beta"]\n'
JOBS_AND_REPORTS: dict[str, Callable[[], Part]] = {
    "alpha": lambda: Part(runs=runs("alpha", "prices", "shared")),
    "beta": lambda: Part(verbs=(verb("render"),), runs=runs("beta", "summary", "shared", noun="report", option="--regions")),
}
"""A part that runs jobs and one that runs reports, which both run something named `shared`."""


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
    """The one ERROR line a failure prints: the last line, after the record that says the command finished."""
    lines = capsys.readouterr().err.splitlines()
    assert [line for line in lines if line.startswith("ERROR")] == lines[-1:]
    assert len(lines) >= 2, lines
    assert json.loads(lines[-2])["message"].startswith("finished ")
    return lines[-1]


def test_help_lists_the_declared_parts_verbs_and_the_core_verbs_they_give_something_to_do(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo(tmp_path), "--help") == 0
    listed = [line.split()[0] for line in capsys.readouterr().out.splitlines() if line.startswith("  ") and not line.lstrip().startswith("-")]
    assert listed == ["check", "review", "sync", "upgrade", "doctor", "serve"]


def test_a_verb_runs_from_a_folder_inside_the_repo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo(tmp_path) / "jobs", "serve") == 0
    assert capsys.readouterr().out == "serve\n"


def test_a_verb_of_an_installed_part_the_repo_does_not_declare_says_how_to_declare_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo(tmp_path), "replay", "prices", "RUN") == 1
    assert error_line(capsys).endswith("comes with the beta part, which " + str(tmp_path / "pyproject.toml") + " does not declare: add 'beta' to its [tool.basis] parts")


def test_a_verb_no_installed_part_has_does_not_parse(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo(tmp_path), "nonsense") == 2
    assert "invalid choice: 'nonsense'" in error_line(capsys)


def test_a_declared_part_that_no_package_provides_says_to_add_the_package_its_entry_point_is_named_for(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo(tmp_path, '[tool.basis]\nparts = ["report"]\n'), "check") == 1
    assert error_line(capsys).endswith(
        "declares the part 'report', which no installed package provides: add the package whose basis.verbs entry point is named 'report' to its [project]"
        " dependencies, and run `uv sync --all-packages`"
    )


@pytest.mark.parametrize(
    ("pyproject", "problem"),
    [
        ('[project]\nname = "repo"\n', 'declares no basis parts: add [tool.basis] parts = ["job"]'),
        ("[tool.basis]\nparts = []\n", "parts is not a list of part names"),
        ('[tool.basis]\nparts = "alpha"\n', "parts is not a list of part names"),
        ('[tool.basis]\nparts = ["alpha", "alpha"]\n', "names a part twice"),
        ('[tool.basis]\npart = ["alpha"]\n', "has part, which basis does not read"),
        ("[tool.basis\n", "is not TOML"),
        (
            '[tool.basis]\nparts = ["alpha"]\n\n[tool.basis.api]\nreleases = "v*"\n',
            "has [tool.basis.api], a part's own table, and its [tool.basis] parts does not declare the api part: add 'api' to its parts, or remove the table",
        ),
        ('[tool.basis]\nparts = ["alpha"]\nbeta = "v*"\n', "has beta, which basis does not read"),
        ('[tool.basis]\nparts = "alpha"\n\n[tool.basis.alpha]\nreleases = "v*"\n', "parts is not a list of part names"),
        ('[tool.basis]\nparts = ["alpha"]\nalpha = "v*"\n', "[tool.basis] alpha is not a table: give the alpha part's own as [tool.basis.alpha]"),
    ],
    ids=[
        "no-parts",
        "none",
        "not-a-list",
        "twice",
        "an-unknown-key",
        "not-toml",
        "an-undeclared-parts-table",
        "an-unknown-key-beside-the-parts",
        "a-parts-table-whose-parts-are-not-a-list",
        "a-parts-table-that-is-not-one",
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
    assert error_line(capsys) == "ERROR: while replaying: no run R1 of prices: give another"


def test_a_builtin_error_raised_on_purpose_prints_as_one_line_where_the_call_says_so(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def handle(_: argparse.Namespace) -> int:
        with deliberate(LookupError):
            msg = "no run R1: is it the right snapshot root?"
            raise LookupError(msg)

    assert basis(repo(tmp_path), "serve", installed={"alpha": lambda: Part(verbs=(verb("serve", handle),))}) == 1
    assert error_line(capsys) == "ERROR: no run R1: is it the right snapshot root?"


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
    assert error_line(capsys) == (
        "ERROR: ETL_DATABASES__WAREHOUSE__KIND, ETL_SNAPSHOT_ROOT are set, and basis reads only BASIS_ variables now: rename ETL_DATABASES__WAREHOUSE__KIND"
        " to BASIS_DATABASES__WAREHOUSE__KIND, ETL_SNAPSHOT_ROOT to BASIS_SNAPSHOT_ROOT, where they are set and in each .env file"
    )


def test_version_names_basis_core_and_each_installed_part_anywhere(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(tmp_path, "--version") == 0
    core, *parts = capsys.readouterr().out.rstrip("\n").split(", ")  # the parts in the order their packages are found
    installed = {f"{entry.dist.name} {entry.dist.version}" for entry in entry_points(group=command.GROUP) if entry.dist is not None}
    assert (core, sorted(parts)) == (f"basis-core {version('basis-core')}", sorted(installed))
    assert {found.rpartition(" ")[2] for found in (core, *parts)} == {version("basis-core")}  # one version, with no part installed too


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
    installed = {"alpha": lambda: Part(verbs=(verb("serve"),)), "beta": lambda: Part(verbs=(verb("serve"),))}
    assert basis(repo(tmp_path, BOTH), "serve", installed=installed) == 1
    assert error_line(capsys).endswith("the alpha and beta parts both add `basis serve`, and a repo cannot have both: declare one of them")


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


def test_a_shared_verb_goes_to_the_only_part_that_has_it_whatever_its_name(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = repo(tmp_path, '[tool.basis]\nparts = ["beta"]\n')
    assert basis(folder, "run", "beta.toys.listed", "--interval", "2026-11-05..2026-11-06") == 0
    assert basis(folder, "replay", "R1") == 0
    assert capsys.readouterr().out.splitlines() == ["beta runs name=beta.toys.listed interval=2026-11-05..2026-11-06", "beta replays run_id=R1"]


def test_a_shared_verb_goes_to_the_part_that_runs_the_name_and_its_parser_reads_the_rest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = repo(tmp_path, BOTH)
    assert basis(folder, "run", "summary", "--regions", "EU", installed=JOBS_AND_REPORTS) == 0
    assert basis(folder, "run", "prices", "--interval", "2026-11-05..2026-11-06", installed=JOBS_AND_REPORTS) == 0
    assert basis(folder, "replay", "summary", "R1", installed=JOBS_AND_REPORTS) == 0
    assert basis(folder, "regress", "prices", installed=JOBS_AND_REPORTS) == 0
    assert capsys.readouterr().out.splitlines() == [
        "beta runs name=summary regions=EU",
        "alpha runs name=prices interval=2026-11-05..2026-11-06",
        "beta replays name=summary run_id=R1",
        '{"name": "prices"}',
    ]
    assert basis(folder, "run", "prices", "--regions", "EU", installed=JOBS_AND_REPORTS) == 2
    assert error_line(capsys) == "ERROR: basis run: unrecognized arguments: --regions EU: `basis run --help` says what it takes"


@pytest.mark.parametrize(
    ("argv", "problem", "code"),
    [
        (["run", "rates"], "nothing in this repo is named 'rates': give one of prices, shared, summary", 1),
        (["replay", "rates", "R1"], "nothing in this repo is named 'rates': give one of prices, shared, summary", 1),
        (["run", "shared"], "the alpha and beta parts both run something named 'shared': rename one of them", 1),
        (["describe"], "the alpha and beta parts both run something named 'shared': rename one of them", 1),
        (["replay", "R1"], "more than one part of this repo has `basis replay` (alpha, beta): give the job's or report's name before the run's id", 1),
        (["run", "--interval", "2026-11-05..2026-11-06"], "more than one part of this repo has `basis run` (alpha, beta): give the job's or report's name first", 1),
        (["replay", "summary"], "basis replay summary takes the id of one of its runs after the name: give it, as `basis replay summary RUN_ID`", 2),
    ],
    ids=["no-such-name", "no-such-name-before-a-run-id", "a-name-two-parts-run", "a-listing-with-a-name-two-parts-run", "a-run-id-alone", "options-alone", "a-name-alone"],
)
def test_where_several_parts_share_a_verb_its_name_must_be_one_of_theirs(tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str], problem: str, code: int) -> None:
    assert basis(repo(tmp_path, BOTH), *argv, installed=JOBS_AND_REPORTS) == code
    assert error_line(capsys) == f"ERROR: {problem}"


def test_without_a_name_describe_lists_every_part_and_regress_replays_every_part_and_ddl_prints_the_ledger(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    installed = {"alpha": lambda: Part(runs=runs("alpha", "prices")), "beta": lambda: Part(runs=runs("beta", "summary", noun="report"))}
    folder = repo(tmp_path, BOTH)
    assert basis(folder, "describe", installed=installed) == 0
    assert json.loads(capsys.readouterr().out) == {"prices": "alpha.prices", "summary": "beta.summary"}
    assert basis(folder, "regress", "--last", "5", installed=installed) == 0
    assert json.loads(capsys.readouterr().out) == {"alpha": {"last": "5"}, "beta": {"last": "5"}}  # one JSON document, by part
    assert basis(folder, "ddl", "--ledger", "etl.publishes", "--dialect", "oracle", installed=installed) == 0
    assert capsys.readouterr().out == ledger("etl.publishes", "oracle")  # core's, not a part's
    assert basis(folder, "ddl", "--dialect", "oracle", installed=installed) == 2
    assert error_line(capsys).startswith("ERROR: basis ddl: the following arguments are required: --ledger")


def test_without_a_name_regress_takes_only_the_options_every_part_takes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo(tmp_path, BOTH), "regress", "--regions", "EU", installed=JOBS_AND_REPORTS) == 2
    assert error_line(capsys) == ("ERROR: basis regress: --regions is not an option of the alpha part's `basis regress`: name the job or report it is for, as `basis regress NAME --regions ...`")
    assert capsys.readouterr().out == ""  # no part ran


def test_a_shared_verbs_help_after_a_name_gives_that_parts_options_before_its_run_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = repo(tmp_path, BOTH)
    for verb in ("replay", "resume"):
        assert basis(folder, verb, "summary", "--help", installed=JOBS_AND_REPORTS) == 0
        assert f"usage: basis {verb} [-h] [--regions REGIONS] [NAME] RUN_ID" in capsys.readouterr().out


def test_a_shared_verb_reads_what_the_parts_run_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    LISTINGS.clear()
    assert basis(repo(tmp_path, BOTH), "replay", "summary", "R1", installed=JOBS_AND_REPORTS) == 0
    assert sorted(LISTINGS) == ["alpha", "beta"]
    assert capsys.readouterr().out == "beta replays name=summary run_id=R1\n"


def test_a_shared_verb_that_argparse_reaches_runs_with_nothing_after_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """As after `--`, which ends basis's own options."""
    assert basis(repo(tmp_path, '[tool.basis]\nparts = ["beta"]\n'), "--", "describe", installed={"beta": lambda: Part(runs=runs("beta", "summary"))}) == 0
    assert json.loads(capsys.readouterr().out) == {"summary": "beta.summary"}


def test_describe_without_a_name_lists_what_the_only_part_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo(tmp_path, '[tool.basis]\nparts = ["beta"]\n'), "describe", installed={"beta": lambda: Part(runs=runs("beta", "summary"))}) == 0
    assert json.loads(capsys.readouterr().out) == {"summary": "beta.summary"}


def test_help_lists_the_shared_verbs_for_what_the_parts_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo(tmp_path, BOTH), "--help", installed=JOBS_AND_REPORTS) == 0
    out = capsys.readouterr().out
    assert "\nFor its jobs and reports:\n  run       jobs: run a job | reports: run a report\n  describe  jobs: describe a job | reports: describe a report\n" in out
    assert "From the alpha part" not in out  # it adds no verb of its own
    assert "\nFrom the beta part:\n  render    the render verb\n" in out


def test_a_shared_verbs_help_gives_the_arguments_of_the_part_its_name_names(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = repo(tmp_path, BOTH)
    assert basis(folder, "run", "--help", installed=JOBS_AND_REPORTS) == 0
    assert capsys.readouterr().out == (
        "usage: basis run [-h] NAME ...\n\n"
        "With a job's NAME, it does as the alpha part says: run a job.\n"
        "With a report's NAME, it does as the beta part says: run a report.\n"
        "`basis run NAME --help` gives the arguments of NAME's part.\n"
    )
    assert basis(folder, "run", "summary", "--help", installed=JOBS_AND_REPORTS) == 0
    assert "usage: basis run [-h] [--regions REGIONS] NAME" in capsys.readouterr().out


def test_where_one_part_has_a_shared_verb_its_help_names_what_the_part_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo(tmp_path, '[tool.basis]\nparts = ["beta"]\n'), "replay", "--help") == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: basis replay [-h] [--interval INTERVAL] [JOB] RUN_ID\n\nreplay a job's run\n")
    assert "the job whose run it is; without it, the run is found by its id" in " ".join(out.split())


def test_a_part_cannot_add_a_verb_core_shares_among_the_parts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo(tmp_path), "run", installed={"alpha": lambda: Part(verbs=(verb("run"),))}) == 1
    assert error_line(capsys).endswith("the alpha part adds `basis run`, which is core's: install every basis package from one release, with `uv sync`")


def test_a_part_refuses_the_variables_an_earlier_version_read_before_any_verb_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    def renamed(environ: Mapping[str, str]) -> None:
        if "ALPHA_ROOT" in environ:
            msg = "ALPHA_ROOT is set, and alpha reads BASIS_ALPHA__ROOT now: rename it"
            raise BasisError(msg)

    monkeypatch.setenv("ALPHA_ROOT", "here")
    assert basis(repo(tmp_path), "check", installed={"alpha": lambda: Part(verbs=(verb("serve"),), renamed=renamed)}) == 1
    assert error_line(capsys) == "ERROR: ALPHA_ROOT is set, and alpha reads BASIS_ALPHA__ROOT now: rename it"


@pytest.mark.parametrize("count", ["0", "-3", "ten"])
def test_a_count_a_verb_takes_is_a_whole_number_of_one_or_more(count: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match=f"give a whole number of 1 or more, not {count!r}"):
        command.positive(count)


@pytest.mark.parametrize(("text", "label"), [("desk=rates", ("desk", "rates")), ("desk=", ("desk", "")), ("note=a=b", ("note", "a=b"))])
def test_a_label_is_a_key_and_the_value_after_its_first_equals_sign(text: str, label: tuple[str, str]) -> None:
    assert command.label(text) == label


@pytest.mark.parametrize("text", ["desk", "=rates"])
def test_a_label_without_a_key_and_an_equals_sign_is_refused(text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match=f"a label is KEY=VALUE, not {text!r}"):
        command.label(text)


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
    skill = "---\nname: alpha-new\ndescription: Make one.\n---\n"
    files = AgentFiles("basis-alpha", "the alpha block\n", {"alpha-new": skill})
    folder = repo(tmp_path)
    assert basis(folder, "sync", installed={"alpha": lambda: Part(files=lambda: files)}) == 0
    assert "the alpha block" in (folder / "AGENTS.md").read_text(encoding="utf-8")
    assert (folder / ".agents/skills/alpha-new/SKILL.md").read_text(encoding="utf-8") == stamped(skill)


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
    assert error_line(capsys) == "ERROR: pricing already exists: give a new folder"


@pytest.mark.parametrize(
    ("argv", "code"),
    [(["serve"], 0), (["serve", "--port", "x"], 2), (["fails"], 1), (["raises"], 1)],
    ids=["success", "a-command-line-that-does-not-parse", "a-failure-it-returns", "an-error-it-raises"],
)
def test_every_command_ends_with_a_record_of_its_exit_code_before_any_error_line(tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str], code: int) -> None:
    def port(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--port", type=int)

    def fails(_: argparse.Namespace) -> int:
        fail("the port is taken", "give another")
        sys.stderr.write('{"message": "a record logged after the failure"}\n')
        return 1

    verbs = (Verb("serve", "serve", port, says("served")), verb("fails", fails), verb("raises", raises(BasisError("no port: give one"))))
    assert basis(repo(tmp_path), *argv, installed={"alpha": lambda: Part(verbs=verbs)}) == code
    lines = capsys.readouterr().err.splitlines()
    finished = next(json.loads(line) for line in lines if line.startswith("{") and json.loads(line)["message"].startswith("finished "))
    assert (finished["message"], finished["verb"], finished["exit_code"]) == (f"finished {argv[0]}", argv[0], code)
    assert finished["ms"] >= 0
    errors = [index for index, line in enumerate(lines) if line.startswith("ERROR")]
    assert errors == ([len(lines) - 1] if code else [])  # one ERROR line, the last, held back until the record


def test_a_verb_of_more_than_one_installed_part_the_repo_does_not_declare_names_them_all(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    installed = {**JOBS_AND_REPORTS, "gamma": lambda: Part(verbs=(verb("serve"),))}
    assert basis(repo(tmp_path, '[tool.basis]\nparts = ["gamma"]\n'), "replay", "summary", "R1", installed=installed) == 1
    assert error_line(capsys).endswith(
        f"`basis replay` comes with the alpha and beta parts, which {tmp_path / 'pyproject.toml'} does not declare: add the one the repo has, 'alpha' or 'beta', to its [tool.basis] parts"
    )


@pytest.mark.parametrize("option", [["--no-qa"], ["--settings-from", "."]], ids=["no-qa", "settings-from"])
def test_the_options_of_a_full_check_are_refused_without_full(tmp_path: Path, capsys: pytest.CaptureFixture[str], option: list[str]) -> None:
    assert basis(repo(tmp_path), "check", *option) == 1
    assert error_line(capsys).startswith("ERROR: --settings-from and --no-qa change what `basis check --full` does, and were given without --full")


def test_a_parts_verb_is_given_the_repo_basis_found(tmp_path: Path) -> None:
    """A handler that needs its repo reads the root `basis` resolved, here from a folder inside it."""
    seen: list[object] = []

    def handle(namespace: argparse.Namespace) -> int:
        seen.append(namespace.repo_root)
        return 0

    root = repo(tmp_path)
    assert basis(root / "jobs", "serve", installed={"alpha": lambda: Part(verbs=(verb("serve", handle),))}) == 0
    assert seen == [root]


def test_a_declared_part_reads_its_own_table(tmp_path: Path) -> None:
    folder = repo(tmp_path, '[tool.basis]\nparts = ["alpha"]\n\n[tool.basis.alpha]\nreleases = "v*"\n')
    read: list[Mapping[str, object]] = []

    def reads(_: argparse.Namespace) -> int:
        read.append(table(folder, "alpha"))
        return 0

    assert basis(folder, "serve", installed={"alpha": lambda: Part(verbs=(verb("serve", reads),))}) == 0
    assert read == [{"releases": "v*"}]
    (folder / "pyproject.toml").write_text(ALPHA, encoding="utf-8")
    assert table(folder, "alpha") == {}


def test_sync_writes_the_files_a_declared_part_makes(tmp_path: Path) -> None:
    document = f'{{\n  "x-basis": "{sync.MARKER}",\n  "openapi": "3.1.0"\n}}\n'
    files = AgentFiles("basis-alpha", "the alpha block\n", {}, made={"openapi.json": lambda: document})
    folder = repo(tmp_path)
    assert basis(folder, "sync", installed={"alpha": lambda: Part(files=lambda: files)}) == 0
    assert (folder / "openapi.json").read_text(encoding="utf-8") == document


def test_a_part_reads_its_table_as_basis_reads_the_parts(tmp_path: Path) -> None:
    folder = repo(tmp_path, "[tool.basis\n")
    with pytest.raises(ProjectError, match=r"pyproject.toml is not TOML: .*: fix it$"):
        table(folder, "alpha")
    (folder / "pyproject.toml").write_text('[tool.basis]\nparts = ["alpha"]\nalpha = 1\n', encoding="utf-8")
    with pytest.raises(ProjectError, match=r"\[tool.basis\] alpha is not a table: give the alpha part's own as \[tool.basis.alpha\], or remove it$"):
        table(folder, "alpha")
