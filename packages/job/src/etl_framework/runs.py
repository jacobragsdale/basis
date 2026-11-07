"""A run, and what is done with one afterwards: `run`, `replay`, `resume` and `regress`, which the command line and
`etl_framework.api` share. Every run takes the same steps:

1. Read the params, strictly, and give the run its id.
2. Describe each destination table and check it against its row, where its database is configured: a table that has
   drifted fails the run before anything loads.
3. Load the inputs, each database in one transaction that only reads.
4. Run the transform, then build each table's changes, conform them, and check them: the core's checks, the values
   against each destination, and the job's own `check`.
5. Write the snapshot.
6. Publish, if asked and nothing gave a reason not to: each database in one transaction, with its ledger rows.

A run that fails at any step records why in its `run.json`, and a run that loaded its inputs keeps them in its
snapshot, so it can be replayed on a laptop with the code of the day."""

import logging
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import cache
from pathlib import Path

import polars as pl
from pydantic import ValidationError

from etl_framework import logs, publish, snapshots
from etl_framework.changes import Planned, plan
from etl_framework.databases import Database, open_database
from etl_framework.destination import TableSpec, check_table, check_values
from etl_framework.diff import OperationDiff, diff
from etl_framework.interval import Interval
from etl_framework.job import Job, repo_root
from etl_framework.loader import load
from etl_framework.logs import utc_now
from etl_framework.outputs import Changes, Target
from etl_framework.params import Params
from etl_framework.settings import Settings
from etl_framework.snapshots import InputRecord, PublishRecord, RunRecord, Snapshot, Status
from etl_framework.storage import Root
from etl_framework.version import VERSION

log = logging.getLogger(__name__)


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
    databases = _databases(settings)
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
    final = record.done(_published(publishes, len({target.database for target in targets.values()})), reasons=[], finished_at=clock(), publishes=publishes)
    result.snapshot.save_record(final)
    _summary(final)
    return Result(final, result.snapshot, frames, planned)


def _databases(settings: Settings) -> Callable[[str], Database]:
    @cache
    def opened(name: str) -> Database:
        return open_database(settings, name)

    return opened


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
                reasons.append(f"database {database!r} is not configured, and the run publishes to it: set ETL_DATABASES__{database.upper()}__*")
            else:
                log.info("not checking the destination: its database is not configured", extra={"database": database, "outputs": sorted(tables)})
            continue
        opened = databases(database)
        if publishing and not opened.writable:
            reasons.append(f"database {database!r} has no write settings, and the run publishes to it: set ETL_DATABASES__{database.upper()}__WRITE__*")
        with opened.reading() as reader:
            for name, target in tables.items():
                spec = reader.describe(target.table)
                if spec is None:
                    reasons.append(f"{name}: {target.table} does not exist in {database}: `etl ddl` prints a CREATE TABLE for it")
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


def _published(publishes: Mapping[str, PublishRecord], databases: int) -> Status:
    committed = sum(record.status == "committed" for record in publishes.values())
    if committed == databases:
        return "published"
    return "partial" if committed else "failed"


def _finish(record: RunRecord, root: Root | None, params_text: str, frames: Mapping[str, pl.DataFrame], planned: Mapping[str, Planned]) -> Result:
    changes = {name: entry.changes for name, entry in planned.items() if entry.changes is not None}
    snapshot = snapshots.write(root, record, params_text, dict(frames), changes) if root is not None else None
    if snapshot is not None:
        log.info("saved the snapshot", extra={"snapshot": snapshot.root.location})
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
    original = source.record()
    if original.job not in {job.name, job.module}:
        msg = f"run {original.run_id} is a run of {original.job}, not {job.name}"
        raise ValueError(msg)
    params_text = params.read_text(encoding="utf-8") if params is not None else source.params_json()
    parsed = job.params.model_validate_json(params_text)
    run_id = snapshots.new_run_id()
    logs.bind(run_id=run_id, job=job.name, replay_of=original.run_id)
    saved = source.inputs()
    if missing := sorted(set(job.inputs.declared()) - set(saved)):
        msg = f"run {original.run_id} did not read {missing}, which the job reads now: a replay runs on what the run read"
        raise ValueError(msg)
    interval = original.parsed_interval()
    record = _Record(
        run_id=run_id, job=job.name, module=job.module, version=VERSION, git_sha=None, started_at=clock(), interval=original.interval,
        params_path=str(params) if params is not None else f"{original.run_id}/params.json", params_sha256=snapshots.params_digest(params_text),
        labels={"replay_of": original.run_id}, data=None, replay_of=original.run_id,
        inputs={name: InputRecord(source=f"snapshot: {original.run_id}", rows=frame.height, read_ms=0.0) for name, frame in saved.items()},
    )  # fmt: skip
    frames = {name: saved[name] for name in job.inputs.declared()}
    planned, reasons = _transform(job, parsed, frames, interval)
    record = record.with_outputs(planned, record.inputs)
    result = _finish(record.done("failed" if reasons else "checked", reasons=reasons, finished_at=clock()), snapshot_root, params_text, frames, planned)
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
    if record.reasons or not record.publishes:
        msg = f"run {record.run_id} is {record.status} and never published: only a run that failed while publishing resumes; run it again instead"
        raise ValueError(msg)
    targets = targets_of(job)
    for name, output in record.outputs.items():
        now = (targets[name].database, targets[name].table) if name in targets else None
        if now != (output.database, output.table):
            msg = f"{name} is written to {now} now, and the run wrote ({output.database}, {output.table}): resume with the code the run ran"
            raise ValueError(msg)
    publication = publish.Publication(record.run_id, job.name, record.parsed_interval(), VERSION, False, clock)  # noqa: FBT003 -- a dataclass field, by position
    publishes = record.publishes | publish.publish(publication, targets, source.changes(), _databases(settings))
    final = RunRecord.model_validate(record.model_dump() | {"publishes": publishes, "status": _published(publishes, len({output.database for output in record.outputs.values()}))})
    source.save_record(final)
    _summary(final)
    return final


@dataclass(frozen=True)
class Regression:
    run_id: str
    diffs: list[OperationDiff]
    reasons: list[str]


def regress(job: Job, root: Root, *, last: int, labels: Mapping[str, str]) -> list[Regression]:
    """Replay a job's last `last` published runs whose labels include `labels`, with the code of the day, and how each
    one's changes differ. Writes nothing."""
    chosen = [snapshot for snapshot in snapshots.runs_of(root, job.name) if (found := snapshot.record()).status == "published" and labels.items() <= found.labels.items()][:last]
    start = time.perf_counter()
    found: list[Regression] = []
    for snapshot in chosen:
        result, diffs = replay(job, snapshot)
        found.append(Regression(snapshot.root.name, diffs, result.record.reasons))
    log.info("regressed", extra={"runs": len(found), "changed": sum(bool(entry.diffs or entry.reasons) for entry in found), "ms": logs.ms_since(start)})
    return found
