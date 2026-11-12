"""A run, and what is done with one afterwards: `run`, `replay`, `resume` and `regress`, which the command line and
`basis.job.api` share; the last three take the steps every part shares from `basis.core.runs`. Every run takes the
same steps:

1. Read the params, strictly, and give the run its id.
2. Describe each destination table and check it against its row, where its database is configured: a table that has
   drifted fails the run before anything loads.
3. Load the inputs, each database in one transaction that only reads, and keep each as Arrow, which nothing changes.
4. Run the transform, then build each table's changes, conform them, and check them: the core's checks, the values
   against each destination, and the job's own `check`. The transform and the check are each given frames of their
   own of the inputs, since a pandas frame changes in place.
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
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import cache
from pathlib import Path
from uuid import UUID

import httpx2
import pandas as pd
import pyarrow as pa
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Tracer
from pydantic import ValidationError

from basis.core import ddl as core_ddl
from basis.core import fakes, folders, frames, lake, logs, mongo, publish, snapshots, traces
from basis.core import runs as core_runs
from basis.core.databases import Database
from basis.core.destination import TableSpec, check_table, check_values
from basis.core.errors import BasisError
from basis.core.interval import Interval
from basis.core.logs import utc_now
from basis.core.model import Model
from basis.core.opened import SYSTEM_WAITS, Waits, client
from basis.core.publish import refused_overlap
from basis.core.settings import Settings, SettingsError, publish_refusal, variable
from basis.core.snapshots import InputRecord, PublishRecord, RunRecord, Snapshot, Status
from basis.core.storage import Root
from basis.core.tables import Changes
from basis.job import effects, expectations, handles
from basis.job.changes import Planned, plan
from basis.job.handles import Kept
from basis.job.inputs import Clock, Fetch, Mongo, sql_problems
from basis.job.job import Job, JobError, repo_root
from basis.job.loader import MongoOpener, Reach, load
from basis.job.outputs import Lake, Message, Target
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
    tables: dict[str, pa.Table]
    """Each input as the run loaded it, as Arrow, which nothing changes: what the transform and the check were each
    given a frame of, and what the snapshot saved."""
    planned: dict[str, Planned]

    @property
    def inputs(self) -> dict[str, pd.DataFrame]:
        """Each input as a frame of its own, made afresh from `tables`, so that changing it changes nothing the run
        kept."""
        return fresh(self.tables)

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


def open_sql(job: Job) -> None:
    """Refuse a job whose query files are missing or bind a name but the interval's, before anything reads settings
    or data: a run on its fixture reads no query, and would pass without one."""
    if problems := sql_problems(job.inputs):
        raise BasisError("; ".join(problems))


def run(
    job: Job,
    settings: Settings,
    request: Request,
    *,
    snapshot_root: Root,
    clock: Callable[[], datetime] = utc_now,
    databases: Callable[[str], Database] | None = None,
    faked: Mapping[str, fakes.Fake] | None = None,
    provider: TracerProvider | None = None,
    environment: str | None = None,
    open_folder: folders.Opener | None = None,
    open_lake: lake.Opener | None = None,
) -> Result:
    """Run `job` as `request` asks, reading and writing the databases `settings` configure, each opened by
    `databases` when given, such as the QA tier's, which refuse a table it does not allow, and the folders they
    configure, or those `open_folder` gives, and the lakes, or those `open_lake` gives, and reaching the services they
    configure. `faked` names the fakes of a test's own, by the name of what each fakes: a fetch's service, or a clock or
    ids of the job's `Inputs`. The run is one trace, on `provider` where a test gives one, in `environment`,
    the one the settings are of, where there is one."""
    open_sql(job)
    params, params_text = read_params(job, request.params)
    given = _checked(job, faked or {})
    run_id = snapshots.new_run_id()
    logs.bind(run_id=run_id, job=job.name, service=job.name)
    attributes = {"basis.job": job.name, "basis.run_id": run_id}
    with traces.trace("run", service=job.name, settings=settings.traces, version=settings.git_sha, environment=environment, given=provider, **attributes) as (tracer, _):
        opened = open_folder if open_folder is not None else folders.opener(settings)
        with mongo_opener(settings, given, tracer) as open_mongo:
            lakes = open_lake if open_lake is not None else lake.opener(settings)
            return _run(job, settings, request, _Run(params, params_text, run_id, snapshot_root, clock, databases, given, tracer, opened, open_mongo, lakes))


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
    open_folder: folders.Opener
    open_mongo: MongoOpener
    open_lake: lake.Opener


@contextmanager
def mongo_opener(settings: Settings, faked: Mapping[str, fakes.Fake], tracer: Tracer) -> Generator[MongoOpener]:
    """Each Mongo database a run names, as the settings configure it, or its fake, opened once however often asked,
    with the most documents a read may return: the settings', or a million for a fake the settings do not name; each
    real one's client closed on leaving."""
    clients: list[mongo.MongoStore] = []

    @cache
    def opened(name: str) -> tuple[mongo.Store, int]:
        configured = settings.mongo.get(name)
        limit = configured.max_documents if configured is not None else 1_000_000
        if isinstance(fake := faked.get(name), fakes.Mongo):
            return fake, limit
        clients.append(store := mongo.store(name, settings, tracer))
        return store, limit

    try:
        yield opened
    finally:
        for client in clients:
            client.close()


def _run(job: Job, settings: Settings, request: Request, this: _Run) -> Result:
    params, params_text, run_id, snapshot_root, clock = this.params, this.params_text, this.run_id, this.snapshot_root, this.clock
    started = clock()
    log.info("started", extra={"interval": str(request.interval) if request.interval else None, "params": str(request.params), "labels": request.labels, "version": VERSION})
    databases = this.databases if this.databases is not None else core_runs.opener(settings)
    record = Draft(
        run_id=run_id, job=job.name, module=job.module, version=VERSION, git_sha=request.git_sha, started_at=started,
        interval=str(request.interval) if request.interval is not None else None, params_path=str(request.params),
        params_sha256=snapshots.params_digest(params_text), labels=dict(request.labels), data=str(request.data) if request.data is not None else None,
        again=request.again, publish=request.publish, trace_id=logs.trace_id(),
    )  # fmt: skip
    targets = targets_of(job)
    reach = effects.Reach(run_id, job.name, request.interval, request.again, clock, settings, databases, this.faked, this.tracer, this.open_folder, this.open_mongo, this.open_lake)
    try:
        specs, reasons = _check_destinations(targets, settings, databases, publishing=request.publish)
        reasons += _check_ledgers(job, settings, databases, publishing=request.publish)
        reasons += effects.check(job.outputs, reach) if request.publish else []
    except Exception as error:
        log.exception("could not check the destinations")
        return _summarized(finish(record.done("failed", error=_headline(error), finished_at=clock()), snapshot_root, params_text, {}, {}))
    if reasons:
        return _summarized(finish(record.done("failed", reasons=reasons, finished_at=clock()), snapshot_root, params_text, {}, {}))
    try:
        declared, fetched = job.inputs.declared(), _reach(settings, this.faked)
        loaded, inputs = load(declared, request.interval, open_database=databases, reach=fetched, tracer=this.tracer, data=request.data, open_folder=this.open_folder, open_mongo=this.open_mongo)
        tables = {name: frames.to_arrow(frame) for name, frame in loaded.items()}
        past = expectations.history(snapshot_root, job.name, job.outputs, package=PACKAGE)  # the job's recent published rows, for its row counts
    except Exception as error:
        log.exception("could not load the inputs")
        return _summarized(finish(record.done("failed", error=_headline(error), finished_at=clock()), snapshot_root, params_text, {}, {}))
    try:
        kept = handles.live(job.inputs, this.faked, this.tracer)
    except Exception as error:  # a defect: the inputs' declarations are checked when their class is made
        log.exception("could not give the transform its clocks and ids")
        return _summarized(finish(record.with_outputs({}, inputs).done("failed", error=_headline(error), finished_at=clock()), snapshot_root, params_text, tables, {}))
    made = transformed(job, params, tables, request.interval, kept, past=past)
    planned, reasons = made.planned, made.reasons
    for name, entry in planned.items():
        if (spec := specs.get(name)) is not None and entry.changes is not None:
            reasons.extend(f"{name}: {reason}" for reason in check_values(entry.target, spec, entry.changes))
    status: Status = "failed" if reasons else "publishing" if request.publish else "checked"  # saved before the first commit, so a run stopped while it publishes says so
    record = record.with_outputs(planned, inputs).with_made(kept).with_effects(made.effects_planned.records)
    result = finish(record.done(status, reasons=reasons, finished_at=clock()), snapshot_root, params_text, tables, planned, reports=made.effects_planned.frames)
    if reasons or not request.publish or result.snapshot is None:
        log_summary(result.record)
        return result
    changes = {name: entry.changes for name, entry in planned.items() if entry.changes is not None}
    publication = publish.Publication(run_id, job.name, request.interval, VERSION, request.again, clock)
    publishes = publish.publish(publication, targets, changes, databases, checked=specs)
    status = core_runs.status_of(publishes, len({target.database for target in targets.values()}))
    if status == "published":
        published = result.record.model_copy(update={"publishes": publishes})
        done, complete = effects.apply(job.outputs, published, made.effects_planned.frames.__getitem__, reach)
        status = "published" if complete else "partial"
    else:
        done = {}
    final = record.done(status, reasons=[], finished_at=clock(), publishes=publishes, made=done)
    snapshot = result.snapshot.save_record(final)
    log_summary(final)
    return Result(final, snapshot, tables, planned)


def _checked(job: Job, faked: Mapping[str, fakes.Fake]) -> dict[str, fakes.Fake]:
    """The fakes a test gives, each of a service a fetch of the job's reaches, or of a clock or ids its `Inputs`
    declare, by name, of its kind."""
    declared: dict[str, type[fakes.Fake]] = {dataset.http: fakes.Http for dataset in job.inputs.declared().values() if isinstance(dataset, Fetch)}
    declared |= {name: fakes.Clock if isinstance(given, Clock) else fakes.Ids for name, given in job.inputs.declared_resources().items()}
    declared |= {dataset.mongo: fakes.Mongo for dataset in job.inputs.declared().values() if isinstance(dataset, Mongo)}
    declared |= effects.fakes_of(job.outputs)
    for name, fake in faked.items():
        if (kind := declared.get(name)) is None:
            msg = f"a fake is given for {name}, which {job.name} neither fetches from, declares nor sends to: fake its fetches' services, its clocks and ids, and its messages' channels and emails"
            raise JobError(msg)
        if not isinstance(fake, kind):
            msg = f"{name} is given a fakes.{type(fake).__name__}, and {job.name} takes a fakes.{kind.__name__} for it"
            raise JobError(msg)
    return dict(faked)


def _reach(settings: Settings, faked: Mapping[str, fakes.Fake]) -> Reach:
    """The client of each service a fetch names, and how it waits between attempts: its fake's, or as the settings
    configure it, waiting as the system does."""

    def reach(name: str) -> tuple[httpx2.AsyncClient, Waits]:
        if isinstance(fake := faked.get(name), fakes.Http):
            return fake.client(name), fake.waits
        if (service := settings.http.get(name)) is None:
            msg = f"the service {name} is not configured, and a fetch reads it: set {variable(('http', name, 'url'))}, as .env.example shows"
            raise SettingsError(msg)
        return client(name, service), SYSTEM_WAITS

    return reach


def failure_of(record: RunRecord, *, publish: bool) -> tuple[str, str] | None:
    """Why a run did not do what it was asked, and the fix, or None when it did: `checked` without `--publish`, or
    `published` with it."""
    if record.status == ("published" if publish else "checked"):
        return None
    run = f"run {record.run_id} of {record.job}"
    if record.status in {"partial", "publishing"}:
        return _partial(run, record)
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
            "where a run it names stopped before it finished publishing, `basis resume` finishes it; run the job again with --again only if its rows are meant to be inserted a second time"
            if again
            else "fix it, and run the job again",
        )
    return f"{run} ended {record.status} with no reason recorded, when it was asked to publish", "its log records say what happened; run it again, and report it if it does the same"


def _partial(run: str, record: RunRecord) -> tuple[str, str]:
    """Why a run that published to some of its databases, or could not send a message, or stopped while it published,
    is not done, and the fix."""
    if record.status == "publishing":
        return f"{run} began to publish and stopped before it recorded what it published", f"`{resume_command(record)}`, which asks each ledger what it holds and publishes the rest"
    if (failed := effects.failures(record, run, resume_command(record))) is not None:
        return failed
    return f"{run} published to some of its databases and failed on another", f"fix what its publishes record, then `{resume_command(record)}`"


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


def _check_ledgers(job: Job, settings: Settings, databases: Callable[[str], Database], *, publishing: bool) -> list[str]:
    """Every reason a run that publishes cannot write a ledger row: the ledger table of a database it publishes to, or
    whose ledger records a message or an archive, does not exist, which would otherwise be found only once the
    databases before it had committed."""
    if not publishing:
        return []
    names = {target.database for target in targets_of(job).values()}
    names |= {message.ledger for message in job.outputs.declared_of(Message).values()} | {declared.ledger for declared in job.outputs.declared_of(Lake).values()}
    reasons: list[str] = []
    for name in sorted(names):
        found = settings.databases.get(name)
        if found is None or found.write is None or (ledger := found.write.ledger) is None:
            continue  # its own reason
        with databases(name).reading() as reader:
            if missing := [table for table in core_ddl.ledger_tables(ledger, found.kind) if reader.describe(table) is None]:
                what = f"ledger table {ledger}" if missing == [ledger] else f"table {' or '.join(missing)} of its ledger"
                reasons.append(f"database {name!r} has no {what}, which a publish writes in its transaction: `basis ddl --ledger {ledger} --dialect {found.kind}` prints it")
    return reasons


def fresh(tables: Mapping[str, pa.Table]) -> dict[str, pd.DataFrame]:
    """A frame of each table, its own: a pandas frame changes in place, and an Arrow table never does, so a frame that
    job code changes changes no table, nor another frame of it."""
    return {name: frames.from_arrow(table) for name, table in tables.items()}


@dataclass(frozen=True)
class Transformed:
    """What a transform made: each table's changes, every reason not to write, and each effect's plan."""

    planned: dict[str, Planned]
    reasons: list[str]
    effects_planned: effects.Planned = field(default_factory=effects.Planned)


def transformed(job: Job, params: Params, tables: Mapping[str, pa.Table], interval: Interval | None, kept: Kept, *, past: expectations.History | None = None) -> Transformed:
    """Each table's changes, and every reason not to write them. The transform and the check are each given frames of
    their own of the inputs, so neither sees what the other changed in them. A transform or check that raises is a
    reason that names the line of job code that raised; the log holds its traceback."""
    inputs = job.inputs(fresh(tables), interval=interval, handles=kept.handles)
    try:
        with logs.timed(log, "transformed"):
            outputs = job.run_transform(inputs, params)
    except Exception as error:
        log.exception("the transform raised")
        return Transformed({}, [f"the transform raised {_headline(error)}{_at(error, job.directory)}"])
    planned = plan(outputs, interval)
    reasons = [reason for entry in planned.values() for reason in entry.reasons]
    made = effects.plan(outputs, interval)
    reasons.extend(made.reasons)
    reasons.extend(expectations.reasons(outputs, tables, past or {}))
    try:
        checked = job.inputs(fresh(tables), interval=interval, handles=kept.handles)
        reasons.extend(f"check: {reason}" for reason in job.run_check(checked, outputs, params))
    except Exception as error:
        log.exception("the job's check raised")
        reasons.append(f"the job's check raised {_headline(error)}{_at(error, job.directory)}")
    for reason in reasons:
        log.warning("not writing: %s", reason)
    return Transformed(planned, reasons, made)


def _headline(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _at(error: BaseException, directory: Path) -> str:
    """Where in the job's code an error surfaced, as `, at jobs/positions/job.py:14 in transform`."""
    ours = [frame for frame in traceback.extract_tb(error.__traceback__) if Path(frame.filename).resolve().is_relative_to(directory)]
    if not ours:
        return ""
    frame = ours[-1]
    return f", at {Path(frame.filename).resolve().relative_to(repo_root(directory)).as_posix()}:{frame.lineno} in {frame.name}"


@dataclass(frozen=True)
class Draft:
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
    publish: bool = False
    trace_id: str | None = None
    inputs: dict[str, InputRecord] = field(default_factory=dict)
    outputs: dict[str, snapshots.OutputRecord] = field(default_factory=dict)
    clocks: dict[str, list[datetime]] = field(default_factory=dict)
    ids: dict[str, list[UUID]] = field(default_factory=dict)
    effects: dict[str, dict[str, Model]] = field(default_factory=dict)
    """Each effect's plan, by the run record's field."""

    def with_effects(self, planned: Mapping[str, Mapping[str, Model]]) -> Draft:
        return replace(self, effects={key: dict(value) for key, value in planned.items()})

    def with_outputs(self, planned: Mapping[str, Planned], inputs: Mapping[str, InputRecord]) -> Draft:
        outputs = {name: snapshots.output_record(entry.target.database, entry.target.table, type(entry.target.write).__name__, entry.changes) for name, entry in planned.items()}
        return replace(self, inputs=dict(inputs), outputs=outputs)

    def with_made(self, kept: Kept) -> Draft:
        """The record, with each value the run's clocks and ids gave its transform."""
        return replace(self, clocks={name: list(values) for name, values in kept.clocks.items()}, ids={name: list(values) for name, values in kept.ids.items()})

    def done(
        self,
        status: Status,
        *,
        finished_at: datetime,
        reasons: list[str] | None = None,
        error: str | None = None,
        publishes: dict[str, PublishRecord] | None = None,
        made: Mapping[str, Mapping[str, Model]] | None = None,
    ) -> RunRecord:
        """The run's record; `made` is what became of each effect, by the run record's field."""
        return RunRecord.model_validate(dict(
            run_id=self.run_id, job=self.job, module=self.module, version=self.version, git_sha=self.git_sha, started_at=self.started_at,
            finished_at=finished_at, interval=self.interval, params_path=self.params_path, params_sha256=self.params_sha256, labels=self.labels,
            data=self.data, replay_of=self.replay_of, again=self.again, publish=self.publish, trace_id=self.trace_id, status=status, inputs=self.inputs, outputs=self.outputs,
            reasons=reasons or [], error=error, publishes=publishes or {}, clocks=self.clocks, ids=self.ids, **self.effects, **(made or {}),
        ))  # fmt: skip


def finish(record: RunRecord, root: Root | None, params_text: str, tables: Mapping[str, pa.Table], planned: Mapping[str, Planned], *, reports: Mapping[str, pd.DataFrame] | None = None) -> Result:
    """The run's result, its snapshot saved: its inputs, each table's changes and each workbook's rows, as an insert."""
    changes = {name: entry.changes for name, entry in planned.items() if entry.changes is not None} | {name: Changes(insert=frame) for name, frame in (reports or {}).items()}
    snapshot = snapshots.write(root, record, params_text, tables, changes, package=PACKAGE) if root is not None else None
    if snapshot is not None:
        log.info("saved the snapshot", extra={"snapshot": snapshot.path.location})
    return Result(record, snapshot, dict(tables), dict(planned))


def _summarized(result: Result) -> Result:
    log_summary(result.record)
    return result


def log_summary(record: RunRecord) -> None:
    """The run's one summary record: what a dashboard or a monitor reads."""
    said = {key: value for key, value in summary(record).items() if key not in {"run_id", "job"}}  # which every record of the run carries already
    log.info("run summary", extra={**said, "reasons": len(record.reasons)})


def summary(record: RunRecord) -> dict[str, object]:
    """What a run did, as `basis run` prints it on stdout and its summary record logs it: its id, which `replay` and
    `resume` take, its status, the rows each input and output held, why it wrote nothing, and each publish's status."""
    duration = (record.finished_at - record.started_at).total_seconds() * 1000 if record.finished_at is not None else None
    return {
        "run_id": record.run_id,
        "job": record.job,
        "status": record.status,
        "interval": record.interval,
        "labels": record.labels,
        "inputs": {name: entry.rows for name, entry in record.inputs.items()},
        "outputs": {name: entry.rows for name, entry in record.outputs.items()},
        "reasons": list(record.reasons),
        "error": record.error,
        "publishes": {name: entry.status for name, entry in record.publishes.items()},
        "run_ms": round(duration, 1) if duration is not None else None,
    }


def resume(
    job: Job,
    source: Snapshot,
    settings: Settings,
    *,
    again: bool = False,
    clock: Callable[[], datetime] = utc_now,
    environment: str | None = None,
    databases: Callable[[str], Database] | None = None,
    faked: Mapping[str, fakes.Fake] | None = None,
    open_folder: folders.Opener | None = None,
    open_lake: lake.Opener | None = None,
) -> RunRecord:
    """Finish publishing a run that committed to some of its databases and failed on another, or could not write a
    workbook or send a message, from its snapshot: told `again` as the run was, or as `again` says now. Workbooks and
    messages go only once every database has committed, and a message never twice: the ledger has each one sent.
    `databases`, `faked`, `open_folder` and `open_lake` are a test's."""
    record = source.record()
    logs.bind(run_id=record.run_id, job=job.name, service=job.name)
    with traces.trace("resume", service=job.name, settings=settings.traces, version=settings.git_sha, environment=environment, **{"basis.job": job.name, "basis.run_id": record.run_id}) as (tracer, _):
        if record.status == "published":
            log.info("already published")
            return record
        core_runs.check_resumable(record)
        databases = databases if databases is not None else core_runs.opener(settings)
        count = len({output.database for output in record.outputs.values()})
        final = record
        if record.reasons or record.error is not None or not effects.planned_any(record) or core_runs.status_of(record.publishes, count) != "published":
            final = core_runs.resume(source, record, name=job.name, targets=targets_of(job), databases=databases, version=VERSION, clock=clock, again=again)  # which refuses a run that never published
        if core_runs.status_of(final.publishes, count) == "published":
            given, opened = _checked(job, faked or {}), open_folder or folders.opener(settings)
            lakes = open_lake if open_lake is not None else lake.opener(settings)
            final = _finish_files(job, source, final, settings, again=again, clock=clock, databases=databases, faked=given, tracer=tracer, open_folder=opened, open_lake=lakes)
        log_summary(final)
        return final


def _finish_files(
    job: Job,
    source: Snapshot,
    record: RunRecord,
    settings: Settings,
    *,
    again: bool,
    clock: Callable[[], datetime],
    databases: Callable[[str], Database],
    faked: Mapping[str, fakes.Fake],
    tracer: Tracer,
    open_folder: folders.Opener,
    open_lake: lake.Opener,
) -> RunRecord:
    """`record`, once its databases have committed, with each effect it had not made made from its snapshot, in the
    order and with the stop a run's are (`effects.apply`), and saved."""
    if not effects.unfinished(record):
        return record
    told = record.again or again
    with mongo_opener(settings, faked, tracer) as open_mongo:
        reach = effects.Reach(record.run_id, job.name, record.parsed_interval(), told, clock, settings, databases, faked, tracer, open_folder, open_mongo, open_lake)
        done, complete = effects.apply(job.outputs, record, lambda name: source.dataset(f"outputs/{name}/insert"), reach)
    status: Status = "published" if complete else "partial"
    final = RunRecord.model_validate(record.model_dump() | done | {"status": status, "again": told})
    source.save_record(final)
    return final
