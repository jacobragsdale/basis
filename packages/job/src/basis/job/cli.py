"""The `etl` command line: one handler per command, each reading its arguments as a frozen dataclass. `etl run` is the
only thing that writes to a database, and only with `--publish`. Exit codes: 0 when the command did what it says, 1
when a run failed, refused or published partly, or a regression found changes, and 2 for a command line that does
not parse. Logs are JSON lines on stderr; results a caller reads, such as `describe` and `regress`, are JSON on
stdout."""

import argparse
import json
import logging
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from basis.core import logs, snapshots
from basis.core.destination import Dialect
from basis.core.interval import Interval
from basis.core.new import NoReleaseError
from basis.core.settings import Settings
from basis.core.storage import Root
from basis.core.tables import Column
from basis.job import ddl, docs, new, runs, sync
from basis.job import job as jobs
from basis.job.version import VERSION

log = logging.getLogger(__name__)


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
    job: str
    run_id: str
    params: Path | None


@dataclass(frozen=True)
class ResumeArgs:
    job: str
    run_id: str


@dataclass(frozen=True)
class RegressArgs:
    job: str
    last: int
    labels: dict[str, str]
    snapshots: str | None


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
    what: Literal["repo", "job"]
    name: str
    job: str | None
    directory: Path
    index: str | None
    framework_path: Path | None


@dataclass(frozen=True)
class DocsArgs:
    page: str | None


def _labels(values: Sequence[str]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for value in values:
        key, separator, text = value.partition("=")
        if not separator or not key:
            msg = f"a label is KEY=VALUE, not {value!r}"
            raise argparse.ArgumentTypeError(msg)
        labels[key] = text
    return labels


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="etl", description="Run, replay and publish ETL jobs.")
    parser.add_argument("--version", action="version", version=f"basis-job {VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run a job, and publish it with --publish")
    run.add_argument("job", help="the job's entry point name, or its module path")
    run.add_argument("--params", type=Path, required=True, help="the params JSON file")
    run.add_argument("--interval", type=Interval.parse, help="START..END, dates or ISO datetimes with an offset")
    run.add_argument("--data", type=Path, help="a folder of files that replace the datasets they name")
    run.add_argument("--label", action="append", default=[], help="KEY=VALUE, recorded in run.json; repeatable")
    run.add_argument("--publish", action="store_true", help="write the changes to their databases, if every check passes")
    run.add_argument("--again", action="store_true", help="publish a plain insert for an interval the ledger has already")

    replay = commands.add_parser("replay", help="run a saved run's transform again, with the code of the day")
    replay.add_argument("job")
    replay.add_argument("run_id")
    replay.add_argument("--params", type=Path, help="other params to replay with")

    resume = commands.add_parser("resume", help="finish publishing a run that failed partway")
    resume.add_argument("job")
    resume.add_argument("run_id")

    regress = commands.add_parser("regress", help="replay recent published runs and list what changed")
    regress.add_argument("job")
    regress.add_argument("--last", type=int, default=10, help="how many published runs, newest first")
    regress.add_argument("--label", action="append", default=[], help="KEY=VALUE a run's labels must include; repeatable")
    regress.add_argument("--snapshots", help="the snapshot root to read, instead of ETL_SNAPSHOT_ROOT")

    describe = commands.add_parser("describe", help="print a job's params, inputs and outputs as JSON")
    describe.add_argument("job")

    commands.add_parser("list", help="list the jobs the installed packages list")

    create = commands.add_parser("ddl", help="print CREATE TABLE statements for a job's tables, or for the ledger")
    create.add_argument("job", nargs="?")
    create.add_argument("--ledger", metavar="TABLE", help="print the ledger's CREATE TABLE instead, for TABLE")
    create.add_argument("--dialect", choices=["mssql", "oracle"], required=True)

    make = commands.add_parser("new", help="make a job repo, or a job in this one")
    make.add_argument("what", choices=["repo", "job"])
    make.add_argument("name", help="the repo's folder, or the job's name")
    make.add_argument("job", nargs="?", help="for a repo: its first job's name")
    make.add_argument("--dir", type=Path, default=Path(), help="where to make the repo")
    make.add_argument("--index", help="the package index basis is published to, written into the repo")
    make.add_argument("--framework-path", type=Path, help="a checkout of basis, whose packages the repo installs editable")

    commands.add_parser("sync", help="rewrite this repo's agent files from the installed basis-job")

    read = commands.add_parser("docs", help="list the docs, or print one")
    read.add_argument("page", nargs="?")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    namespace = _parser().parse_args(argv)
    command = str(namespace.command)
    logs.setup()
    handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "run": lambda n: _run(RunArgs(n.job, n.params, n.interval, n.data, _labels(n.label), publish=n.publish, again=n.again)),
        "replay": lambda n: _replay(ReplayArgs(n.job, n.run_id, n.params)),
        "resume": lambda n: _resume(ResumeArgs(n.job, n.run_id)),
        "regress": lambda n: _regress(RegressArgs(n.job, n.last, _labels(n.label), n.snapshots)),
        "describe": lambda n: _describe(DescribeArgs(n.job)),
        "list": lambda _: _list(),
        "ddl": lambda n: _ddl(DdlArgs(n.job, n.ledger, n.dialect)),
        "new": lambda n: _new(NewArgs(n.what, n.name, n.job, n.dir, n.index, n.framework_path)),
        "sync": lambda _: _sync(),
        "docs": lambda n: _docs(DocsArgs(n.page)),
    }
    try:
        return handlers[command](namespace)
    except NoReleaseError as error:  # the caller's to fix, not a defect: one line with the fix, and no traceback
        sys.stderr.write(f"ERROR: {error}\n")
        return 1
    except Exception:
        log.exception("etl %s failed", command)
        return 1


def _run(args: RunArgs) -> int:
    job = jobs.load(args.job)
    logs.include(job.module.partition(".")[0])
    settings = Settings()
    request = runs.Request(args.params, args.interval, args.data, args.labels, publish=args.publish, again=args.again, git_sha=os.environ.get("ETL_GIT_SHA"))
    result = runs.run(job, settings, request, snapshot_root=settings.snapshots())
    return 0 if result.ok and (result.record.status == "published" or not args.publish) else 1


def _replay(args: ReplayArgs) -> int:
    job = jobs.load(args.job)
    root = Settings().snapshots()
    result, diffs = runs.replay(job, snapshots.find(root, args.run_id, job.name), params=args.params, snapshot_root=root)
    _print({"run_id": result.record.run_id, "replay_of": args.run_id, "status": result.record.status, "reasons": result.record.reasons, "differences": [vars(entry) for entry in diffs]})
    return 0 if result.ok else 1


def _resume(args: ResumeArgs) -> int:
    job = jobs.load(args.job)
    settings = Settings()
    record = runs.resume(job, snapshots.find(settings.snapshots(), args.run_id, job.name), settings)
    return 0 if record.status == "published" else 1


def _regress(args: RegressArgs) -> int:
    job = jobs.load(args.job)
    root = Root.at(args.snapshots) if args.snapshots is not None else Settings().snapshots()
    found = runs.regress(job, root, last=args.last, labels=args.labels)
    _print([{"run_id": entry.run_id, "reasons": entry.reasons, "differences": [vars(diff) for diff in entry.diffs]} for entry in found])
    return 1 if any(entry.diffs or entry.reasons for entry in found) else 0


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


def _list() -> int:
    _print(jobs.listed())
    return 0


def _ddl(args: DdlArgs) -> int:
    if args.ledger is not None:
        sys.stdout.write(ddl.ledger(args.ledger, args.dialect))
        return 0
    if args.job is None:
        msg = "give a job, or --ledger TABLE"
        raise ValueError(msg)
    job = jobs.load(args.job)
    sys.stdout.write("\n".join(ddl.create_table(table.target, args.dialect) for table in job.outputs.declared().values()))
    return 0


def _new(args: NewArgs) -> int:
    if args.what == "repo":
        if args.job is None:
            msg = "etl new repo REPO JOB: name the repo's first job"
            raise ValueError(msg)
        path = new.repo(args.directory / args.name, args.job, index=args.index, framework_path=args.framework_path)
    else:
        path = new.job(Path.cwd(), args.name)
    log.info("made %s", path)
    return 0


def _sync() -> int:
    for path in sync.write(Path.cwd()):
        log.info("wrote %s", path)
    return 0


def _docs(args: DocsArgs) -> int:
    sys.stdout.write(docs.read(args.page) if args.page is not None else "\n".join(docs.pages()) + "\n")
    return 0


def _print(value: object) -> None:
    sys.stdout.write(json.dumps(value, indent=2, default=str) + "\n")
