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
from basis.core import fakes, folders, frames, lake, logs, mongo, publish, sending, snapshots, traces
from basis.core import runs as core_runs
from basis.core.databases import Database
from basis.core.destination import TableSpec, check_table, check_values
from basis.core.interval import Interval
from basis.core.logs import utc_now
from basis.core.messages import Mail, SlackResource, digest
from basis.core.opened import SYSTEM_WAITS, Waits, client
from basis.core.publish import refused_overlap
from basis.core.runs import Regression
from basis.core.settings import Settings, SettingsError, Traces, publish_refusal, variable
from basis.core.snapshots import (
    ArchivedRecord,
    ArchiveRecord,
    DocumentsRecord,
    InputRecord,
    MessageRecord,
    PublishRecord,
    ReportRecord,
    RunRecord,
    SendRecord,
    Snapshot,
    Status,
    UpsertRecord,
    WrittenRecord,
)
from basis.core.storage import Root
from basis.core.tables import Changes
from basis.job import handles
from basis.job.changes import Planned, plan
from basis.job.diff import OperationDiff, diff, diff_frames
from basis.job.handles import Kept
from basis.job.inputs import Clock, Fetch, Mongo
from basis.job.job import Job, JobError, repo_root
from basis.job.loader import MongoOpener, Reach, load
from basis.job.outputs import Email, Slack, Target, report_value
from basis.job.outputs import Outputs as OutputsOf
from basis.job.params import Params
from basis.job.rows import SchemaError, conform
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
    record = _Record(
        run_id=run_id, job=job.name, module=job.module, version=VERSION, git_sha=request.git_sha, started_at=started,
        interval=str(request.interval) if request.interval is not None else None, params_path=str(request.params),
        params_sha256=snapshots.params_digest(params_text), labels=dict(request.labels), data=str(request.data) if request.data is not None else None,
        again=request.again, publish=request.publish,
    )  # fmt: skip
    targets = targets_of(job)
    try:
        specs, reasons = _check_destinations(targets, settings, databases, publishing=request.publish)
        reasons += _check_ledgers(job, settings, databases, publishing=request.publish)
        reasons += _check_messages(job, settings, this.faked, publishing=request.publish) + _check_reports(job, settings, this.open_folder, publishing=request.publish)
        reasons += _check_upserts(job, settings, this.faked, this.open_mongo, publishing=request.publish) + _check_lakes(job, settings, this.open_lake, publishing=request.publish)
    except Exception as error:
        log.exception("could not check the destinations")
        return _summarized(_finish(record.done("failed", error=_headline(error), finished_at=clock()), snapshot_root, params_text, {}, {}))
    if reasons:
        return _summarized(_finish(record.done("failed", reasons=reasons, finished_at=clock()), snapshot_root, params_text, {}, {}))
    try:
        reach = _reach(settings, this.faked)
        declared = job.inputs.declared()
        loaded, inputs = load(declared, request.interval, open_database=databases, reach=reach, tracer=this.tracer, data=request.data, open_folder=this.open_folder, open_mongo=this.open_mongo)
        tables = {name: frames.to_arrow(frame) for name, frame in loaded.items()}
    except Exception as error:
        log.exception("could not load the inputs")
        return _summarized(_finish(record.done("failed", error=_headline(error), finished_at=clock()), snapshot_root, params_text, {}, {}))
    try:
        kept = handles.live(job.inputs, this.faked, this.tracer)
    except Exception as error:  # a defect: the inputs' declarations are checked when their class is made
        log.exception("could not give the transform its clocks and ids")
        return _summarized(_finish(record.with_outputs({}, inputs).done("failed", error=_headline(error), finished_at=clock()), snapshot_root, params_text, tables, {}))
    made = _transform(job, params, tables, request.interval, kept)
    planned, reasons = made.planned, made.reasons
    for name, entry in planned.items():
        if (spec := specs.get(name)) is not None and entry.changes is not None:
            reasons.extend(f"{name}: {reason}" for reason in check_values(entry.target, spec, entry.changes))
    status: Status = "failed" if reasons else "publishing" if request.publish else "checked"  # saved before the first commit, so a run stopped while it publishes says so
    record = record.with_outputs(planned, inputs).with_made(kept).with_messages(made.messages).with_reports(made.reports).with_documents(made.documents).with_archives(made.archives)
    result = _finish(record.done(status, reasons=reasons, finished_at=clock()), snapshot_root, params_text, tables, planned, reports=made.frames)
    if reasons or not request.publish or result.snapshot is None:
        _summary(result.record)
        return result
    changes = {name: entry.changes for name, entry in planned.items() if entry.changes is not None}
    publication = publish.Publication(run_id, job.name, request.interval, VERSION, request.again, clock)
    publishes = publish.publish(publication, targets, changes, databases, checked=specs)
    status = core_runs.status_of(publishes, len({target.database for target in targets.values()}))
    after = _after_commit(job, made, request, this, settings=settings, databases=databases) if status == "published" else _AfterCommit()
    final = record.done(after.status or status, reasons=[], finished_at=clock(), publishes=publishes, sent=after.sent, written=after.written, upserted=after.upserted, archived=after.archived)
    snapshot = result.snapshot.save_record(final)
    _summary(final)
    return Result(final, snapshot, tables, planned)


@dataclass(frozen=True)
class _AfterCommit:
    """What a run made once its databases committed: each workbook, upsert, archive and message."""

    status: Status | None = None
    """`published`, or `partial` where one failed; None where nothing ran."""
    written: dict[str, WrittenRecord] = field(default_factory=dict)
    upserted: dict[str, UpsertRecord] = field(default_factory=dict)
    archived: dict[str, ArchivedRecord] = field(default_factory=dict)
    sent: dict[str, SendRecord] = field(default_factory=dict)


def _after_commit(job: Job, made: _Transformed, request: Request, this: _Run, *, settings: Settings, databases: Callable[[str], Database]) -> _AfterCommit:  # noqa: PLR0913 -- the run, and how it reaches each
    """Write each workbook, then make each upsert, then each archive, then send each message, each step only once
    every step before it has gone, as the module's run does."""
    status: Status = "published"
    written: dict[str, WrittenRecord] = {}
    if made.reports:
        written = write_reports(job, made.reports, made.frames, this.open_folder, this.clock)
        status = "partial" if any(entry.status == "failed" for entry in written.values()) else status
    upserted: dict[str, UpsertRecord] = {}
    if status == "published" and made.documents:
        upserted = upsert_all(job, made.documents, made.frames, this.open_mongo, this.clock)
        status = "partial" if any(entry.status == "failed" for entry in upserted.values()) else status
    archived: dict[str, ArchivedRecord] = {}
    if status == "published" and made.archives and request.interval is not None:
        archiving = lake.Archiving(this.run_id, job.name, request.interval, VERSION, request.again, this.clock)
        archived = archive_all(made.archives, made.frames, open_lake=this.open_lake, databases=databases, archiving=archiving)
        status = "partial" if any(entry.status == "failed" for entry in archived.values()) else status
    sent: dict[str, SendRecord] = {}
    if status == "published" and made.messages:
        delivery = sending.Delivery(this.run_id, job.name, request.interval, VERSION, request.again, this.clock)
        sent = send(job, made.messages, delivery, settings=settings, databases=databases, faked=this.faked, tracer=this.tracer)
        status = "partial" if any(entry.status == "failed" for entry in sent.values()) else status
    return _AfterCommit(status, written, upserted, archived, sent)


def _checked(job: Job, faked: Mapping[str, fakes.Fake]) -> dict[str, fakes.Fake]:
    """The fakes a test gives, each of a service a fetch of the job's reaches, or of a clock or ids its `Inputs`
    declare, by name, of its kind."""
    declared: dict[str, type[fakes.Fake]] = {dataset.http: fakes.Http for dataset in job.inputs.declared().values() if isinstance(dataset, Fetch)}
    declared |= {name: fakes.Clock if isinstance(given, Clock) else fakes.Ids for name, given in job.inputs.declared_resources().items()}
    declared |= {message.to: fakes.Slack if isinstance(message, Slack) else fakes.Email for message in job.outputs.declared_messages().values()}
    declared |= {dataset.mongo: fakes.Mongo for dataset in job.inputs.declared().values() if isinstance(dataset, Mongo)}
    declared |= {upsert.mongo: fakes.Mongo for upsert in job.outputs.declared_upserts().values()}
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
    if unsent := [f"{name}: {entry.error}" for name, entry in record.sent.items() if entry.status == "failed"]:
        return f"{run} published, and could not send {'; '.join(unsent)}", f"fix it, then `{resume_command(record)}`, which sends what it has not"
    if unarchived := [f"{name}: {entry.error}" for name, entry in record.archived.items() if entry.status == "failed"]:
        return f"{run} published, and could not archive {'; '.join(unarchived)}", f"fix it, then `{resume_command(record)}`, which archives what it has not"
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
    names |= {message.ledger for message in job.outputs.declared_messages().values()} | {declared.ledger for declared in job.outputs.declared_lakes().values()}
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
class _Transformed:
    """What a transform made: each table's changes, every reason not to write, each message and each workbook."""

    planned: dict[str, Planned]
    reasons: list[str]
    messages: dict[str, MessageRecord] = field(default_factory=dict)
    reports: dict[str, ReportRecord] = field(default_factory=dict)
    frames: dict[str, pd.DataFrame] = field(default_factory=dict)
    """Each workbook's, each upsert's and each archive's rows, conformed to its row."""
    documents: dict[str, DocumentsRecord] = field(default_factory=dict)
    archives: dict[str, ArchiveRecord] = field(default_factory=dict)


def _transform(job: Job, params: Params, tables: Mapping[str, pa.Table], interval: Interval | None, kept: Kept) -> _Transformed:
    """Each table's changes, and every reason not to write them. The transform and the check are each given frames of
    their own of the inputs, so neither sees what the other changed in them. A transform or check that raises is a
    reason that names the line of job code that raised; the log holds its traceback."""
    inputs = job.inputs(fresh(tables), interval=interval, handles=kept.handles)
    try:
        with logs.timed(log, "transformed"):
            outputs = job.run_transform(inputs, params)
    except Exception as error:
        log.exception("the transform raised")
        return _Transformed({}, [f"the transform raised {_headline(error)}{_at(error, job.directory)}"])
    planned = plan(outputs, interval)
    reasons = [reason for entry in planned.values() for reason in entry.reasons]
    reports, report_frames, refused = planned_reports(job, outputs, interval)
    documents, document_frames, unconformed = planned_upserts(job, outputs)
    archives, archive_frames, unarchivable = planned_archives(job, outputs, interval)
    reasons.extend(refused + unconformed + unarchivable)
    try:
        checked = job.inputs(fresh(tables), interval=interval, handles=kept.handles)
        reasons.extend(f"check: {reason}" for reason in job.run_check(checked, outputs, params))
    except Exception as error:
        log.exception("the job's check raised")
        reasons.append(f"the job's check raised {_headline(error)}{_at(error, job.directory)}")
    for reason in reasons:
        log.warning("not writing: %s", reason)
    return _Transformed(planned, reasons, planned_messages(job, outputs), reports, report_frames | document_frames | archive_frames, documents, archives)


def planned_upserts(job: Job, outputs: OutputsOf) -> tuple[dict[str, DocumentsRecord], dict[str, pd.DataFrame], list[str]]:
    """Each Mongo upsert the transform gave, by its output's name, its rows conformed to its row, and every reason it
    cannot be made."""
    records: dict[str, DocumentsRecord] = {}
    frames_of: dict[str, pd.DataFrame] = {}
    reasons: list[str] = []
    for name, upsert in job.outputs.declared_upserts().items():
        try:
            frame = conform(report_value(outputs, name), upsert.row, what=f"{name}'s rows", naive="reject")
        except SchemaError as error:
            reasons.append(f"{name}: {error}")
            continue
        if (repeated := int(frame.duplicated(subset=list(upsert.key)).sum())) > 0:
            reasons.append(f"{name}: {repeated} rows repeat a key of {list(upsert.key)}, so an upsert would set one document twice: make each key once")
            continue
        records[name] = DocumentsRecord(mongo=upsert.mongo, collection=upsert.collection, key=tuple(upsert.field(column) for column in upsert.key), rows=len(frame))
        frames_of[name] = frame
    return records, frames_of, reasons


def upsert_all(job: Job, documents: Mapping[str, DocumentsRecord], frames_of: Mapping[str, pd.DataFrame], open_mongo: MongoOpener, clock: Callable[[], datetime]) -> dict[str, UpsertRecord]:
    """Upsert each output's documents, in name order; one that fails is recorded as failed, and the rest are still
    upserted. An upsert is safe to make again, so a resume makes a failed one again whole."""
    declared = job.outputs.declared_upserts()
    done: dict[str, UpsertRecord] = {}
    for name in sorted(documents):
        record = documents[name]
        try:
            store, _ = open_mongo(record.mongo)
            counts = store.upsert(record.collection, record.key, mongo.documents_of(frames_of[name], declared[name].row.columns, declared[name].fields))
        except Exception as error:
            log.exception("could not upsert %s", name, extra={"output": name})
            done[name] = UpsertRecord(status="failed", at=clock(), error=_headline(error))
            continue
        log.info("upserted %s into %s.%s", name, record.mongo, record.collection, extra={"output": name, "matched": counts.matched, "modified": counts.modified, "upserted": counts.upserted})
        done[name] = UpsertRecord(status="upserted", at=clock(), matched=counts.matched, modified=counts.modified, upserted=counts.upserted)
    return done


def _check_upserts(job: Job, settings: Settings, faked: Mapping[str, fakes.Fake], open_mongo: MongoOpener, *, publishing: bool) -> list[str]:
    """Every reason a run that publishes cannot upsert into a Mongo database the job declares: it is not configured,
    and has no fake; or its collection names a field otherwise than the upsert does, or has no unique index on the key
    (`mongo.refusals`)."""
    if not publishing:
        return []
    reasons: list[str] = []
    for name, upsert in job.outputs.declared_upserts().items():
        if upsert.mongo not in settings.mongo and upsert.mongo not in faked:
            reasons.append(f"{name}: the Mongo database {upsert.mongo} is not configured, and the run upserts into it: set BASIS_MONGO__{upsert.mongo.upper()}__*, as .env.example shows")
            continue
        store, _ = open_mongo(upsert.mongo)
        fields = [upsert.field(column.name) for column in upsert.row.columns]
        reasons.extend(f"{name}: {reason}" for reason in mongo.refusals(store, upsert.collection, [upsert.field(column) for column in upsert.key], fields))
    return reasons


def planned_archives(job: Job, outputs: OutputsOf, interval: Interval | None) -> tuple[dict[str, ArchiveRecord], dict[str, pd.DataFrame], list[str]]:
    """Each lake archive the transform gave, by its output's name, its rows conformed to its row, and every reason it
    cannot be archived: an archive is its run's interval's partition, so a run without one archives nothing."""
    records: dict[str, ArchiveRecord] = {}
    frames_of: dict[str, pd.DataFrame] = {}
    reasons: list[str] = []
    for name, declared in job.outputs.declared_lakes().items():
        try:
            frame = conform(report_value(outputs, name), declared.row, what=f"{name}'s rows", naive="reject")
        except SchemaError as error:
            reasons.append(f"{name}: {error}")
            continue
        if interval is None:
            reasons.append(f"{name}: an archive is its run's interval's partition of {declared.table}, and this run has none: give it one")
            continue
        try:
            path = lake.partition(declared.partition, interval)
        except lake.LakeError as error:
            reasons.append(f"{name}: {error}")
            continue
        frames_of[name] = frame
        records[name] = ArchiveRecord(lake=declared.lake, table=declared.table, partition=path, ledger=declared.ledger, rows=len(frame))
    return records, frames_of, reasons


def archive_all(
    archives: Mapping[str, ArchiveRecord], frames_of: Mapping[str, pd.DataFrame], *, open_lake: lake.Opener, databases: Callable[[str], Database], archiving: lake.Archiving
) -> dict[str, ArchivedRecord]:
    """Archive each output the run planned to, in name order, each recorded in its ledger's database; one that fails
    is recorded as failed, and the rest are still archived."""
    done: dict[str, ArchivedRecord] = {}
    for name in sorted(archives):
        record = archives[name]
        try:
            rows = frames.to_arrow(frames_of[name])
            done[name] = lake.archive(archiving, name, rows, open_lake(record.lake), databases(record.ledger), table=record.table, path=record.partition)
        except Exception as error:
            log.exception("could not archive %s", name, extra={"output": name})
            done[name] = ArchivedRecord(status="failed", at=archiving.clock(), error=_headline(error))
    return done


def _check_lakes(job: Job, settings: Settings, open_lake: lake.Opener, *, publishing: bool) -> list[str]:
    """Every reason a run that publishes cannot archive to a lake the job declares: the lake is not configured, or the
    database whose ledger records it cannot be published to."""
    if not publishing:
        return []
    reasons: list[str] = []
    for name, declared in job.outputs.declared_lakes().items():
        try:
            open_lake(declared.lake)
        except SettingsError as error:
            reasons.append(f"{name}: {error}")
        if (database := settings.databases.get(declared.ledger)) is None:
            reasons.append(f"{name}: database {declared.ledger!r}, whose ledger records it, is not configured: set BASIS_DATABASES__{declared.ledger.upper()}__*")
        elif (refused := publish_refusal(declared.ledger, database)) is not None:
            reasons.append(f"{name}: database {declared.ledger!r}, whose ledger records it, {refused[0]}: {refused[1]}")
    return reasons


def planned_reports(job: Job, outputs: OutputsOf, interval: Interval | None) -> tuple[dict[str, ReportRecord], dict[str, pd.DataFrame], list[str]]:
    """Each workbook the transform gave, by its output's name, its rows conformed to its row, and every reason it
    cannot be written."""
    records: dict[str, ReportRecord] = {}
    frames_of: dict[str, pd.DataFrame] = {}
    reasons: list[str] = []
    for name, report in job.outputs.declared_reports().items():
        try:
            frame = conform(report_value(outputs, name), report.row, what=f"{name}'s rows", naive="reject")
            location = report.location(interval)
        except (SchemaError, ValueError) as error:
            reasons.append(f"{name}: {error}")
            continue
        records[name] = ReportRecord(folder=report.folder, path=location, sheet=report.sheet, rows=len(frame))
        frames_of[name] = frame
    return records, frames_of, reasons


def write_reports(job: Job, reports: Mapping[str, ReportRecord], frames_of: Mapping[str, pd.DataFrame], open_folder: folders.Opener, clock: Callable[[], datetime]) -> dict[str, WrittenRecord]:
    """Write each workbook the run planned, in name order, each whole or not at all; one that fails is recorded as
    failed, and the rest are still written."""
    declared = job.outputs.declared_reports()
    written: dict[str, WrittenRecord] = {}
    for name in sorted(reports):
        record = reports[name]
        zones = {column.name: column.zone for column in declared[name].row.columns if column.zone is not None}
        try:
            done = open_folder(record.folder).write_workbook(record.path, {record.sheet: frames_of[name]}, zones=zones)
        except Exception as error:
            log.exception("could not write %s", name, extra={"output": name})
            written[name] = WrittenRecord(status="failed", at=clock(), error=_headline(error))
            continue
        log.info("wrote %s to %s: %s", name, record.folder, record.path, extra={"output": name, "rows": record.rows, "sha256": done.sha256})
        written[name] = WrittenRecord(status="written", at=clock(), size=done.size, sha256=done.sha256)
    return written


def _check_reports(job: Job, settings: Settings, open_folder: folders.Opener, *, publishing: bool) -> list[str]:
    """Every reason a run that publishes cannot write a workbook the job declares: its folder is not configured."""
    if not publishing:
        return []
    reasons: list[str] = []
    for name, report in job.outputs.declared_reports().items():
        try:
            open_folder(report.folder)
        except SettingsError as error:
            reasons.append(f"{name}: {error}")
    del settings
    return reasons


def planned_messages(job: Job, outputs: OutputsOf) -> dict[str, MessageRecord]:
    """Each message the transform gave, by its output's name, with its text's hash; a message it gave None is none."""
    found: dict[str, MessageRecord] = {}
    for name, message in job.outputs.declared_messages().items():
        if (value := message.checked(outputs.values[name])) is None:  # noqa: PD011 -- the outputs by name, no frame's values
            continue
        subject, text = (None, value) if isinstance(value, str) else (value.subject, value.text)
        found[name] = MessageRecord(destination=message.destination, to=message.to, ledger=message.ledger, subject=subject, text=text, sha256=digest(text))
    return found


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
    publish: bool = False
    inputs: dict[str, InputRecord] = field(default_factory=dict)
    outputs: dict[str, snapshots.OutputRecord] = field(default_factory=dict)
    clocks: dict[str, list[datetime]] = field(default_factory=dict)
    ids: dict[str, list[UUID]] = field(default_factory=dict)
    messages: dict[str, MessageRecord] = field(default_factory=dict)
    reports: dict[str, ReportRecord] = field(default_factory=dict)
    documents: dict[str, DocumentsRecord] = field(default_factory=dict)
    archives: dict[str, ArchiveRecord] = field(default_factory=dict)

    def with_archives(self, archives: Mapping[str, ArchiveRecord]) -> _Record:
        return replace(self, archives=dict(archives))

    def with_documents(self, documents: Mapping[str, DocumentsRecord]) -> _Record:
        return replace(self, documents=dict(documents))

    def with_messages(self, messages: Mapping[str, MessageRecord]) -> _Record:
        return replace(self, messages=dict(messages))

    def with_reports(self, reports: Mapping[str, ReportRecord]) -> _Record:
        return replace(self, reports=dict(reports))

    def with_outputs(self, planned: Mapping[str, Planned], inputs: Mapping[str, InputRecord]) -> _Record:
        outputs = {name: snapshots.output_record(entry.target.database, entry.target.table, type(entry.target.write).__name__, entry.changes) for name, entry in planned.items()}
        return replace(self, inputs=dict(inputs), outputs=outputs)

    def with_made(self, kept: Kept) -> _Record:
        """The record, with each value the run's clocks and ids gave its transform."""
        return replace(self, clocks={name: list(values) for name, values in kept.clocks.items()}, ids={name: list(values) for name, values in kept.ids.items()})

    def done(  # noqa: PLR0913 -- how the run ended, by keyword
        self,
        status: Status,
        *,
        finished_at: datetime,
        reasons: list[str] | None = None,
        error: str | None = None,
        publishes: dict[str, PublishRecord] | None = None,
        sent: dict[str, SendRecord] | None = None,
        written: dict[str, WrittenRecord] | None = None,
        upserted: dict[str, UpsertRecord] | None = None,
        archived: dict[str, ArchivedRecord] | None = None,
    ) -> RunRecord:
        return RunRecord(
            run_id=self.run_id, job=self.job, module=self.module, version=self.version, git_sha=self.git_sha, started_at=self.started_at,
            finished_at=finished_at, interval=self.interval, params_path=self.params_path, params_sha256=self.params_sha256, labels=self.labels,
            data=self.data, replay_of=self.replay_of, again=self.again, publish=self.publish, status=status, inputs=self.inputs, outputs=self.outputs, reasons=reasons or [], error=error,
            publishes=publishes or {}, messages=self.messages, sent=sent or {}, reports=self.reports, written=written or {},
            documents=self.documents, upserted=upserted or {}, archives=self.archives, archived=archived or {}, clocks=self.clocks, ids=self.ids,
        )  # fmt: skip


def _finish(  # noqa: PLR0913 -- what the run saves, by keyword
    record: RunRecord, root: Root | None, params_text: str, tables: Mapping[str, pa.Table], planned: Mapping[str, Planned], *, reports: Mapping[str, pd.DataFrame] | None = None
) -> Result:
    """The run's result, its snapshot saved: its inputs, each table's changes and each workbook's rows, as an insert."""
    changes = {name: entry.changes for name, entry in planned.items() if entry.changes is not None} | {name: Changes(insert=frame) for name, frame in (reports or {}).items()}
    snapshot = snapshots.write(root, record, params_text, tables, changes, package=PACKAGE) if root is not None else None
    if snapshot is not None:
        log.info("saved the snapshot", extra={"snapshot": snapshot.path.location})
    return Result(record, snapshot, dict(tables), dict(planned))


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
    environment: str | None = None,
) -> tuple[Result, list[OperationDiff]]:
    """Run a saved run's transform again on its inputs, with the code of the day and, if given, other params, and how
    its changes differ from the run's: one trace, exported where `tracing` says, or on a test's `provider`. Reads no
    database and writes none, and gives the transform the times and ids the run kept."""
    with (
        logs.fields(job=job.name, service=job.name, replay_of=source.run_id),  # the replay's own, whatever an earlier run in the process bound, until it ends
        traces.trace("replay", service=job.name, settings=tracing, version=None, environment=environment, given=provider, **{"basis.job": job.name, "basis.replay_of": source.run_id}) as (tracer, _),
    ):
        return _replay(job, source, params=params, snapshot_root=snapshot_root, clock=clock, tracer=tracer)


def _replay(job: Job, source: Snapshot, *, params: Path | None, snapshot_root: Root | None, clock: Callable[[], datetime], tracer: Tracer) -> tuple[Result, list[OperationDiff]]:  # noqa: PLR0913 -- replay's, by keyword
    with core_runs.replaying(source, name=job.name, module=job.module, params=params, parse=job.params.model_validate_json, reads=job.inputs.declared()) as opened:
        return _replayed(job, source, opened, snapshot_root=snapshot_root, clock=clock, tracer=tracer)


def _replayed(job: Job, source: Snapshot, opened: core_runs.Replay[Params], *, snapshot_root: Root | None, clock: Callable[[], datetime], tracer: Tracer) -> tuple[Result, list[OperationDiff]]:  # noqa: PLR0913 -- replay's, by keyword
    original = opened.original
    interval = original.parsed_interval()
    record = _Record(
        run_id=opened.run_id, job=job.name, module=job.module, version=VERSION, git_sha=None, started_at=clock(), interval=original.interval,
        params_path=opened.params_path, params_sha256=snapshots.params_digest(opened.params_text),
        labels={"replay_of": original.run_id}, data=None, replay_of=original.run_id,
        inputs={name: InputRecord(source=f"snapshot: {original.run_id}", rows=table.num_rows, read_ms=0.0) for name, table in opened.saved.items()},
    )  # fmt: skip
    tables = {name: opened.saved[name] for name in job.inputs.declared()}
    kept = handles.replayed(job.inputs, original, tracer)
    made = _transform(job, opened.params, tables, interval, kept)
    planned, reasons = made.planned, made.reasons
    record = record.with_outputs(planned, record.inputs).with_made(kept).with_messages(made.messages).with_reports(made.reports)
    record = record.with_documents(made.documents).with_archives(made.archives)
    result = _finish(record.done("failed" if reasons else "checked", reasons=reasons, finished_at=clock()), snapshot_root, opened.params_text, tables, planned, reports=made.frames)
    return result, _differences(job, source, original, made, result)


def _differences(job: Job, source: Snapshot, original: RunRecord, made: _Transformed, result: Result) -> list[OperationDiff]:
    """How a replay's outputs differ from its run's: each table's changes, the rows of each workbook, upsert and
    archive, and each message's text."""
    before = source.changes()
    targets = targets_of(job)
    diffs = [found for name in sorted(set(before) | set(result.planned)) if name in targets for found in diff(name, targets[name], before.get(name), result.changes(name))]
    saved = {*original.reports, *original.documents, *original.archives}  # each saved as its rows, as resume reads them
    for name in sorted(saved | set(made.frames)):
        old = source.dataset(f"outputs/{name}/insert") if name in saved else None
        new = made.frames.get(name)
        if not (found := diff_frames(name, "rows", old, new, None)).same:
            diffs.append(found)
    for name in sorted(set(original.messages) | set(made.messages)):
        old_message, new_message = original.messages.get(name), made.messages.get(name)
        if old_message is None or new_message is None:
            diffs.append(OperationDiff(name, "message", added=int(old_message is None), removed=int(new_message is None), changed=0))
        elif (old_message.sha256, old_message.subject) != (new_message.sha256, new_message.subject):
            diffs.append(OperationDiff(name, "message", added=0, removed=0, changed=1))
    return diffs


def resume(  # noqa: PLR0913 -- resume's, by keyword
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
        if record.reasons or record.error is not None or not (record.messages or record.reports or record.documents or record.archives) or core_runs.status_of(record.publishes, count) != "published":
            final = core_runs.resume(source, record, name=job.name, targets=targets_of(job), databases=databases, version=VERSION, clock=clock, again=again)  # which refuses a run that never published
        if core_runs.status_of(final.publishes, count) == "published":
            given, opened = _checked(job, faked or {}), open_folder or folders.opener(settings)
            lakes = open_lake if open_lake is not None else lake.opener(settings)
            final = _finish_files(job, source, final, settings, again=again, clock=clock, databases=databases, faked=given, tracer=tracer, open_folder=opened, open_lake=lakes)
        _summary(final)
        return final


def _finish_files(  # noqa: PLR0913 -- the run, and how it reaches what it writes, by keyword
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
    """`record`, once its databases have committed, with each workbook, upsert and archive it had not made made from
    its snapshot, and then, once every one of them is, each message it had not sent sent, and saved."""
    unwritten = {name: entry for name, entry in record.reports.items() if (done := record.written.get(name)) is None or done.status == "failed"}
    unupserted = {name: entry for name, entry in record.documents.items() if (done := record.upserted.get(name)) is None or done.status == "failed"}
    unarchived = {name: entry for name, entry in record.archives.items() if (done := record.archived.get(name)) is None or done.status == "failed"}
    unsent = {name: message for name, message in record.messages.items() if (done := record.sent.get(name)) is None or done.status == "failed"}
    if not unwritten and not unsent and not unupserted and not unarchived:
        return record
    written = record.written | write_reports(job, unwritten, {name: source.dataset(f"outputs/{name}/insert") for name in unwritten}, open_folder, clock)
    saved_rows = {name: source.dataset(f"outputs/{name}/insert") for name in unupserted}
    with mongo_opener(settings, faked, tracer) as open_mongo:
        upserted = record.upserted | upsert_all(job, unupserted, saved_rows, open_mongo, clock)
    told = record.again or again
    archived = record.archived
    if unarchived and (interval := record.parsed_interval()) is not None:
        archiving = lake.Archiving(record.run_id, job.name, interval, VERSION, told, clock)
        archived = archived | archive_all(unarchived, {name: source.dataset(f"outputs/{name}/insert") for name in unarchived}, open_lake=open_lake, databases=databases, archiving=archiving)
    sent = record.sent
    finished = (
        all(entry.status == "written" for entry in written.values()) and all(entry.status == "upserted" for entry in upserted.values()) and all(entry.status != "failed" for entry in archived.values())
    )
    if unsent and finished:
        delivery = sending.Delivery(record.run_id, job.name, record.parsed_interval(), VERSION, told, clock)
        sent = sent | send(job, unsent, delivery, settings=settings, databases=databases, faked=faked, tracer=tracer)
    failed = any(entry.status == "failed" for entry in [*written.values(), *upserted.values(), *archived.values(), *sent.values()]) or len(sent) < len(record.messages)
    status: Status = "partial" if failed else "published"
    final = RunRecord.model_validate(record.model_dump() | {"written": written, "upserted": upserted, "archived": archived, "sent": sent, "status": status, "again": told})
    source.save_record(final)
    return final


def send(  # noqa: PLR0913 -- what it sends and how it reaches them, by keyword
    job: Job, planned: Mapping[str, MessageRecord], delivery: sending.Delivery, *, settings: Settings, databases: Callable[[str], Database], faked: Mapping[str, fakes.Fake], tracer: Tracer
) -> dict[str, SendRecord]:
    """Send each message the run planned, in name order, each recorded in its ledger's database; one that fails is
    recorded as failed, and the rest are still sent."""
    declared = job.outputs.declared_messages()
    sent: dict[str, SendRecord] = {}
    for name in sorted(planned):
        message = planned[name]
        try:
            sender = _sender(declared[name], message, settings, faked, tracer)
            sent[name] = sending.deliver(delivery, name, message, sender, databases(message.ledger))
        except Exception as error:
            log.exception("could not send %s", name, extra={"output": name})
            sent[name] = SendRecord(status="failed", at=delivery.clock(), error=_headline(error))
    return sent


def _sender(declared: Slack | Email, message: MessageRecord, settings: Settings, faked: Mapping[str, fakes.Fake], tracer: Tracer) -> Callable[[], None]:
    """What sends `message`: the channel's poster, or the people's mailer, as the settings configure them, or its fake."""
    fake = faked.get(message.to)
    if isinstance(resource := declared.resource, SlackResource):
        poster = sending.poster(resource, settings, tracer, fake=fake if isinstance(fake, fakes.Slack) else None)
        return lambda: poster.post(message.text)
    mailer = sending.mailer(resource, settings, tracer, fake=fake if isinstance(fake, fakes.Email) else None)
    mail = Mail(subject=message.subject or "", text=message.text)
    return lambda: mailer.send(mail)


def _check_messages(job: Job, settings: Settings, faked: Mapping[str, fakes.Fake], *, publishing: bool) -> list[str]:
    """Every reason a run that publishes cannot send a message the job declares: its ledger's database cannot be
    published to, or its channel or email is not configured and has no fake."""
    if not publishing:
        return []
    reasons: list[str] = []
    for name, message in job.outputs.declared_messages().items():
        if (database := settings.databases.get(message.ledger)) is None:
            reasons.append(f"{name}: database {message.ledger!r}, whose ledger records it, is not configured: set BASIS_DATABASES__{message.ledger.upper()}__*")
        elif (refused := publish_refusal(message.ledger, database)) is not None:
            reasons.append(f"{name}: database {message.ledger!r}, whose ledger records it, {refused[0]}: {refused[1]}")
        configured = settings.slack if isinstance(message, Slack) else settings.email
        if message.to not in configured and message.to not in faked:
            setting = variable(("slack", message.to, "webhook")) if isinstance(message, Slack) else variable(("email", message.to, "host")).removesuffix("__HOST") + "__*"
            reasons.append(f"{name}: the {message.destination} {message.to!r} it sends to is not configured: set {setting}, as .env.example shows")
    return reasons


def regress(job: Job, root: Root, *, last: int, labels: Mapping[str, str]) -> list[Regression[OperationDiff]]:
    """Replay a job's last `last` published runs whose labels include `labels`, with the code of the day, and how each
    one's changes differ. Writes nothing."""

    def replayed(snapshot: Snapshot) -> tuple[RunRecord, list[OperationDiff]]:
        result, diffs = replay(job, snapshot)
        return result.record, diffs

    return core_runs.regress(root, job.name, package=PACKAGE, last=last, labels=labels, replay=replayed)
