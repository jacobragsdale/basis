"""A run, and what is done with one afterwards: `run`, `replay`, `resume` and `regress`, which the command line and
`basis.job.api` share; the last three take the steps every part shares from `basis.core.runs`. Every run takes the
same steps:

1. Read the params, strictly, and give the run its id.
2. Describe each destination table and check it against its row, where its database is configured: a table that has
   drifted fails the run before anything loads.
3. Load the inputs, each database in one transaction that only reads.
4. Run the transform, then build each table's changes, conform them, and check them: the core's checks, the values
   against each destination, and the job's own `check`.
5. Write the snapshot.
6. Publish, if asked and nothing gave a reason not to: each database in one transaction, with its ledger rows.

A run that fails at any step records why in its manifest, and a run that loaded its inputs keeps them in its
snapshot, so it can be replayed on a laptop with the code of the day."""

import logging
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

import polars as pl
from pydantic import ValidationError

from basis.core import logs, publish, snapshots
from basis.core import runs as core_runs
from basis.core.databases import Database
from basis.core.destination import TableSpec, check_table, check_values
from basis.core.interval import Interval
from basis.core.logs import utc_now
from basis.core.runs import Regression
from basis.core.settings import Settings
from basis.core.snapshots import InputRecord, PublishRecord, RunRecord, Snapshot, Status
from basis.core.storage import Root
from basis.core.tables import Changes
from basis.job.changes import Planned, plan
from basis.job.diff import OperationDiff, diff
from basis.job.job import Job, repo_root
from basis.job.loader import load
from basis.job.outputs import Target
from basis.job.params import Params
from basis.job.version import VERSION

log = logging.getLogger(__name__)

PACKAGE = "job"
"""basis-job's folder in the run store, which holds each job's manifests: `runs/job/JOB/RUN_ID.json`."""


@dataclass(frozen=True)
class Request:
    params: Path
    interval: Interval | None = None
    data: Path | None = None
    """A folder of files that replace the datasets they name, as a fixture does."""
    labels: dict[str, str] = field(default_factory=dict)
    publish: bool = False
    again: bool = False
    """Publish a plain insert for an interval the ledger says is already published."""
    git_sha: str | None = None


@dataclass(frozen=True)
class Result:
    record: RunRecord
    snapshot: Snapshot | None
    inputs: dict[str, pl.DataFrame]
    planned: dict[str, Planned]

    @property
    def ok(self) -> bool:
        return self.record.status in {"checked", "published"}

    def changes(self, output: str) -> Changes | None:
        return self.planned[output].changes if output in self.planned else None


def read_params(job: Job, path: Path) -> tuple[Params, str]:
    """The params a file holds, validated strictly, and its text, which the snapshot keeps."""
    text = path.read_text(encoding="utf-8")
    try:
        return job.params.model_validate_json(text), text
    except ValidationError as error:
        error.add_note(f"while reading {job.name}'s params from {path}")
        raise


def targets_of(job: Job) -> dict[str, Target]:
    return {name: table.target for name, table in job.outputs.declared().items()}


def run(job: Job, settings: Settings, request: Request, *, snapshot_root: Root, clock: Callable[[], datetime] = utc_now) -> Result:
    params, params_text = read_params(job, request.params)
    run_id = snapshots.new_run_id()
    logs.bind(run_id=run_id, job=job.name)
    started = clock()
    log.info("started", extra={"interval": str(request.interval) if request.interval else None, "params": str(request.params), "labels": request.labels, "version": VERSION})
    databases = core_runs.opener(settings)
    record = _Record(
        run_id=run_id, job=job.name, module=job.module, version=VERSION, git_sha=request.git_sha, started_at=started,
        interval=str(request.interval) if request.interval is not None else None, params_path=str(request.params),
        params_sha256=snapshots.params_digest(params_text), labels=dict(request.labels), data=str(request.data) if request.data is not None else None,
    )  # fmt: skip
    targets = targets_of(job)
    specs, reasons = _check_destinations(targets, settings, databases, publishing=request.publish)
    if reasons:
        return _summarized(_finish(record.done("failed", reasons=reasons, finished_at=clock()), snapshot_root, params_text, {}, {}))
    try:
        frames, inputs = load(job.inputs.declared(), request.interval, open_database=databases, data=request.data)
    except Exception as error:
        log.exception("could not load the inputs")
        return _summarized(_finish(record.done("failed", error=_headline(error), finished_at=clock()), snapshot_root, params_text, {}, {}))
    planned, reasons = _transform(job, params, frames, request.interval)
    for name, entry in planned.items():
        if (spec := specs.get(name)) is not None and entry.changes is not None:
            reasons.extend(f"{name}: {reason}" for reason in check_values(entry.target, spec, entry.changes))
    status: Status = "failed" if reasons else "checked"
    record = record.with_outputs(planned, inputs)
    result = _finish(record.done(status, reasons=reasons, finished_at=clock()), snapshot_root, params_text, frames, planned)
    if reasons or not request.publish or result.snapshot is None:
        _summary(result.record)
        return result
    changes = {name: entry.changes for name, entry in planned.items() if entry.changes is not None}
    publication = publish.Publication(run_id, job.name, request.interval, VERSION, request.again, clock)
    publishes = publish.publish(publication, targets, changes, databases)
    final = record.done(core_runs.status_of(publishes, len({target.database for target in targets.values()})), reasons=[], finished_at=clock(), publishes=publishes)
    snapshot = result.snapshot.save_record(final)
    _summary(final)
    return Result(final, snapshot, frames, planned)


def _check_destinations(targets: Mapping[str, Target], settings: Settings, databases: Callable[[str], Database], *, publishing: bool) -> tuple[dict[str, TableSpec], list[str]]:
    """Each destination table as its database describes it, where the settings configure the database, and every
    reason the run cannot write it. A run that does not publish may leave a database unconfigured, as on files."""
    specs: dict[str, TableSpec] = {}
    reasons: list[str] = []
    by_database: dict[str, dict[str, Target]] = {}
    for name, target in targets.items():
        by_database.setdefault(target.database, {})[name] = target
    for database, tables in sorted(by_database.items()):
        if database not in settings.databases:
            if publishing:
                reasons.append(f"database {database!r} is not configured, and the run publishes to it: set BASIS_DATABASES__{database.upper()}__*")
            else:
                log.info("not checking the destination: its database is not configured", extra={"database": database, "outputs": sorted(tables)})
            continue
        opened = databases(database)
        if publishing and not opened.writable:
            reasons.append(f"database {database!r} has no write settings, and the run publishes to it: set BASIS_DATABASES__{database.upper()}__WRITE__*")
        with opened.reading() as reader:
            for name, target in tables.items():
                spec = reader.describe(target.table)
                if spec is None:
                    reasons.append(f"{name}: {target.table} does not exist in {database}: `basis ddl` prints a CREATE TABLE for it")
                    continue
                specs[name] = spec
                reasons.extend(f"{name}: {reason}" for reason in check_table(target, spec))
    return specs, reasons


def _transform(job: Job, params: Params, frames: Mapping[str, pl.DataFrame], interval: Interval | None) -> tuple[dict[str, Planned], list[str]]:
    """Each table's changes, and every reason not to write them. A transform or check that raises is a reason that
    names the line of job code that raised; the log holds its traceback."""
    inputs = job.inputs(frames, interval=interval)
    try:
        with logs.timed(log, "transformed"):
            outputs = job.run_transform(inputs, params)
    except Exception as error:
        log.exception("the transform raised")
        return {}, [f"the transform raised {_headline(error)}{_at(error, job.directory)}"]
    planned = plan(outputs, interval)
    reasons = [reason for entry in planned.values() for reason in entry.reasons]
    try:
        reasons.extend(f"check: {reason}" for reason in job.run_check(inputs, outputs, params))
    except Exception as error:
        log.exception("the job's check raised")
        reasons.append(f"the job's check raised {_headline(error)}{_at(error, job.directory)}")
    for reason in reasons:
        log.warning("not writing: %s", reason)
    return planned, reasons


def _headline(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _at(error: BaseException, directory: Path) -> str:
    """Where in the job's code an error surfaced, as `, at jobs/positions/job.py:14 in transform`."""
    ours = [frame for frame in traceback.extract_tb(error.__traceback__) if Path(frame.filename).resolve().is_relative_to(directory)]
    if not ours:
        return ""
    frame = ours[-1]
    return f", at {Path(frame.filename).resolve().relative_to(repo_root(directory))}:{frame.lineno} in {frame.name}"


@dataclass(frozen=True)
class _Record:
    """A run's record as it is built, step by step."""

    run_id: str
    job: str
    module: str
    version: str
    git_sha: str | None
    started_at: datetime
    interval: str | None
    params_path: str
    params_sha256: str
    labels: dict[str, str]
    data: str | None
    replay_of: str | None = None
    inputs: dict[str, InputRecord] = field(default_factory=dict)
    outputs: dict[str, snapshots.OutputRecord] = field(default_factory=dict)

    def with_outputs(self, planned: Mapping[str, Planned], inputs: Mapping[str, InputRecord]) -> _Record:
        outputs = {name: snapshots.output_record(entry.target.database, entry.target.table, type(entry.target.write).__name__, entry.changes) for name, entry in planned.items()}
        return replace(self, inputs=dict(inputs), outputs=outputs)

    def done(self, status: Status, *, finished_at: datetime, reasons: list[str] | None = None, error: str | None = None, publishes: dict[str, PublishRecord] | None = None) -> RunRecord:
        return RunRecord(
            run_id=self.run_id, job=self.job, module=self.module, version=self.version, git_sha=self.git_sha, started_at=self.started_at,
            finished_at=finished_at, interval=self.interval, params_path=self.params_path, params_sha256=self.params_sha256, labels=self.labels,
            data=self.data, replay_of=self.replay_of, status=status, inputs=self.inputs, outputs=self.outputs, reasons=reasons or [], error=error,
            publishes=publishes or {},
        )  # fmt: skip


def _finish(record: RunRecord, root: Root | None, params_text: str, frames: Mapping[str, pl.DataFrame], planned: Mapping[str, Planned]) -> Result:
    changes = {name: entry.changes for name, entry in planned.items() if entry.changes is not None}
    snapshot = snapshots.write(root, record, params_text, frames, changes, package=PACKAGE) if root is not None else None
    if snapshot is not None:
        log.info("saved the snapshot", extra={"snapshot": snapshot.path.location})
    return Result(record, snapshot, dict(frames), dict(planned))


def _summarized(result: Result) -> Result:
    _summary(result.record)
    return result


def _summary(record: RunRecord) -> None:
    """The run's one summary record: what a dashboard or a monitor reads."""
    duration = (record.finished_at - record.started_at).total_seconds() * 1000 if record.finished_at is not None else None
    log.info(
        "run summary",
        extra={
            "status": record.status,
            "interval": record.interval,
            "labels": record.labels,
            "inputs": {name: entry.rows for name, entry in record.inputs.items()},
            "outputs": {name: entry.rows for name, entry in record.outputs.items()},
            "reasons": len(record.reasons),
            "error": record.error,
            "publishes": {name: entry.status for name, entry in record.publishes.items()},
            "run_ms": round(duration, 1) if duration is not None else None,
        },
    )


def replay(job: Job, source: Snapshot, *, params: Path | None = None, snapshot_root: Root | None = None, clock: Callable[[], datetime] = utc_now) -> tuple[Result, list[OperationDiff]]:
    """Run a saved run's transform again on its inputs, with the code of the day and, if given, other params, and how
    its changes differ from the run's. Reads no database and writes none."""
    opened = core_runs.replaying(source, name=job.name, module=job.module, params=params, parse=job.params.model_validate_json, reads=job.inputs.declared())
    original = opened.original
    interval = original.parsed_interval()
    record = _Record(
        run_id=opened.run_id, job=job.name, module=job.module, version=VERSION, git_sha=None, started_at=clock(), interval=original.interval,
        params_path=opened.params_path, params_sha256=snapshots.params_digest(opened.params_text),
        labels={"replay_of": original.run_id}, data=None, replay_of=original.run_id,
        inputs={name: InputRecord(source=f"snapshot: {original.run_id}", rows=frame.height, read_ms=0.0) for name, frame in opened.saved.items()},
    )  # fmt: skip
    frames = {name: opened.saved[name] for name in job.inputs.declared()}
    planned, reasons = _transform(job, opened.params, frames, interval)
    record = record.with_outputs(planned, record.inputs)
    result = _finish(record.done("failed" if reasons else "checked", reasons=reasons, finished_at=clock()), snapshot_root, opened.params_text, frames, planned)
    before = source.changes()
    targets = targets_of(job)
    diffs = [found for name in sorted(set(before) | set(planned)) if name in targets for found in diff(name, targets[name], before.get(name), result.changes(name))]
    return result, diffs


def resume(job: Job, source: Snapshot, settings: Settings, *, clock: Callable[[], datetime] = utc_now) -> RunRecord:
    """Finish publishing a run that committed to some of its databases and failed on another, from its snapshot."""
    record = source.record()
    logs.bind(run_id=record.run_id, job=job.name)
    if record.status == "published":
        log.info("already published")
        return record
    final = core_runs.resume(source, record, name=job.name, targets=targets_of(job), databases=core_runs.opener(settings), version=VERSION, clock=clock)
    _summary(final)
    return final


def regress(job: Job, root: Root, *, last: int, labels: Mapping[str, str]) -> list[Regression[OperationDiff]]:
    """Replay a job's last `last` published runs whose labels include `labels`, with the code of the day, and how each
    one's changes differ. Writes nothing."""

    def replayed(snapshot: Snapshot) -> tuple[RunRecord, list[OperationDiff]]:
        result, diffs = replay(job, snapshot)
        return result.record, diffs

    return core_runs.regress(root, job.name, package=PACKAGE, last=last, labels=labels, replay=replayed)
