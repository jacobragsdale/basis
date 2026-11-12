"""What a run makes of each output that is not a table, once every database has committed: a workbook, an upsert, an
archive, a copy or a message. Each kind is one `Kind`, which checks that a run that publishes can make it, plans it from what
the transform gave, and makes what it planned, and `KINDS` holds them in the order a run makes them. `apply` is the
one loop that `run` and `resume` share: each kind in that order, each output of it in name order, and a kind only
once every kind before it was made whole, so a failure stops the kinds after it, and a resume goes on from there.

A run's record keeps each kind's plan and what became of it under the record's own names for it (`reports` and
`written`, `documents` and `upserted`, `archives` and `archived`, `copies` and `copied`, `messages` and `sent`), which this module reads and
writes through each kind's `planned` and `done`."""

import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, override

import pandas as pd
import pyarrow as pa
from opentelemetry.trace import Tracer

from basis.core import fakes, folders, frames, lake, mongo, sending
from basis.core.databases import Database
from basis.core.interval import Interval
from basis.core.messages import Mail, SlackResource, digest
from basis.core.model import Model
from basis.core.settings import Settings, SettingsError, publish_refusal, variable
from basis.core.snapshots import ArchivedRecord, ArchiveRecord, CopyRecord, DocumentsRecord, MessageRecord, ReportRecord, RunRecord, SendRecord, UpsertRecord, WrittenRecord
from basis.job.inputs import Query
from basis.job.loader import MongoOpener
from basis.job.outputs import Effect, ExcelReport, Lake, LakeCopy, Message, MongoUpsert, Outputs, Slack
from basis.job.rows import SchemaError, conform
from basis.job.version import VERSION

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Reach:
    """The run whose effects are made, and how it reaches each: the settings, its databases, the fakes a test gives,
    its trace, and its folders, Mongo databases and lakes."""

    run_id: str
    job: str
    interval: Interval | None
    again: bool
    clock: Callable[[], datetime]
    settings: Settings
    databases: Callable[[str], Database]
    faked: Mapping[str, fakes.Fake]
    tracer: Tracer
    open_folder: folders.Opener
    open_mongo: MongoOpener
    open_lake: lake.Opener


type Rows = Callable[[str], pd.DataFrame]
"""The rows a run planned for an output, by its name: the transform's, conformed, or as its snapshot saved them."""


class Kind[D: Effect, P: Model, R: Model]:
    """One kind of effect: its declaration `D`, the record of its plan `P` and of what became of it `R`."""

    declaration: ClassVar[type[Effect]]
    planned: ClassVar[str]
    """The run record's field of each output's plan."""
    done: ClassVar[str]
    """The run record's field of what became of each."""
    failure: ClassVar[str]
    """What a run could not do, as `could not upsert summaries`."""
    finishes: ClassVar[str]
    """What a resume does of what the run could not, as `upserts`."""
    rows: ClassVar[bool] = True
    """Whether the snapshot saves each output's rows, as `outputs/NAME/insert`."""

    def declared(self, outputs: type[Outputs]) -> dict[str, D]:
        found: dict[str, D] = outputs.declared_of(self.declaration)  # ty: ignore[invalid-assignment] -- each kind's declaration is D's class
        return found

    def fakes_for(self, declared: D) -> dict[str, type[fakes.Fake]]:
        """The fakes a test may give for what `declared` reaches, by name."""
        del declared
        return {}

    def check(self, name: str, declared: D, reach: Reach) -> list[str]:
        """Every reason a run that publishes cannot make `declared`, found before it reads anything."""
        raise NotImplementedError

    def plan(self, name: str, declared: D, value: object, interval: Interval | None) -> tuple[P, pd.DataFrame | None] | str:
        """What the run will make of the transform's `value`, and its rows conformed, or the reason it cannot."""
        raise NotImplementedError

    def make(self, planned: Mapping[str, P], declared: Mapping[str, D], rows: Rows, reach: Reach) -> dict[str, R]:
        """Make each output planned, in name order; one that fails is recorded as failed, and the rest are still made."""
        raise NotImplementedError

    def failed(self, done: R) -> bool:
        return bool(getattr(done, "status", None) == "failed")

    def of(self, record: RunRecord) -> tuple[dict[str, P], dict[str, R]]:
        """`record`'s plan of this kind, and what became of it."""
        return dict(getattr(record, self.planned)), dict(getattr(record, self.done))


def _headline(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _ledger_refusal(name: str, ledger: str, settings: Settings) -> list[str]:
    """Why a run cannot record `name` in the ledger of the database `ledger`, if it cannot."""
    if (database := settings.databases.get(ledger)) is None:
        return [f"{name}: database {ledger!r}, whose ledger records it, is not configured: set BASIS_DATABASES__{ledger.upper()}__*"]
    if (refused := publish_refusal(ledger, database)) is not None:
        return [f"{name}: database {ledger!r}, whose ledger records it, {refused[0]}: {refused[1]}"]
    return []


class Reports(Kind[ExcelReport, ReportRecord, WrittenRecord]):
    declaration = ExcelReport
    planned, done, failure, finishes = "reports", "written", "write the workbook", "writes"

    @override
    def check(self, name: str, declared: ExcelReport, reach: Reach) -> list[str]:
        try:
            reach.open_folder(declared.folder)
        except SettingsError as error:
            return [f"{name}: {error}"]
        return []

    @override
    def plan(self, name: str, declared: ExcelReport, value: object, interval: Interval | None) -> tuple[ReportRecord, pd.DataFrame] | str:
        try:
            frame = conform(declared.checked(value), declared.row, what=f"{name}'s rows", naive="reject")
            location = declared.location(interval)
        except (SchemaError, ValueError) as error:
            return f"{name}: {error}"
        return ReportRecord(folder=declared.folder, path=location, sheet=declared.sheet, rows=len(frame)), frame

    @override
    def make(self, planned: Mapping[str, ReportRecord], declared: Mapping[str, ExcelReport], rows: Rows, reach: Reach) -> dict[str, WrittenRecord]:
        """Each workbook whole or not at all."""
        written: dict[str, WrittenRecord] = {}
        for name in sorted(planned):
            record = planned[name]
            zones = {column.name: column.zone for column in declared[name].row.columns if column.zone is not None}
            try:
                done = reach.open_folder(record.folder).write_workbook(record.path, {record.sheet: rows(name)}, zones=zones)
            except Exception as error:
                log.exception("could not write %s", name, extra={"output": name})
                written[name] = WrittenRecord(status="failed", at=reach.clock(), error=_headline(error))
                continue
            log.info("wrote %s to %s: %s", name, record.folder, record.path, extra={"output": name, "rows": record.rows, "sha256": done.sha256})
            written[name] = WrittenRecord(status="written", at=reach.clock(), size=done.size, sha256=done.sha256)
        return written


class Upserts(Kind[MongoUpsert, DocumentsRecord, UpsertRecord]):
    declaration = MongoUpsert
    planned, done, failure, finishes = "documents", "upserted", "upsert", "upserts"

    @override
    def fakes_for(self, declared: MongoUpsert) -> dict[str, type[fakes.Fake]]:
        return {declared.mongo: fakes.Mongo}

    @override
    def check(self, name: str, declared: MongoUpsert, reach: Reach) -> list[str]:
        """Its Mongo database is configured or faked, and its collection names no field otherwise than the upsert does,
        and has a unique index on the key (`mongo.refusals`)."""
        if declared.mongo not in reach.settings.mongo and declared.mongo not in reach.faked:
            return [f"{name}: the Mongo database {declared.mongo} is not configured, and the run upserts into it: set BASIS_MONGO__{declared.mongo.upper()}__*, as .env.example shows"]
        store, _ = reach.open_mongo(declared.mongo)
        fields = [declared.field(column.name) for column in declared.row.columns]
        return [f"{name}: {reason}" for reason in mongo.refusals(store, declared.collection, [declared.field(column) for column in declared.key], fields)]

    @override
    def plan(self, name: str, declared: MongoUpsert, value: object, interval: Interval | None) -> tuple[DocumentsRecord, pd.DataFrame] | str:
        try:
            frame = conform(declared.checked(value), declared.row, what=f"{name}'s rows", naive="reject")
        except SchemaError as error:
            return f"{name}: {error}"
        if (repeated := int(frame.duplicated(subset=list(declared.key)).sum())) > 0:
            return f"{name}: {repeated} rows repeat a key of {list(declared.key)}, so an upsert would set one document twice: make each key once"
        return DocumentsRecord(mongo=declared.mongo, collection=declared.collection, key=tuple(declared.field(column) for column in declared.key), rows=len(frame)), frame

    @override
    def make(self, planned: Mapping[str, DocumentsRecord], declared: Mapping[str, MongoUpsert], rows: Rows, reach: Reach) -> dict[str, UpsertRecord]:
        """An upsert is safe to make again, so a resume makes a failed one again whole."""
        done: dict[str, UpsertRecord] = {}
        for name in sorted(planned):
            record = planned[name]
            try:
                store, _ = reach.open_mongo(record.mongo)
                counts = store.upsert(record.collection, record.key, mongo.documents_of(rows(name), declared[name].row.columns, declared[name].fields))
            except Exception as error:
                log.exception("could not upsert %s", name, extra={"output": name})
                done[name] = UpsertRecord(status="failed", at=reach.clock(), error=_headline(error))
                continue
            log.info("upserted %s into %s.%s", name, record.mongo, record.collection, extra={"output": name, "matched": counts.matched, "modified": counts.modified, "upserted": counts.upserted})
            done[name] = UpsertRecord(status="upserted", at=reach.clock(), matched=counts.matched, modified=counts.modified, upserted=counts.upserted)
        return done


class Archives(Kind[Lake, ArchiveRecord, ArchivedRecord]):
    declaration = Lake
    planned, done, failure, finishes = "archives", "archived", "archive", "archives"

    @override
    def check(self, name: str, declared: Lake, reach: Reach) -> list[str]:
        """The lake is configured, and the database whose ledger records it can be published to."""
        reasons: list[str] = []
        try:
            reach.open_lake(declared.lake)
        except SettingsError as error:
            reasons.append(f"{name}: {error}")
        return reasons + _ledger_refusal(name, declared.ledger, reach.settings)

    @override
    def plan(self, name: str, declared: Lake, value: object, interval: Interval | None) -> tuple[ArchiveRecord, pd.DataFrame] | str:
        """An archive is its run's interval's partition, so a run without one archives nothing."""
        try:
            frame = conform(declared.checked(value), declared.row, what=f"{name}'s rows", naive="reject")
        except SchemaError as error:
            return f"{name}: {error}"
        if interval is None:
            return f"{name}: an archive is its run's interval's partition of {declared.table}, and this run has none: give it one"
        try:
            path = lake.partition(declared.partition, interval)
        except lake.LakeError as error:
            return f"{name}: {error}"
        return ArchiveRecord(lake=declared.lake, table=declared.table, partition=path, ledger=declared.ledger, rows=len(frame)), frame

    @override
    def make(self, planned: Mapping[str, ArchiveRecord], declared: Mapping[str, Lake], rows: Rows, reach: Reach) -> dict[str, ArchivedRecord]:
        """Each recorded in its ledger's database."""
        done: dict[str, ArchivedRecord] = {}
        for name in sorted(planned):
            record = planned[name]
            try:
                if reach.interval is None:  # a defect: an archive is planned only for a run with an interval
                    msg = "an archive is its run's interval's partition, and this run has none"
                    raise ValueError(msg)  # noqa: TRY301 -- recorded as the archive's failure, as any other is
                archiving = lake.Archiving(reach.run_id, reach.job, reach.interval, VERSION, reach.again, reach.clock)
                done[name] = lake.archive(archiving, name, frames.to_arrow(rows(name)), reach.open_lake(record.lake), reach.databases(record.ledger), table=record.table, path=record.partition)
            except Exception as error:
                log.exception("could not archive %s", name, extra={"output": name})
                done[name] = ArchivedRecord(status="failed", at=reach.clock(), error=_headline(error))
        return done


class Copies(Kind[LakeCopy, CopyRecord, ArchivedRecord]):
    declaration = LakeCopy
    planned, done, failure, finishes = "copies", "copied", "copy", "copies"
    rows = False

    @override
    def check(self, name: str, declared: LakeCopy, reach: Reach) -> list[str]:
        """The query's database and the lake are configured, and the ledger's database can be published to."""
        reasons: list[str] = []
        if declared.query.database not in reach.settings.databases:
            reasons.append(f"{name}: database {declared.query.database!r}, which it copies from, is not configured: set BASIS_DATABASES__{declared.query.database.upper()}__*")
        try:
            reach.open_lake(declared.lake)
        except SettingsError as error:
            reasons.append(f"{name}: {error}")
        return reasons + _ledger_refusal(name, declared.ledger, reach.settings)

    @override
    def plan(self, name: str, declared: LakeCopy, value: object, interval: Interval | None) -> tuple[CopyRecord, None] | str:
        """A copy is its run's interval's partition, so a run without one copies nothing."""
        del value  # a copy has none
        if interval is None:
            return f"{name}: a copy is its run's interval's partition of {declared.table}, and this run has none: give it one"
        try:
            path = lake.partition(declared.partition, interval)
        except lake.LakeError as error:
            return f"{name}: {error}"
        query = declared.query
        return CopyRecord(database=query.database, sql=query.sql, lake=declared.lake, table=declared.table, partition=path, ledger=declared.ledger), None

    @override
    def make(self, planned: Mapping[str, CopyRecord], declared: Mapping[str, LakeCopy], rows: Rows, reach: Reach) -> dict[str, ArchivedRecord]:
        """Each streamed from its query to the lake, recorded in its ledger's database."""
        del rows  # a copy's rows are its query's, streamed, never a frame the run kept
        done: dict[str, ArchivedRecord] = {}
        for name in sorted(planned):
            record, query = planned[name], declared[name].query
            try:
                if reach.interval is None:  # a defect: a copy is planned only for a run with an interval
                    msg = "a copy is its run's interval's partition, and this run has none"
                    raise ValueError(msg)  # noqa: TRY301 -- recorded as the copy's failure, as any other is
                archiving = lake.Archiving(reach.run_id, reach.job, reach.interval, VERSION, reach.again, reach.clock)
                bound = reach.interval.in_zone(query.zone) if query.zone is not None else reach.interval
                with reach.databases(record.database).reading() as reader:
                    streamed = lake.Streamed(frames.to_arrow(frames.empty(query.row.columns)).schema, _conformed(reader.batches(query.text(), bound, what=name), query, name))
                    done[name] = lake.archive(archiving, name, streamed, reach.open_lake(record.lake), reach.databases(record.ledger), table=record.table, path=record.partition)
            except Exception as error:
                log.exception("could not copy %s", name, extra={"output": name})
                done[name] = ArchivedRecord(status="failed", at=reach.clock(), error=_headline(error))
        return done


def _conformed(batches: Iterator[pa.RecordBatch], query: Query, name: str) -> Iterator[pa.RecordBatch]:
    """Each batch conformed to the query's row, as every frame a run reads is, one batch at a time; none fails a query
    whose dataset does not allow it."""
    what = f"{name}'s query ({query.sql})"
    rows = 0
    for batch in batches:
        table = frames.floats(pa.Table.from_batches([batch]), what=what)
        conformed = frames.to_arrow(conform(frames.from_arrow(table), query.row, what=what, naive="in_zone"))
        rows += conformed.num_rows
        yield from conformed.to_batches()
    if rows == 0 and query.empty == "fail":
        msg = f"{name}'s query {query.sql} returned no rows, which its dataset does not allow (empty='fail'): has the data landed?"
        raise ValueError(msg)


class Messages(Kind[Message[str] | Message[Mail], MessageRecord, SendRecord]):
    declaration = Message
    planned, done, failure, finishes = "messages", "sent", "send", "sends"
    rows = False

    @override
    def fakes_for(self, declared: Message[str] | Message[Mail]) -> dict[str, type[fakes.Fake]]:
        return {declared.to: fakes.Slack if isinstance(declared, Slack) else fakes.Email}

    @override
    def check(self, name: str, declared: Message[str] | Message[Mail], reach: Reach) -> list[str]:
        """Its ledger's database can be published to, and its channel or email is configured or faked."""
        reasons = _ledger_refusal(name, declared.ledger, reach.settings)
        configured = reach.settings.slack if isinstance(declared, Slack) else reach.settings.email
        if declared.to not in configured and declared.to not in reach.faked:
            setting = variable(("slack", declared.to, "webhook")) if isinstance(declared, Slack) else variable(("email", declared.to, "host")).removesuffix("__HOST") + "__*"
            reasons.append(f"{name}: the {declared.destination} {declared.to!r} it sends to is not configured: set {setting}, as .env.example shows")
        return reasons

    @override
    def plan(self, name: str, declared: Message[str] | Message[Mail], value: object, interval: Interval | None) -> tuple[MessageRecord, None] | str:
        """A message the transform gave None is none, which `plan` leaves out."""
        del interval
        given = declared.checked(value)
        assert given is not None  # noqa: S101 -- `planned` leaves out a message given None
        subject, text = (None, given) if isinstance(given, str) else (given.subject, given.text)
        return MessageRecord(destination=declared.destination, to=declared.to, ledger=declared.ledger, subject=subject, text=text, sha256=digest(text)), None

    @override
    def make(self, planned: Mapping[str, MessageRecord], declared: Mapping[str, Message[str] | Message[Mail]], rows: Rows, reach: Reach) -> dict[str, SendRecord]:
        """Each recorded in its ledger's database, which has each one sent, so none is sent twice."""
        delivery = sending.Delivery(reach.run_id, reach.job, reach.interval, VERSION, reach.again, reach.clock)
        sent: dict[str, SendRecord] = {}
        for name in sorted(planned):
            message = planned[name]
            try:
                sender = _sender(declared[name], message, reach)
                sent[name] = sending.deliver(delivery, name, message, sender, reach.databases(message.ledger))
            except Exception as error:
                log.exception("could not send %s", name, extra={"output": name})
                sent[name] = SendRecord(status="failed", at=reach.clock(), error=_headline(error))
        return sent


def _sender(declared: Message[str] | Message[Mail], message: MessageRecord, reach: Reach) -> Callable[[], None]:
    """What sends `message`: the channel's poster, or the people's mailer, as the settings configure them, or its fake."""
    fake = reach.faked.get(message.to)
    if isinstance(resource := declared.resource, SlackResource):
        poster = sending.poster(resource, reach.settings, reach.tracer, fake=fake if isinstance(fake, fakes.Slack) else None)
        return lambda: poster.post(message.text)
    mailer = sending.mailer(resource, reach.settings, reach.tracer, fake=fake if isinstance(fake, fakes.Email) else None)
    mail = Mail(subject=message.subject or "", text=message.text)
    return lambda: mailer.send(mail)


KINDS: tuple[Kind[Any, Any, Any], ...] = (Reports(), Upserts(), Archives(), Copies(), Messages())
"""Every kind, in the order a run makes them: files and documents first, and each message last, once everything it
may report on is made."""


def fakes_of(outputs: type[Outputs]) -> dict[str, type[fakes.Fake]]:
    """The fakes a test may give for what the job's effects reach, by name."""
    return {name: fake for kind in KINDS for declared in kind.declared(outputs).values() for name, fake in kind.fakes_for(declared).items()}


def check(outputs: type[Outputs], reach: Reach) -> list[str]:
    """Every reason a run that publishes cannot make one of the job's effects."""
    return [reason for kind in KINDS for name, declared in kind.declared(outputs).items() for reason in kind.check(name, declared, reach)]


@dataclass(frozen=True)
class Planned:
    """What a run will make of its effects: each kind's plan, by the run record's field, each output's rows conformed,
    and every reason it cannot make one."""

    records: dict[str, dict[str, Model]] = field(default_factory=lambda: {kind.planned: {} for kind in KINDS})
    frames: dict[str, pd.DataFrame] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def plan(outputs: Outputs, interval: Interval | None) -> Planned:
    """Each effect the transform gave, planned."""
    planned = Planned()
    for kind in KINDS:
        for name, declared in kind.declared(type(outputs)).items():
            value = outputs.values.get(name)  # noqa: PD011 -- the outputs by name, no frame's values
            if value is None and declared.given:
                continue  # a message the transform gave None
            match kind.plan(name, declared, value, interval):
                case str(reason):
                    planned.reasons.append(reason)
                case (record, frame):
                    planned.records[kind.planned][name] = record
                    if frame is not None:
                        planned.frames[name] = frame
    return planned


def unfinished(record: RunRecord) -> bool:
    """Whether `record` planned an effect it has not made."""
    return any(name not in done or kind.failed(done[name]) for kind in KINDS for planned, done in [kind.of(record)] for name in planned)


def planned_any(record: RunRecord) -> bool:
    return any(kind.of(record)[0] for kind in KINDS)


def apply(outputs: type[Outputs], record: RunRecord, rows: Rows, reach: Reach) -> tuple[dict[str, dict[str, Model]], bool]:
    """Make each effect `record` planned and has not made, each kind in turn, and stop at the first kind one of whose
    outputs failed: what became of each, by the run record's field, `record`'s own among them, and whether every one
    was made."""
    made: dict[str, dict[str, Model]] = {}
    stopped = False
    for kind in KINDS:
        planned, done = kind.of(record)
        todo = {name: entry for name, entry in planned.items() if name not in done or kind.failed(done[name])}
        if todo and not stopped:
            done |= kind.make(todo, kind.declared(outputs), rows, reach)
            stopped = any(kind.failed(entry) for entry in done.values())
        made[kind.done] = done
    return made, not stopped


def failures(record: RunRecord, run: str, command: str) -> tuple[str, str] | None:
    """Why a run that published could not make an effect, and the fix, or None where none failed."""
    for kind in KINDS:
        _, done = kind.of(record)
        if failed := [f"{name}: {getattr(entry, 'error', None)}" for name, entry in done.items() if kind.failed(entry)]:
            return f"{run} published, and could not {kind.failure} {'; '.join(failed)}", f"fix it, then `{command}`, which {kind.finishes} what it has not"
    return None
