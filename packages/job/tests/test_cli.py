import json
import os
import shutil
from datetime import UTC, datetime
from importlib.metadata import entry_points, version
from pathlib import Path

import pytest

from basis.core import command, snapshots
from basis.core.new import UNRELEASED
from basis.core.snapshots import RunRecord
from basis.core.storage import Root
from basis.job import cli, new, runs
from basis.job.diff import OperationDiff
from basis.job.example import job

EXAMPLE = Path(job.__file__).parent
RUN = ["run", "basis.job.example.job", "--params", str(EXAMPLE / "params/default.json"), "--data", str(EXAMPLE / "tests/fixture"), "--interval", "2026-11-05..2026-11-06"]


@pytest.fixture(autouse=True)
def snapshot_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The process's environment as a pod's deployment sets it, in dev, where the user has no file of settings."""
    for name in [name for name in os.environ if name.startswith("BASIS_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("BASIS_ENV", "dev")
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


def test_a_run_that_reads_a_source_and_names_no_environment_is_refused_with_the_fix(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BASIS_ENV")
    partly = tmp_path / "partly"
    partly.mkdir()
    for path in (EXAMPLE / "tests/fixture").iterdir():
        if path.name != "trades.csv":  # so the run reads trades from its database
            shutil.copy(path, partly / path.name)
    assert basis(repo, *RUN[:-4], "--data", str(partly)) == 1
    assert error_line(capsys).endswith("names no environment, which basis never assumes: give --env dev|qa|prod, or set BASIS_ENV")


def test_a_run_on_files_alone_needs_no_environment_and_runs_as_main_py_does(repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixture's interval and time, and the repo's own snapshots/, with the run's summary on stdout (steps 9.2, 9.3)."""
    monkeypatch.delenv("BASIS_ENV")
    assert basis(repo, *RUN[:-2]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert (printed["status"], printed["interval"], printed["job"]) == ("checked", "2026-11-05..2026-11-06", "basis.job.example.job")
    assert printed["outputs"]
    assert printed["reasons"] == []
    assert len(list((repo / "snapshots").rglob(f"{printed['run_id']}.json"))) == 1


def test_a_run_on_files_replays_and_regresses_from_the_repos_own_store_with_no_environment(repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BASIS_ENV")
    assert basis(repo, *RUN[:-2]) == 0
    run_id = json.loads(capsys.readouterr().out)["run_id"]
    assert basis(repo, "replay", "basis.job.example.job", run_id) == 0
    assert (json.loads(capsys.readouterr().out)["replay_of"], len(list((repo / "snapshots").rglob("*.json")))) == (run_id, 2)
    assert basis(repo, "regress", "basis.job.example.job") == 0
    assert [entry["run_id"] for entry in json.loads(capsys.readouterr().out)] == [run_id]  # the run, and not its replay


def test_a_run_reads_the_user_s_file_for_the_environment_it_names(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BASIS_ENV")
    (tmp_path / "config" / "basis").mkdir(parents=True)
    (tmp_path / "config" / "basis" / "qa.env").write_text(f"BASIS_ENV=qa\nBASIS_SNAPSHOT_ROOT={tmp_path / 'qa-runs'}\n", encoding="utf-8")
    assert basis(repo, *RUN, "--env", "qa") == 0
    assert len(list((tmp_path / "qa-runs").rglob("*.json"))) == 1  # the run's manifest, in qa's store, not the process's
    assert not (tmp_path / "snapshots").exists()


def test_a_scheduled_run_resolves_a_relative_interval_in_the_job_s_calendar_and_records_the_dates(repo: Path, snapshot_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_now", lambda: datetime(2026, 11, 6, 15, tzinfo=UTC))  # Friday in New York, the day after the fixture's trades
    assert basis(repo, *RUN[:-1], "previous-business-day") == 0
    [manifest] = snapshot_root.rglob("*.json")
    assert json.loads(manifest.read_text(encoding="utf-8"))["interval"] == "2026-11-05..2026-11-06"  # the dates, as a replay reads them


def test_a_relative_interval_for_a_job_with_no_calendar_is_one_error_line(repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(job, "CALENDAR")
    assert basis(repo, *RUN[:-1], "today") == 1
    assert only_error(capsys.readouterr().err) == (
        'ERROR: basis.job.example.job declares no calendar, which --interval today is resolved in: declare one in the job\'s module, as CALENDAR = etl.calendar("sifma-us"), or give dates'
    )


@pytest.mark.parametrize("verb", [[*RUN, "--publish"], ["resume", "basis.job.example.job", "0193f2d4-0000-7000-8000-000000000001"]], ids=["run --publish", "resume"])
def test_a_run_that_would_write_to_prod_is_refused_unless_the_deployment_sets_the_flag(repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, verb: list[str]) -> None:
    monkeypatch.setenv("BASIS_ENV", "prod")
    assert basis(repo, *verb) == 1
    line = error_line(capsys)
    assert "would write to prod, which basis refuses unless the process sets BASIS_PROD_WRITES=1" in line
    monkeypatch.setenv("BASIS_PROD_WRITES", "1")
    basis(repo, *verb)
    assert "would write to prod" not in capsys.readouterr().err  # it gets as far as its database, which is not configured, or its run, which is not saved


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
    assert only_error(capsys.readouterr().err) == f"ERROR: run {run_id} is checked, and was not asked to publish: run it again with --publish"


def test_a_run_told_again_records_it_and_resume_takes_again_too(repo: Path, snapshot_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A resume publishes as its run was told to; `--again` tells a resume of a run that was not."""
    assert basis(repo, *RUN, "--again") == 0
    run_id = next(json.loads(line)["run_id"] for line in capsys.readouterr().err.splitlines() if '"run_id"' in line)
    assert snapshots.find(Root.at(snapshot_root), run_id).record().again
    assert basis(repo, "resume", run_id, "--again") == 1
    assert error_line(capsys) == f"ERROR: run {run_id} is checked, and was not asked to publish: run it again with --publish"


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
    found = cli.regress_jobs(cli.RegressArgs(("no_such_job", "basis.job.example.job"), 10, {}, None))
    assert [(entry.name, entry.run_id, entry.changed) for entry in found.replayed] == [("basis.job.example.job", run_id, False)]
    assert [command.one_line(error) for error in found.failures] == [
        "while regressing no_such_job: no job named 'no_such_job', and it is no module path: give one the repo lists (none yet), or a job's module path, as jobs.NAME.job"
    ]


def test_regress_prints_each_run_replayed_as_json(repo: Path, snapshot_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_id = ran(repo, capsys)
    source = snapshots.find(Root.at(snapshot_root), run_id)
    source.save_record(RunRecord.model_validate(source.record().model_dump() | {"status": "published"}))
    assert basis(repo, "regress", "basis.job.example.job") == 0
    assert [(entry["job"], entry["run_id"], entry["differences"]) for entry in json.loads(capsys.readouterr().out)] == [("basis.job.example.job", run_id, [])]


@pytest.mark.parametrize(
    ("found", "summary"),
    [
        (OperationDiff("positions", "upsert", added=2, removed=0, changed=1, columns={"price": 1}), "upsert: 2 added, 1 changed (price 1)"),
        (OperationDiff("positions", "insert", added=0, removed=3, changed=0), "insert: 3 removed"),
        (OperationDiff("positions", "delete", added=0, removed=0, changed=1), "its delete differs"),
    ],
    ids=["counts-and-columns", "removed-only", "the-delete"],
)
def test_a_difference_reads_in_a_few_words(found: OperationDiff, summary: str) -> None:
    assert cli.difference(found).summary == summary


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
    assert runs.failure_of(RunRecord.model_validate(RECORD | fields), publish=publish) == failure


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
            (
                f"run R1 of prices committed to no database: alpha: {OVERLAPPED['error']}",
                "where a run it names stopped before it finished publishing, `basis resume` finishes it; run the job again with --again only if its rows are meant to be inserted a second time",
            ),
        ),
    ],
    ids=["a-partial-run-told-again", "a-partial-run-a-ledger-refused", "a-run-a-ledger-refused"],
)
def test_a_fix_carries_again_where_the_next_attempt_needs_it(fields: dict[str, object], failure: tuple[str, str]) -> None:
    assert runs.failure_of(RunRecord.model_validate(RECORD | fields), publish=True) == failure


def test_a_repo_made_before_basis_has_a_release_fails_with_one_error_line_that_names_the_fix(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(new, "VERSION", UNRELEASED)  # basis-job as installed from this checkout, before any release
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
    assert (core, sorted(parts)) == (f"basis-core {version('basis-core')}", sorted(installed))
    assert f"basis-job {version('basis-core')}" in installed  # at basis-core's version, as every basis package is


def test_regress_with_a_label_no_published_run_has_fails_and_says_so(repo: Path, snapshot_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_id = ran(repo, capsys)
    source = snapshots.find(Root.at(snapshot_root), run_id)
    source.save_record(RunRecord.model_validate(source.record().model_dump() | {"status": "published"}))
    assert basis(repo, "regress", "basis.job.example.job", "--label", "purpose=prodution") == 1
    assert only_error(capsys.readouterr().err) == (
        "ERROR: no published run of basis.job.example.job has the labels purpose=prodution, so regress replayed nothing:"
        " check them against a run's labels, as its manifest records them, or regress without --label"
    )


def test_regress_with_no_published_run_to_replay_says_it_skipped(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo, "regress", "basis.job.example.job") == 0
    assert "SKIPPED: regress: no published run of basis.job.example.job to replay\n" in capsys.readouterr().err


def test_a_replay_is_saved_in_the_repos_own_store_never_the_environments(repo: Path, snapshot_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_id = ran(repo, capsys)
    assert basis(repo, "replay", run_id) == 0
    replayed = json.loads(capsys.readouterr().out)["run_id"]
    assert snapshots.find(Root.at(repo / "snapshots"), replayed).record().replay_of == run_id
    with pytest.raises(LookupError):
        snapshots.find(Root.at(snapshot_root), replayed)


def test_a_runs_stderr_holds_none_of_the_secrets_its_settings_hold(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    connection = "Server=127.0.0.1,9;UID=etl;PWD=Pa55;tail-of-it;Database=warehouse"  # a password with ; in it, unbraced
    settings = {
        "BASIS_ENV": "dev",
        "BASIS_SNAPSHOT_ROOT": str(tmp_path / "runs"),
        **{
            f"BASIS_DATABASES__{name}__{key}": value
            for name in ("SOURCE", "WAREHOUSE")
            for key, value in {"KIND": "mssql", "CONNECTION_STRING": connection, "ISOLATION": "read_committed", "TIMEOUT_S": "5", "MAX_ROWS": "10"}.items()
        },
        "BASIS_DATABASES__WAREHOUSE__WRITE__CONNECTION_STRING": connection,
        "BASIS_DATABASES__WAREHOUSE__WRITE__LEDGER": "etl.publishes",
        "BASIS_HTTP__PRICING__URL": "https://user:pa55word@pricing.firm/v1",
        "BASIS_HTTP__PRICING__PROXY": "http://proxyuser:proxypa55@proxy.firm:8080",
    }
    (tmp_path / "config" / "basis").mkdir(parents=True)
    (tmp_path / "config" / "basis" / "dev.env").write_text("".join(f"{name}={value}\n" for name, value in settings.items()), encoding="utf-8")
    assert basis(repo, *RUN, "--env", "dev", "--publish") == 1
    err = capsys.readouterr().err
    assert err.splitlines()[-1].startswith("ERROR: ")
    for secret in ("Pa55", "tail-of-it", "pa55word", "proxypa55"):
        assert secret not in err, secret


def test_a_replay_with_rows_lists_each_changed_row_by_key_before_and_after(repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = ran(repo, capsys)
    today = job.transform

    def doubled(inputs: job.Inputs, params: job.Params) -> job.Outputs:
        outputs = today(inputs, params)
        return job.Outputs(positions=outputs.positions.assign(gross_notional=outputs.positions["gross_notional"] * 2), activity=outputs.activity)

    monkeypatch.setattr(job, "transform", doubled)
    assert basis(repo, "replay", "basis.job.example.job", run_id, "--rows", "1") == 0
    (positions,) = [entry for entry in json.loads(capsys.readouterr().out)["differences"] if entry["output"] == "positions"]
    assert positions["changed"] > 1
    (row,) = positions["rows"]
    assert set(row["key"]) == {"trade_date", "account_id", "security_id"}
    assert set(row["changes"]) == {"gross_notional"}
    assert row["changes"]["gross_notional"]["after"] == 2 * row["changes"]["gross_notional"]["before"]
    assert basis(repo, "replay", "basis.job.example.job", run_id) == 0
    assert all("rows" not in entry for entry in json.loads(capsys.readouterr().out)["differences"])


def test_a_replay_asked_for_more_rows_than_it_keeps_is_refused(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_id = ran(repo, capsys)
    assert basis(repo, "replay", run_id, "--rows", "21") == 2
    assert "a replay keeps 20 changed rows of each table, so give --rows 20 or fewer, not 21" in only_error(capsys.readouterr().err)


def test_runs_lists_the_stores_recent_runs_newest_first_with_their_trace(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    first, second = ran(repo, capsys), ran(repo, capsys)
    assert basis(repo, "runs", "basis.job.example.job") == 0
    listed = json.loads(capsys.readouterr().out)
    assert [entry["run_id"] for entry in listed] == [second, first]
    assert {(entry["status"], entry["interval"], entry["error"], entry["reasons"]) for entry in listed} == {("checked", "2026-11-05..2026-11-06", None, 0)}
    assert all(len(entry["trace_id"]) == 32 for entry in listed)
    assert basis(repo, "runs", "basis.job.example.job", "--last", "1") == 0
    assert [entry["run_id"] for entry in json.loads(capsys.readouterr().out)] == [second]


@pytest.mark.parametrize("env", [None, "dev"], ids=["on-its-fixture", "in-an-environment"])
def test_a_run_whose_query_file_is_missing_fails_before_it_reads_its_settings(repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, env: str | None) -> None:
    monkeypatch.setattr(job.Inputs.trades, "sql", "sql/trade.sql")
    monkeypatch.delenv("BASIS_ENV")
    if env is None:
        assert basis(repo, *RUN[:-2]) == 1
    else:
        assert basis(repo, *RUN, "--env", env, "--publish") == 1  # dev has no settings file here, which it never reads
    line = only_error(capsys.readouterr().err)
    assert "trades reads " in line
    assert line.endswith("trade.sql, which does not exist: name the file in sql=, relative to " + str(EXAMPLE))
