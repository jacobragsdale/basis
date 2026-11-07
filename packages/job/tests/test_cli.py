import json
import os
from datetime import UTC, datetime
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from basis.core import command, snapshots
from basis.core.snapshots import RunRecord
from basis.core.storage import Root
from basis.job import cli
from basis.job.example import job

EXAMPLE = Path(job.__file__).parent
RUN = ["run", "basis.job.example.job", "--params", str(EXAMPLE / "params/default.json"), "--data", str(EXAMPLE / "tests/fixture"), "--interval", "2026-11-05..2026-11-06"]


@pytest.fixture(autouse=True)
def snapshot_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in [name for name in os.environ if name.startswith("BASIS_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("BASIS_SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    return tmp_path / "snapshots"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A job repo's root, as `basis` finds it: a pyproject.toml that declares the job part."""
    folder = tmp_path / "repo"
    folder.mkdir()
    (folder / "pyproject.toml").write_text('[project]\nname = "repo"\n\n[tool.basis]\nparts = ["job"]\n', encoding="utf-8")
    return folder


def basis(repo: Path, *argv: str) -> int:
    return command.run(argv, cwd=repo, installed={"job": cli.part})


def error_line(capsys: pytest.CaptureFixture[str]) -> str:
    """The one line a failure prints, after the run's log records."""
    line = capsys.readouterr().err.splitlines()[-1]
    assert line.startswith("ERROR: ")
    return line


def only_error(err: str) -> str:
    """The one ERROR line in what a failure printed on stderr: the last line, after the record that says the command
    finished, with no traceback."""
    lines = err.splitlines()
    assert [line for line in lines if line.startswith("ERROR")] == lines[-1:]
    assert json.loads(lines[-2])["message"].startswith("finished ")
    assert "traceback" not in err
    return lines[-1]


def ran(repo: Path, capsys: pytest.CaptureFixture[str]) -> str:
    """The id of a run of the example on its fixture."""
    assert basis(repo, *RUN) == 0
    return next(json.loads(line)["run_id"] for line in capsys.readouterr().err.splitlines() if '"run_id"' in line)


def test_basis_finds_the_job_part_through_its_entry_point() -> None:
    runs = command.installed()["job"]().runs
    assert [name for name in ("run", "resume", "describe", "ddl") if runs is not None and getattr(runs, name) is not None] == ["run", "resume", "describe", "ddl"]


def test_a_run_that_passes_every_check_exits_zero(repo: Path) -> None:
    assert basis(repo, *RUN) == 0


def test_a_run_that_cannot_publish_exits_nonzero_with_the_reason_and_the_fix(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo, *RUN, "--publish") == 1
    line = error_line(capsys)
    assert "wrote nothing, because database 'warehouse' is not configured, and the run publishes to it: set BASIS_DATABASES__WAREHOUSE__*" in line
    assert line.endswith("fix it, and run the job again")


def test_a_run_whose_params_do_not_validate_names_the_field_and_the_file(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "bad.json").write_text('{"include_closed_acounts": true}', encoding="utf-8")
    assert basis(repo, *RUN[:3], str(tmp_path / "bad.json"), *RUN[4:]) == 1
    assert only_error(capsys.readouterr().err).startswith(
        f"ERROR: while reading basis.job.example.job's params from {tmp_path / 'bad.json'}: Params is not valid: include_closed_acounts: Extra inputs"
    )


def test_describe_prints_the_contract_as_json(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo, "describe", "basis.job.example.job") == 0
    described = json.loads(capsys.readouterr().out)
    assert described["outputs"]["positions"] | {"columns": []} == {
        "database": "warehouse",
        "table": "daily_positions",
        "write": "ReplaceWhere",
        "key": ["trade_date", "account_id", "security_id"],
        "columns": [],
    }
    assert described["inputs"]["accounts"]["empty"] == "fail"


def test_the_ledger_ddl_prints_without_a_job(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo, "ddl", "--ledger", "etl.publishes", "--dialect", "mssql") == 0
    assert capsys.readouterr().out.startswith("CREATE TABLE etl.publishes (")


def test_an_unknown_job_exits_nonzero_and_says_what_to_give(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo, "describe", "no_such_job") == 1
    assert error_line(capsys).endswith("or a job's module path, as jobs.NAME.job")


@pytest.mark.parametrize("named", [True, False], ids=["with-its-job", "by-its-id-alone"])
def test_a_replay_of_a_run_on_the_same_code_changes_nothing(repo: Path, capsys: pytest.CaptureFixture[str], *, named: bool) -> None:
    run_id = ran(repo, capsys)
    assert basis(repo, "replay", *(["basis.job.example.job"] if named else []), run_id) == 0
    replayed = json.loads(capsys.readouterr().out)
    assert (replayed["replay_of"], replayed["status"], replayed["differences"]) == (run_id, "checked", [])


def test_a_resume_of_a_run_that_never_published_is_refused_in_one_line(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_id = ran(repo, capsys)
    assert basis(repo, "resume", run_id) == 1
    assert only_error(capsys.readouterr().err).startswith(f"ERROR: run {run_id} is checked and never published")


def test_a_run_told_again_records_it_and_resume_takes_again_too(repo: Path, snapshot_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A resume publishes as its run was told to; `--again` tells a resume of a run that was not."""
    assert basis(repo, *RUN, "--again") == 0
    run_id = next(json.loads(line)["run_id"] for line in capsys.readouterr().err.splitlines() if '"run_id"' in line)
    assert snapshots.find(Root.at(snapshot_root), run_id).record().again
    assert basis(repo, "resume", run_id, "--again") == 1
    assert error_line(capsys).startswith(f"ERROR: run {run_id} is checked and never published")


@pytest.mark.parametrize(
    "case",
    [
        (["replay", "R1"], {"BASIS_SNAPSHOT_ROOT": None}, 1),
        (["replay", "R1"], {}, 1),
        (["replay", "basis.job.example.job", "R1"], {}, 1),
        (["resume", "R1"], {}, 1),
        (["describe", "no_such_job"], {}, 1),
        (["run", "basis.job.example.job", "--params", "nope.json"], {}, 2),
        (RUN, {"BASIS_DATABASES__SOURCE__KIND": "mssql"}, 1),
        (RUN, {"ETL_SNAPSHOT_ROOT": "snapshots"}, 1),
        (["ddl", "--dialect", "mssql"], {}, 1),
        (["new", "job", "Bad"], {}, 1),
        (["regress", "--last", "0"], {}, 2),
    ],
    ids=[
        "no-snapshot-root",
        "no-such-run",
        "no-such-run-of-the-job",
        "no-run-to-resume",
        "no-such-job",
        "no-params-file",
        "settings-that-do-not-validate",
        "an-old-variable",
        "ddl-of-nothing",
        "a-bad-job-name",
        "regress-nothing",
    ],
)
def test_each_failure_the_caller_can_fix_is_one_line_with_no_traceback(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, case: tuple[list[str], dict[str, str | None], int]
) -> None:
    argv, variables, code = case
    for name, value in variables.items():
        if value is None:
            monkeypatch.delenv(name)
        else:
            monkeypatch.setenv(name, value)
    assert basis(repo, *argv) == code
    only_error(capsys.readouterr().err)


def test_regress_replays_each_job_on_its_own(repo: Path, snapshot_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_id = ran(repo, capsys)
    source = snapshots.find(Root.at(snapshot_root), run_id)
    source.save_record(RunRecord.model_validate(source.record().model_dump() | {"status": "published"}))
    assert cli.regress_jobs(cli.RegressArgs(("no_such_job", "basis.job.example.job"), 10, {}, None)) == 1
    captured = capsys.readouterr()
    assert [(entry["job"], entry["run_id"], entry["differences"]) for entry in json.loads(captured.out)] == [("basis.job.example.job", run_id, [])]
    assert [line for line in captured.err.splitlines() if line.startswith("ERROR")] == [
        "ERROR: while regressing no_such_job: no job named 'no_such_job', and it is no module path: give one the repo lists (none yet), or a job's module path, as jobs.NAME.job"
    ]


RECORD = {"run_id": "R1", "job": "prices", "module": "jobs.prices.job", "version": "0.0.0", "started_at": datetime(2026, 11, 6, tzinfo=UTC), "params_path": "p.json", "params_sha256": "0"}
READ = {"inputs": {"trades": {"source": "source: sql/trades.sql", "rows": 5, "read_ms": 1.0}}}


@pytest.mark.parametrize(
    ("fields", "publish", "failure"),
    [
        ({"status": "checked"}, False, None),
        ({"status": "published"}, True, None),
        ({"status": "partial"}, True, ("run R1 of prices published to some of its databases and failed on another", "fix what its publishes record, then `basis resume prices R1`")),
        ({"status": "failed", "error": "OperationalError: login failed"}, False, ("run R1 of prices failed: OperationalError: login failed", "fix it, and run the job again")),
        (
            {"status": "failed", "reasons": ["a", "b"]} | READ,
            False,
            ("run R1 of prices wrote nothing, because a, and 1 more the log lists", "fix each, and run the job again; `basis replay prices R1` reruns it on what it read"),
        ),
        ({"status": "failed", "reasons": ["a"]}, True, ("run R1 of prices wrote nothing, because a", "fix it, and run the job again")),
        (
            {"status": "failed", "publishes": {"warehouse": {"status": "failed", "at": datetime(2026, 11, 6, tzinfo=UTC), "error": "deadlock"}}},
            True,
            ("run R1 of prices committed to no database: warehouse: deadlock", "fix it, and run the job again"),
        ),
        (
            {"status": "checked"},
            True,
            ("run R1 of prices ended checked with no reason recorded, when it was asked to publish", "its log records say what happened; run it again, and report it if it does the same"),
        ),
    ],
    ids=["checked", "published", "partial", "an-error", "reasons-and-inputs", "a-reason-before-loading", "no-database-committed", "checked-when-asked-to-publish"],
)
def test_a_runs_failure_says_why_and_how_to_fix_it(fields: dict[str, object], *, publish: bool, failure: tuple[str, str] | None) -> None:
    assert cli.failure_of(RunRecord.model_validate(RECORD | fields), publish=publish) == failure


OVERLAPPED = {
    "status": "failed",
    "at": datetime(2026, 11, 6, tzinfo=UTC),
    "error": "PublishRefusedError: prices appends, and runs R0 already published an interval that overlaps 2026-11-05..2026-11-06: pass --again to insert its rows a second time",
}
"""A database's refusal of a plain insert for an interval its ledger has."""


@pytest.mark.parametrize(
    ("fields", "failure"),
    [
        ({"status": "partial", "again": True}, ("run R1 of prices published to some of its databases and failed on another", "fix what its publishes record, then `basis resume prices R1 --again`")),
        (
            {"status": "partial", "publishes": {"beta": OVERLAPPED}},
            ("run R1 of prices published to some of its databases and failed on another", "fix what its publishes record, then `basis resume prices R1 --again`"),
        ),
        (
            {"status": "failed", "publishes": {"alpha": OVERLAPPED}},
            (f"run R1 of prices committed to no database: alpha: {OVERLAPPED['error']}", "run the job again with --again, if its rows are meant to be inserted a second time"),
        ),
    ],
    ids=["a-partial-run-told-again", "a-partial-run-a-ledger-refused", "a-run-a-ledger-refused"],
)
def test_a_fix_carries_again_where_the_next_attempt_needs_it(fields: dict[str, object], failure: tuple[str, str]) -> None:
    assert cli.failure_of(RunRecord.model_validate(RECORD | fields), publish=True) == failure


def test_a_repo_made_before_basis_has_a_release_fails_with_one_error_line_that_names_the_fix(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.scaffold(["new", "repo", "pricing-etl", "--dir", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    line = only_error(captured.err)
    assert line.startswith("ERROR: basis has no release yet")
    assert line.endswith("pass --framework-path with the clone's folder")
    assert not (tmp_path / "pricing-etl").exists()


def test_basis_job_names_every_installed_parts_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.scaffold(["--version"]) == 0
    core, *parts = capsys.readouterr().out.rstrip("\n").split(", ")  # the parts in the order their packages are found
    installed = {f"{entry.dist.name} {entry.dist.version}" for entry in entry_points(group=command.GROUP) if entry.dist is not None}
    assert (core, sorted(parts)) == ("basis-core 0.0.0", sorted(installed))
    assert "basis-job 0.0.0" in installed
