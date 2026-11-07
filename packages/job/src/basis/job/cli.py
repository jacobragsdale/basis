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
from pathlib import Path
from typing import Literal

from basis.core import command, logs, snapshots
from basis.core.destination import Dialect
from basis.core.errors import BasisError, deliberate
from basis.core.interval import Interval
from basis.core.settings import Settings
from basis.core.snapshots import RunRecord, Snapshot
from basis.core.storage import Root
from basis.core.tables import Column
from basis.core.verbs import Part, Regress, Runs, Verb, fail
from basis.job import ddl, docs, new, runs, sync
from basis.job import job as jobs

log = logging.getLogger(__name__)

FIRST_JOB = "positions"
"""A new repo's first job, when `basis-job new repo` is given none: the example, which computes daily positions."""


@dataclass(frozen=True)
class RunArgs:
    job: str
    params: Path
    interval: Interval | None
    data: Path | None
    labels: dict[str, str]
    publish: bool
    again: bool


@dataclass(frozen=True)
class ReplayArgs:
    job: str | None
    run_id: str
    params: Path | None


@dataclass(frozen=True)
class ResumeArgs:
    job: str | None
    run_id: str


@dataclass(frozen=True)
class RegressArgs:
    jobs: tuple[str, ...]
    last: int
    labels: dict[str, str]
    snapshots: str | None


@dataclass(frozen=True)
class DescribeArgs:
    job: str | None


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
    """basis-job's verbs, and what core's verbs do with its jobs: `basis new job`, `basis replay`, `basis regress`,
    its docs and its agent files."""
    return Part(
        verbs=(
            Verb("run", "run a job, and with --publish write its changes to its databases", _run_arguments, _run_from),
            Verb("resume", "finish publishing a run that failed partway, from its snapshot", _resume_arguments, lambda n: _resume(ResumeArgs(n.job, n.run_id))),
            Verb("describe", "print a job's params, inputs and outputs as JSON, or every job's name", _describe_arguments, lambda n: _describe(DescribeArgs(n.job))),
            Verb("ddl", "print CREATE TABLE statements for a job's tables, or for the ledger", _ddl_arguments, lambda n: _ddl(DdlArgs(n.job, n.ledger, n.dialect))),
        ),
        files=sync.agent_files,
        docs=docs.FOLDER,
        new={"job": new.job},
        runs=Runs(names=lambda: sorted(jobs.listed()), replay=lambda args: _replay(ReplayArgs(args.name, args.run_id, args.params)), regress=_regress_jobs),
    )


def scaffold(argv: Sequence[str] | None = None) -> int:
    """The `basis-job` console script, which `uvx basis-job new repo NAME` runs."""
    return command.standalone("basis-job", "Make a repo of basis jobs.", [Verb("new", "make a job repo: basis-job new repo NAME [JOB]", _new_arguments, _new_from)], argv)


def _run_from(n: argparse.Namespace) -> int:
    return _run(RunArgs(n.job, n.params, n.interval, n.data, dict(n.label), publish=n.publish, again=n.again))


def _run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("job", metavar="JOB", help="the job's name, as the repo lists it, or its module path")
    parser.add_argument("--params", type=command.existing_file, required=True, help="the params JSON file")
    parser.add_argument("--interval", type=_interval, help="START..END, dates or ISO datetimes with an offset")
    parser.add_argument("--data", type=command.existing_folder, help="a folder of files that replace the datasets they name")
    parser.add_argument("--label", type=command.label, action="append", default=[], help="KEY=VALUE, recorded in the run's manifest; repeatable")
    parser.add_argument("--publish", action="store_true", help="write the changes to their databases, if every check passes")
    parser.add_argument("--again", action="store_true", help="publish a plain insert for an interval the ledger has already")


def _interval(text: str) -> Interval:
    try:
        return Interval.parse(text)
    except ValueError as error:  # argparse prints an ArgumentTypeError's message, and only a generic one for a ValueError
        raise argparse.ArgumentTypeError(str(error)) from error


def _resume_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("job", metavar="JOB", nargs="?", help="the job whose run it is; without it, the run is found by its id")
    parser.add_argument("run_id", metavar="RUN_ID")


def _describe_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("job", metavar="JOB", nargs="?", help="the job to describe; without it, every job the repo lists, by name")


def _ddl_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("job", metavar="JOB", nargs="?")
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


def _run(args: RunArgs) -> int:
    job = jobs.load(args.job)
    logs.include(job.module.partition(".")[0])
    settings = Settings()
    request = runs.Request(args.params, args.interval, args.data, args.labels, publish=args.publish, again=args.again, git_sha=os.environ.get("BASIS_GIT_SHA"))
    result = runs.run(job, settings, request, snapshot_root=settings.snapshots())
    return fail(*found) if (found := failure_of(result.record, publish=args.publish)) is not None else 0


def failure_of(record: RunRecord, *, publish: bool) -> tuple[str, str] | None:
    """Why a run did not do what it was asked, and the fix, or None when it did: `checked` without `--publish`, or
    `published` with it."""
    if record.status == ("published" if publish else "checked"):
        return None
    run = f"run {record.run_id} of {record.job}"
    if record.status == "partial":
        return f"{run} published to some of its databases and failed on another", f"fix what its publishes record, then `basis resume {record.job} {record.run_id}`"
    if record.error is not None:
        return f"{run} failed: {record.error}", "fix it, and run the job again"
    if record.reasons:
        more = f", and {len(record.reasons) - 1} more the log lists" if len(record.reasons) > 1 else ""
        replay = f"; `basis replay {record.job} {record.run_id}` reruns it on what it read" if record.inputs else ""
        return f"{run} wrote nothing, because {record.reasons[0]}{more}", f"fix {'each' if more else 'it'}, and run the job again{replay}"
    if errors := [f"{name}: {entry.error}" for name, entry in record.publishes.items() if entry.error is not None]:
        return f"{run} committed to no database: {'; '.join(errors)}", "fix it, and run the job again"
    return f"{run} ended {record.status} with no reason recorded, when it was asked to publish", "its log records say what happened; run it again, and report it if it does the same"


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
    root = Settings().snapshots()
    source, job = _saved(root, args.run_id, args.job)
    with deliberate(ValueError):  # a run of another job, or one that did not save what the job reads now
        result, diffs = runs.replay(job, source, params=args.params, snapshot_root=root)
    _print({"run_id": result.record.run_id, "replay_of": args.run_id, "status": result.record.status, "reasons": result.record.reasons, "differences": [vars(entry) for entry in diffs]})
    if result.ok:
        return 0
    why = "; ".join(result.record.reasons) or result.record.error or result.record.status
    return fail(f"the replay of run {args.run_id} of {job.name} would write nothing: {why}", "fix what it says, and replay it again")


def _resume(args: ResumeArgs) -> int:
    settings = Settings()
    source, job = _saved(settings.snapshots(), args.run_id, args.job)
    with deliberate(ValueError):  # a run that never published, or one whose tables the job writes elsewhere now
        record = runs.resume(job, source, settings)
    if record.status == "published":
        return 0
    return fail(f"run {args.run_id} of {job.name} is still {record.status}", f"fix what its publishes record, then `basis resume {job.name} {args.run_id}` again")


def _regress_jobs(args: Regress) -> int:
    return regress_jobs(RegressArgs((args.name,) if args.name is not None else tuple(sorted(jobs.listed())), args.last, args.labels, args.snapshots))


def regress_jobs(args: RegressArgs) -> int:
    """`basis regress` for the jobs `args` names. Each job replays on its own: one that fails to load or to replay
    prints its failure, and the rest still replay and print."""
    root = Root.at(args.snapshots) if args.snapshots is not None else Settings().snapshots()
    found: list[dict[str, object]] = []
    failed = 0
    for name in args.jobs:
        try:
            job = jobs.load(name)
            entries = runs.regress(job, root, last=args.last, labels=args.labels)
        except Exception as error:  # noqa: BLE001 -- each job's failure is its own line, and the other jobs still replay
            error.add_note(f"while regressing {name}")
            failed += command.report(error)
            continue
        found += [{"job": job.name, "run_id": entry.run_id, "reasons": entry.reasons, "differences": [vars(diff) for diff in entry.diffs]} for entry in entries]
    _print(found)
    if changed := [entry for entry in found if entry["differences"] or entry["reasons"]]:
        failed += fail(f"{len(changed)} of the {len(found)} runs replayed differ, or would not write", "stdout lists how; fix the code, or say in the pull request why the change is meant")
    return 1 if failed else 0


def _describe(args: DescribeArgs) -> int:
    if args.job is None:
        _print(jobs.listed())
        return 0
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
