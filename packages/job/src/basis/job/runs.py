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
snapshot, so it can be replayed on a laptop with the code of the day. It keeps each time its clocks gave and each id
its ids made in its record, and a replay is given them again (`handles.py`).

Every run, replay and resume is one trace (`basis.core.traces.trace`), whose root span every SQL statement and GET is
a span under, exported where the settings say, and every record it logs carries the trace's ids and the job's name as
`service`."""

import logging
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from uuid import UUID

import httpx2
import polars as pl
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Tracer
from pydantic import ValidationError

from basis.core import fakes, logs, publish, snapshots, traces
from basis.core import runs as core_runs
from basis.core.databases import Database
from basis.core.destination import TableSpec, check_table, check_values
from basis.core.interval import Interval
from basis.core.logs import utc_now
from basis.core.opened import client
from basis.core.publish import refused_overlap
from basis.core.runs import Regression
from basis.core.settings import Settings, SettingsError, Traces, publish_refusal, variable
from basis.core.snapshots import InputRecord, PublishRecord, RunRecord, Snapshot, Status
from basis.core.storage import Root
from basis.core.tables import Changes
from basis.job import handles
from basis.job.changes import Planned, plan
from basis.job.diff import OperationDiff, diff
from basis.job.handles import Kept
from basis.job.inputs import Clock, Fetch
from basis.job.job import Job, JobError, repo_root
from basis.job.loader import Reach, load
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


def run(  # noqa: PLR0913 -- what the run reads and where it saves, by keyword
    job: Job,
    settings: Settings,
    request: Request,
    *,
    snapshot_root: Root,
    clock: Callable[[], datetime] = utc_now,
    databases: Callable[[str], Database] | None = None,
    faked: Mapping[str, fakes.Fake] | None = None,
    provider: TracerProvider | None = None,
) -> Result:
    """Run `job` as `request` asks, reading and writing the databases `settings` configure, each opened by
    `databases` when given, such as the QA tier's, which refuse a table it does not allow, and reaching the services
    they configure. `faked` names the fakes of a test's own, by the name of what each fakes: a fetch's service, or a
    clock or ids of the job's `Inputs`. The run is one trace, on `provider` where a test gives one."""
    params, params_text = read_params(job, request.params)
    given = _checked(job, faked or {})
    run_id = snapshots.new_run_id()
    logs.bind(run_id=run_id, job=job.name, service=job.name)
    with traces.trace("run", service=job.name, settings=settings.traces, version=settings.git_sha, environment=None, given=provider, **{"basis.job": job.name, "basis.run_id": run_id}) as (tracer, _):
        return _run(job, settings, request, _Run(params, params_text, run_id, snapshot_root, clock, databases, given, tracer))


@dataclass(frozen=True)
class _Run:
    """A run under way, in its trace."""

    params: Params
    params_text: str
    run_id: str
    snapshot_root: Root
    clock: Callable[[], datetime]
    databases: Callable[[str], Database] | None
    faked: Mapping[str, fakes.Fake]
    tracer: Tracer


def _run(job: Job, settings: Settings, request: Request, this: _Run) -> Result:
    params, params_text, run_id, snapshot_root, clock = this.params, this.params_text, this.run_id, this.snapshot_root, this.clock
    started = clock()
    log.info("started", extra={"interval": str(request.interval) if request.interval else None, "params": str(request.params), "labels": request.labels, "version": VERSION})
    databases = this.databases if this.databases is not None else core_runs.opener(settings)
    record = _Record(
        run_id=run_id, job=job.name, module=job.module, version=VERSION, git_sha=request.git_sha, started_at=started,
        interval=str(request.interval) if request.interval is not None else None, params_path=str(request.params),
        params_sha256=snapshots.params_digest(params_text), labels=dict(request.labels), data=str(request.data) if request.data is not None else None,
        again=request.again,
    )  # fmt: skip
    targets = targets_of(job)
    specs, reasons = _check_destinations(targets, settings, databases, publishing=request.publish)
    if reasons:
        return _summarized(_finish(record.done("failed", reasons=reasons, finished_at=clock()), snapshot_root, params_text, {}, {}))
    try:
        frames, inputs = load(job.inputs.declared(), request.interval, open_database=databases, reach=_reach(settings, this.faked), tracer=this.tracer, data=request.data)
    except Exception as error:
        log.exception("could not load the inputs")
        return _summarized(_finish(record.done("failed", error=_headline(error), finished_at=clock()), snapshot_root, params_text, {}, {}))
    try:
        kept = handles.live(job.inputs, this.faked, this.tracer)
    except Exception as error:  # a defect: the inputs' declarations are checked when their class is made
        log.exception("could not give the transform its clocks and ids")
        return _summarized(_finish(record.with_outputs({}, inputs).done("failed", error=_headline(error), finished_at=clock()), snapshot_root, params_text, frames, {}))
    planned, reasons = _transform(job, params, frames, request.interval, kept)
    for name, entry in planned.items():
        if (spec := specs.get(name)) is not None and entry.changes is not None:
            reasons.extend(f"{name}: {reason}" for reason in check_values(entry.target, spec, entry.changes))
    status: Status = "failed" if reasons else "checked"
    record = record.with_outputs(planned, inputs).with_made(kept)
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


def _checked(job: Job, faked: Mapping[str, fakes.Fake]) -> dict[str, fakes.Fake]:
    """The fakes a test gives, each of a service a fetch of the job's reaches, or of a clock or ids its `Inputs`
    declare, by name, of its kind."""
    declared: dict[str, type[fakes.Fake]] = {dataset.http: fakes.Http for dataset in job.inputs.declared().values() if isinstance(dataset, Fetch)}
    declared |= {name: fakes.Clock if isinstance(given, Clock) else fakes.Ids for name, given in job.inputs.declared_resources().items()}
    for name, fake in faked.items():
        if (kind := declared.get(name)) is None:
            msg = f"a fake is given for {name}, which {job.name} neither fetches from nor declares: fake the services its fetches name, and its Inputs' clocks and ids"
            raise JobError(msg)
        if not isinstance(fake, kind):
            msg = f"{name} is given a fakes.{type(fake).__name__}, and {job.name} takes a fakes.{kind.__name__} for it"
            raise JobError(msg)
    return dict(faked)


def _reach(settings: Settings, faked: Mapping[str, fakes.Fake]) -> Reach:
    """The client of each service a fetch names: its fake, or as the settings configure it."""

    def reach(name: str) -> httpx2.AsyncClient:
        if isinstance(fake := faked.get(name), fakes.Http):
            return fake.client(name)
        if (service := settings.http.get(name)) is None:
            msg = f"the service {name} is not configured, and a fetch reads it: set {variable(('http', name, 'url'))}, as .env.example shows"
            raise SettingsError(msg)
        return client(name, service)

    return reach


def failure_of(record: RunRecord, *, publish: bool) -> tuple[str, str] | None:
    """Why a run did not do what it was asked, and the fix, or None when it did: `checked` without `--publish`, or
    `published` with it."""
    if record.status == ("published" if publish else "checked"):
        return None
    run = f"run {record.run_id} of {record.job}"
    if record.status == "partial":
        return f"{run} published to some of its databases and failed on another", f"fix what its publishes record, then `{resume_command(record)}`"
    if record.error is not None:
        return f"{run} failed: {record.error}", "fix it, and run the job again"
    if record.reasons:
        more = f", and {len(record.reasons) - 1} more the log lists" if len(record.reasons) > 1 else ""
        replay = f"; `basis replay {record.job} {record.run_id}` reruns it on what it read" if record.inputs else ""
        return f"{run} wrote nothing, because {record.reasons[0]}{more}", f"fix {'each' if more else 'it'}, and run the job again{replay}"
    if errors := [f"{name}: {entry.error}" for name, entry in record.publishes.items() if entry.error is not None]:
        again = refused_overlap(record.publishes)
        return (
            f"{run} committed to no database: {'; '.join(errors)}",
            "run the job again with --again, if its rows are meant to be inserted a second time" if again else "fix it, and run the job again",
        )
    return f"{run} ended {record.status} with no reason recorded, when it was asked to publish", "its log records say what happened; run it again, and report it if it does the same"


def resume_command(record: RunRecord) -> str:
    """The command that finishes publishing `record`'s run: told `--again` as the run was, or where a database refused
    it for an interval its ledger has, since the next attempt needs it then."""
    again = record.again or refused_overlap(record.publishes)
    return f"basis resume {record.job} {record.run_id}{' --again' if again else ''}"


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
        if publishing and (refused := publish_refusal(database, settings.database(database))) is not None:
            reasons.append(f"database {database!r} {refused[0]}, and the run publishes to it: {refused[1]}")
        with opened.reading() as reader:
            for name, target in tables.items():
                spec = reader.describe(target.table)
                if spec is None:
                    reasons.append(f"{name}: {target.table} does not exist in {database}: `basis ddl` prints a CREATE TABLE for it")
                    continue
                specs[name] = spec
                reasons.extend(f"{name}: {reason}" for reason in check_table(target, spec))
    return specs, reasons


def _transform(job: Job, params: Params, frames: Mapping[str, pl.DataFrame], interval: Interval | None, kept: Kept) -> tuple[dict[str, Planned], list[str]]:
    """Each table's changes, and every reason not to write them. A transform or check that raises is a reason that
    names the line of job code that raised; the log holds its traceback."""
    inputs = job.inputs(frames, interval=interval, handles=kept.handles)
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
    again: bool = False
    inputs: dict[str, InputRecord] = field(default_factory=dict)
    outputs: dict[str, snapshots.OutputRecord] = field(default_factory=dict)
    clocks: dict[str, list[datetime]] = field(default_factory=dict)
    ids: dict[str, list[UUID]] = field(default_factory=dict)

    def with_outputs(self, planned: Mapping[str, Planned], inputs: Mapping[str, InputRecord]) -> _Record:
        outputs = {name: snapshots.output_record(entry.target.database, entry.target.table, type(entry.target.write).__name__, entry.changes) for name, entry in planned.items()}
        return replace(self, inputs=dict(inputs), outputs=outputs)

    def with_made(self, kept: Kept) -> _Record:
        """The record, with each value the run's clocks and ids gave its transform."""
        return replace(self, clocks={name: list(values) for name, values in kept.clocks.items()}, ids={name: list(values) for name, values in kept.ids.items()})

    def done(self, status: Status, *, finished_at: datetime, reasons: list[str] | None = None, error: str | None = None, publishes: dict[str, PublishRecord] | None = None) -> RunRecord:
        return RunRecord(
            run_id=self.run_id, job=self.job, module=self.module, version=self.version, git_sha=self.git_sha, started_at=self.started_at,
            finished_at=finished_at, interval=self.interval, params_path=self.params_path, params_sha256=self.params_sha256, labels=self.labels,
            data=self.data, replay_of=self.replay_of, again=self.again, status=status, inputs=self.inputs, outputs=self.outputs, reasons=reasons or [], error=error,
            publishes=publishes or {}, clocks=self.clocks, ids=self.ids,
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


def replay(  # noqa: PLR0913 -- what it replays, and where it saves and traces, by keyword
    job: Job,
    source: Snapshot,
    *,
    params: Path | None = None,
    snapshot_root: Root | None = None,
    clock: Callable[[], datetime] = utc_now,
    tracing: Traces | None = None,
    provider: TracerProvider | None = None,
) -> tuple[Result, list[OperationDiff]]:
    """Run a saved run's transform again on its inputs, with the code of the day and, if given, other params, and how
    its changes differ from the run's: one trace, exported where `tracing` says, or on a test's `provider`. Reads no
    database and writes none, and gives the transform the times and ids the run kept."""
    with (
        logs.fields(job=job.name, service=job.name, replay_of=source.run_id),  # the replay's own, whatever an earlier run in the process bound
        traces.trace("replay", service=job.name, settings=tracing, version=None, environment=None, given=provider, **{"basis.job": job.name, "basis.replay_of": source.run_id}) as (tracer, _),
    ):
        return _replay(job, source, params=params, snapshot_root=snapshot_root, clock=clock, tracer=tracer)


def _replay(job: Job, source: Snapshot, *, params: Path | None, snapshot_root: Root | None, clock: Callable[[], datetime], tracer: Tracer) -> tuple[Result, list[OperationDiff]]:  # noqa: PLR0913 -- replay's, by keyword
    opened = core_runs.replaying(source, name=job.name, module=job.module, params=params, parse=job.params.model_validate_json, reads=job.inputs.declared())
    with logs.fields(run_id=opened.run_id):
        return _replayed(job, source, opened, snapshot_root=snapshot_root, clock=clock, tracer=tracer)


def _replayed(job: Job, source: Snapshot, opened: core_runs.Replay[Params], *, snapshot_root: Root | None, clock: Callable[[], datetime], tracer: Tracer) -> tuple[Result, list[OperationDiff]]:  # noqa: PLR0913 -- replay's, by keyword
    original = opened.original
    interval = original.parsed_interval()
    record = _Record(
        run_id=opened.run_id, job=job.name, module=job.module, version=VERSION, git_sha=None, started_at=clock(), interval=original.interval,
        params_path=opened.params_path, params_sha256=snapshots.params_digest(opened.params_text),
        labels={"replay_of": original.run_id}, data=None, replay_of=original.run_id,
        inputs={name: InputRecord(source=f"snapshot: {original.run_id}", rows=frame.height, read_ms=0.0) for name, frame in opened.saved.items()},
    )  # fmt: skip
    frames = {name: opened.saved[name] for name in job.inputs.declared()}
    kept = handles.replayed(job.inputs, original, tracer)
    planned, reasons = _transform(job, opened.params, frames, interval, kept)
    record = record.with_outputs(planned, record.inputs).with_made(kept)
    result = _finish(record.done("failed" if reasons else "checked", reasons=reasons, finished_at=clock()), snapshot_root, opened.params_text, frames, planned)
    before = source.changes()
    targets = targets_of(job)
    diffs = [found for name in sorted(set(before) | set(planned)) if name in targets for found in diff(name, targets[name], before.get(name), result.changes(name))]
    return result, diffs


def resume(job: Job, source: Snapshot, settings: Settings, *, again: bool = False, clock: Callable[[], datetime] = utc_now) -> RunRecord:
    """Finish publishing a run that committed to some of its databases and failed on another, from its snapshot: told
    `again` as the run was, or as `again` says now."""
    record = source.record()
    logs.bind(run_id=record.run_id, job=job.name, service=job.name)
    with traces.trace("resume", service=job.name, settings=settings.traces, version=settings.git_sha, environment=None, **{"basis.job": job.name, "basis.run_id": record.run_id}):
        if record.status == "published":
            log.info("already published")
            return record
        final = core_runs.resume(source, record, name=job.name, targets=targets_of(job), databases=core_runs.opener(settings), version=VERSION, clock=clock, again=again)
        _summary(final)
        return final


def regress(job: Job, root: Root, *, last: int, labels: Mapping[str, str]) -> list[Regression[OperationDiff]]:
    """Replay a job's last `last` published runs whose labels include `labels`, with the code of the day, and how each
    one's changes differ. Writes nothing."""

    def replayed(snapshot: Snapshot) -> tuple[RunRecord, list[OperationDiff]]:
        result, diffs = replay(job, snapshot)
        return result.record, diffs

    return core_runs.regress(root, job.name, package=PACKAGE, last=last, labels=labels, replay=replayed)
