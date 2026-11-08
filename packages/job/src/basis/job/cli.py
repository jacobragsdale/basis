"""basis-job's part of the `basis` command, which its `basis.verbs` entry point names, and the `basis-job` command,
which makes a job repo. Each handler reads its arguments as a frozen dataclass. `basis run` is the only verb that
writes to a database, and only with `--publish`. Exit codes: 0 when a verb did what it says, 1 when a run failed,
refused or published partly, or a regression found changes, and 2 for a command line that does not parse. Logs are
JSON lines on stderr, with every failure's one `ERROR` line after them; results a caller reads, such as `describe`
and `regress`, are JSON on stdout."""

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from basis.core import command, environments, interval, logs, snapshots
from basis.core.destination import Dialect
from basis.core.errors import BasisError, deliberate
from basis.core.interval import Interval, Relative
from basis.core.settings import Settings
from basis.core.snapshots import Snapshot
from basis.core.storage import Root
from basis.core.sync import Problem
from basis.core.tables import Column
from basis.core.verbs import Difference, Part, Regress, Regressed, Regressions, Runs, Shared, Verb, fail
from basis.job import ddl, docs, new, qa, runs, sync
from basis.job import job as jobs
from basis.job.diff import OperationDiff

log = logging.getLogger(__name__)

FIRST_JOB = "positions"
"""A new repo's first job, when `basis-job new repo` is given none: the example, which computes daily positions."""


@dataclass(frozen=True)
class RunArgs:
    job: str
    params: Path
    interval: Interval | Relative | None
    """The interval `--interval` gives: dates or instants, or relative to the run's start, resolved in the job's calendar."""
    data: Path | None
    labels: dict[str, str]
    publish: bool
    again: bool
    env: str | None = None
    """The environment `--env` names, or None for `BASIS_ENV`'s."""


@dataclass(frozen=True)
class ReplayArgs:
    job: str | None
    run_id: str
    params: Path | None
    env: str | None = None


@dataclass(frozen=True)
class ResumeArgs:
    job: str | None
    run_id: str
    again: bool
    """Whether to publish a plain insert for an interval the ledger has already, though the run was not told to."""
    env: str | None = None


@dataclass(frozen=True)
class RegressArgs:
    jobs: tuple[str, ...]
    last: int
    labels: dict[str, str]
    snapshots: str | None
    """The run store to read, or None for the one the environment's settings name."""
    env: str | None = None


@dataclass(frozen=True)
class DescribeArgs:
    job: str


@dataclass(frozen=True)
class DdlArgs:
    job: str | None
    ledger: str | None
    dialect: Dialect


@dataclass(frozen=True)
class NewArgs:
    what: Literal["repo"]
    name: str
    job: str
    directory: Path
    index: str | None
    framework_path: Path | None


def part() -> Part:
    """basis-job's side of the verbs core shares among the parts that run things, which every job verb is, and what
    core's verbs do with its jobs: `basis new job`, the regressions and QA runs of `basis check --full`, its docs and
    its agent files."""
    return Part(
        files=sync.agent_files,
        docs=docs.FOLDER,
        new={"job": new.job},
        runs=Runs(
            "job",
            jobs.listed,
            run=Shared("run a job, and with --publish write its changes to its databases", _run_from, _run_arguments),
            describe=Shared("print a job's params, inputs and outputs as JSON, or every job's name", lambda n: _describe(DescribeArgs(n.name))),
            resume=Shared("finish publishing a run that failed partway, from its snapshot", lambda n: _resume(ResumeArgs(n.name, n.run_id, n.again, n.env)), _resume_arguments),
            replay=Shared("rerun a saved run's transform with today's code, and print how its outputs differ", lambda n: _replay(ReplayArgs(n.name, n.run_id, n.params, n.env)), _replay_arguments),
            regress=Shared("replay recent published runs with today's code, and list what changed", _regress_from, _regress_arguments),
            ddl=Shared("print CREATE TABLE statements for a job's tables, or for the ledger", lambda n: _ddl(DdlArgs(n.name, n.ledger, n.dialect)), _ddl_arguments),
            regressions=_regress_jobs,
        ),
        qa=qa.plan,
        smoke=_smoke,
    )


def _smoke(repo: Path, env: str, commit: str | None) -> list[Problem]:  # noqa: ARG001 -- a job's dry run is of the checkout's code, whatever commit an environment serves
    from basis.job import smoke  # noqa: PLC0415 -- the databases load when the verb runs

    return smoke.smoke(repo, env)


def scaffold(argv: Sequence[str] | None = None) -> int:
    """The `basis-job` console script, which `uvx basis-job new repo NAME` runs."""
    return command.standalone("basis-job", "Make a repo of basis jobs.", [Verb("new", "make a job repo: basis-job new repo NAME [JOB]", _new_arguments, _new_from)], argv)


def _run_from(n: argparse.Namespace) -> int:
    return _run(RunArgs(n.name, n.params, n.interval, n.data, dict(n.label), publish=n.publish, again=n.again, env=n.env))


def _run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--params", type=command.existing_file, required=True, help="the params JSON file")
    parser.add_argument(
        "--interval", type=_interval, help="START..END of dates or ISO datetimes with an offset; or, in the job's CALENDAR, previous-business-day, today, yesterday, previous-month, or today-7d..today"
    )
    parser.add_argument("--data", type=command.existing_folder, help="a folder of files that replace the datasets they name")
    parser.add_argument("--label", type=command.label, action="append", default=[], help="KEY=VALUE, recorded in the run's manifest; repeatable")
    parser.add_argument("--publish", action="store_true", help="write the changes to their databases, if every check passes")
    parser.add_argument("--again", action="store_true", help="publish a plain insert for an interval the ledger has already")


def _interval(text: str) -> Interval | Relative:
    try:
        return interval.given(text)
    except ValueError as error:  # argparse prints an ArgumentTypeError's message, and only a generic one for a ValueError
        raise argparse.ArgumentTypeError(str(error)) from error


def _resume_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--again", action="store_true", help="publish a plain insert for an interval the ledger has already, as a run told --again does")


def _replay_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--params", type=command.existing_file, help="other params to replay with, instead of the run's own")


def _regress_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--last", type=command.positive, default=10, help="how many published runs of each, newest first")
    parser.add_argument("--label", type=command.label, action="append", default=[], help="KEY=VALUE a run's labels must include; repeatable")
    parser.add_argument("--snapshots", help="the snapshot root to read, instead of the environment's BASIS_SNAPSHOT_ROOT")


def _ddl_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ledger", metavar="TABLE", help="print the ledger's CREATE TABLE instead, for TABLE")
    parser.add_argument("--dialect", choices=["mssql", "oracle"], required=True)


def _new_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("what", metavar="repo", choices=["repo"], help="what to make: a repo")
    parser.add_argument("name", metavar="NAME", help="the repo's folder")
    parser.add_argument("job", metavar="JOB", nargs="?", default=FIRST_JOB, help=f"its first job's name, a copy of the example (default: {FIRST_JOB})")
    parser.add_argument("--dir", type=Path, default=Path(), help="where to make the repo")
    parser.add_argument("--index", help="the package index basis is published to, written into the repo")
    parser.add_argument("--framework-path", type=Path, help="a checkout of basis, whose packages the repo installs editable")


def _new_from(namespace: argparse.Namespace) -> int:
    return _new(NewArgs(namespace.what, namespace.name, namespace.job, namespace.dir, namespace.index, namespace.framework_path))


def _now() -> datetime:
    """The moment a run starts, which a relative interval is resolved at."""
    return datetime.now(UTC)


def _run(args: RunArgs) -> int:
    job = jobs.load(args.job)
    logs.include(job.module.partition(".")[0])
    environment = environments.load(args.env, Settings)
    if args.publish:
        environment.refuse_writes(f"basis run {job.name} --publish")
    settings = environment.settings
    given = args.interval.resolve(_now(), job.calendar, job=job.name) if isinstance(args.interval, Relative) else args.interval
    request = runs.Request(args.params, given, args.data, args.labels, publish=args.publish, again=args.again, git_sha=os.environ.get("BASIS_GIT_SHA"))
    result = runs.run(job, settings, request, snapshot_root=settings.snapshots(), environment=environment.name)
    return fail(*found) if (found := runs.failure_of(result.record, publish=args.publish)) is not None else 0


def _saved(root: Root, run_id: str, name: str | None) -> tuple[Snapshot, jobs.Job]:
    """The run `run_id` in the store at `root`, and the job it is a run of: `name`, or the job its manifest names."""
    if name is not None:
        job = jobs.load(name)
        with deliberate(LookupError):  # the store finds no such run of the job
            return snapshots.find(root, run_id, job.name), job
    with deliberate(LookupError):  # the store finds no such run
        source = snapshots.find(root, run_id)
    if (part := source.path.parent.parent.name) != runs.PACKAGE:  # the manifest is at runs/PACKAGE/NAME/RUN_ID.json
        msg = f"run {run_id} is a run of the {part} part, not a job's: replay it in a repo with that part"
        raise BasisError(msg)
    return source, jobs.load(source.record().job)


def _replay(args: ReplayArgs) -> int:
    environment = environments.load(args.env, Settings)
    settings = environment.settings
    root = settings.snapshots()
    source, job = _saved(root, args.run_id, args.job)
    with deliberate(ValueError):  # a run of another job, or one that did not save what the job reads now
        result, diffs = runs.replay(job, source, params=args.params, snapshot_root=root, tracing=settings.traces, environment=environment.name)
    _print({"run_id": result.record.run_id, "replay_of": args.run_id, "status": result.record.status, "reasons": result.record.reasons, "differences": [vars(entry) for entry in diffs]})
    if result.ok:
        return 0
    why = "; ".join(result.record.reasons) or result.record.error or result.record.status
    return fail(f"the replay of run {args.run_id} of {job.name} would write nothing: {why}", "fix what it says, and replay it again")


def _resume(args: ResumeArgs) -> int:
    environment = environments.load(args.env, Settings)
    environment.refuse_writes(f"basis resume {args.run_id}")
    settings = environment.settings
    source, job = _saved(settings.snapshots(), args.run_id, args.job)
    with deliberate(ValueError):  # a run that never published, or one whose tables the job writes elsewhere now
        record = runs.resume(job, source, settings, again=args.again, environment=environment.name)
    if record.status == "published":
        return 0
    return fail(f"run {args.run_id} of {job.name} is still {record.status}", f"fix what its publishes record, then `{runs.resume_command(record)}` again")


def _regress_from(n: argparse.Namespace) -> int:
    """`basis regress`: each run replayed, as JSON on stdout, and a line for each failure to replay a job; 1 when one
    failed, or when a run differs or would not write."""
    found = regress_jobs(RegressArgs((n.name,) if n.name is not None else tuple(sorted(jobs.listed())), n.last, dict(n.label), n.snapshots, n.env))
    failed = sum(command.report(error) for error in found.failures)
    _print([{"job": entry.name, "run_id": entry.run_id, "reasons": list(entry.reasons), "differences": [dict(diff.detail) for diff in entry.differences]} for entry in found.replayed])
    if changed := [entry for entry in found.replayed if entry.changed]:
        failed += fail(f"{len(changed)} of the {len(found.replayed)} runs replayed differ, or would not write", "stdout lists how; fix the code, or say in the pull request why the change is meant")
    return 1 if failed else 0


def _regress_jobs(args: Regress) -> Regressions:
    return regress_jobs(RegressArgs((args.name,) if args.name is not None else tuple(sorted(jobs.listed())), args.last, args.labels, args.snapshots))


def regress_jobs(args: RegressArgs) -> Regressions:
    """`basis regress` for the jobs `args` names, which `basis` prints. Each job replays on its own: one that fails to
    load or to replay is a failure of its own, and the rest still replay."""
    root = Root.at(args.snapshots) if args.snapshots is not None else environments.load(args.env, Settings).settings.snapshots()
    replayed: list[Regressed] = []
    failures: list[Exception] = []
    for name in args.jobs:
        try:
            job = jobs.load(name)
            entries = runs.regress(job, root, last=args.last, labels=args.labels)
        except Exception as error:  # noqa: BLE001 -- each job's failure is its own line, and the other jobs still replay
            error.add_note(f"while regressing {name}")
            failures.append(error)
            continue
        replayed += [Regressed(job.name, entry.run_id, tuple(entry.reasons), tuple(difference(diff) for diff in entry.diffs)) for entry in entries]
    return Regressions(tuple(replayed), tuple(failures))


def difference(found: OperationDiff) -> Difference:
    """How a table's changes differ, in a few words, in rows and then columns: `upsert: 2 added, 1 changed (price 1)`,
    or `insert: 1 changed (price 1); note added`."""
    if found.operation == "delete":
        return Difference(found.output, "its delete differs", vars(found))
    counts = ", ".join(f"{count} {word}" for count, word in ((found.added, "added"), (found.removed, "removed"), (found.changed, "changed")) if count)
    columns = f" ({', '.join(f'{name} {count}' for name, count in found.columns.items())})" if found.columns else ""
    schema = ", ".join(f"{name} {change}" for name, change in found.schema.items())
    return Difference(found.output, f"{found.operation}: {'; '.join(part for part in (counts + columns, schema) if part)}", vars(found))


def _describe(args: DescribeArgs) -> int:
    job = jobs.load(args.job)
    _print(describe(job))
    return 0


def describe(job: jobs.Job) -> dict[str, object]:
    """A job's contract as data: what an agent needs to drive it without reading its code."""
    return {
        "job": job.name,
        "module": job.module,
        "params": job.params.model_json_schema(),
        "inputs": {name: {"source": dataset.describe(), "empty": dataset.empty, "columns": _columns(dataset.row.columns)} for name, dataset in job.inputs.declared().items()},
        "given": {name: resource.kind for name, resource in job.inputs.declared_resources().items()},
        "outputs": {
            name: {
                "database": table.target.database,
                "table": table.target.table,
                "write": type(table.target.write).__name__,
                "key": list(table.target.key) if table.target.key is not None else None,
                "columns": _columns(table.target.row.columns),
            }
            for name, table in job.outputs.declared().items()
        },
        "messages": {name: {"destination": message.destination, "to": message.to, "ledger": message.ledger} for name, message in job.outputs.declared_messages().items()},
        "reports": {name: {"folder": report.folder, "path": report.path, "sheet": report.sheet, "columns": _columns(report.row.columns)} for name, report in job.outputs.declared_reports().items()},
        "upserts": {
            name: {"mongo": upsert.mongo, "collection": upsert.collection, "key": list(upsert.key), "columns": _columns(upsert.row.columns)} for name, upsert in job.outputs.declared_upserts().items()
        },
        "lakes": {
            name: {"lake": lake.lake, "table": lake.table, "partition": lake.partition, "ledger": lake.ledger, "columns": _columns(lake.row.columns)}
            for name, lake in job.outputs.declared_lakes().items()
        },
        "check": job.check is not None,
    }


def _columns(columns: Sequence[Column]) -> list[dict[str, object]]:
    return [{key: value for key, value in vars(column).items() if key != "dtype" and value is not None} for column in columns]


def _ddl(args: DdlArgs) -> int:
    if args.ledger is not None:
        sys.stdout.write(ddl.ledger(args.ledger, args.dialect))
        return 0
    if args.job is None:
        return fail("basis ddl prints a job's tables or the ledger, and was given neither", "give a job, as `basis ddl JOB --dialect mssql`, or --ledger TABLE")
    job = jobs.load(args.job)
    sys.stdout.write("\n".join(ddl.create_table(table.target, args.dialect) for table in job.outputs.declared().values()))
    return 0


def _new(args: NewArgs) -> int:
    path = new.repo(args.directory / args.name, args.job, index=args.index, framework_path=args.framework_path)
    log.info("made %s: run `uv run basis check` in it", path)
    return 0


def _print(value: object) -> None:
    sys.stdout.write(json.dumps(value, indent=2, default=str) + "\n")
