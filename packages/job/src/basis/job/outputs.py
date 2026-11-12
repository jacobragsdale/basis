"""What a job writes. Each output is a `Table` on the job's `Outputs`: its row, the database and table it is written
to, its key if it has one, and how it is written, which every table names, since nothing about a table is assumed.

The core writes a table through four operations, applied in one transaction in this order: delete (every row, rows
in a range, or rows matching given values), upsert by key, insert where the key is new, and insert, as
`basis.core.tables.Changes`. Each write mode builds them from the frame the transform returns for the table:

| Mode | Operations |
|---|---|
| `Append()` | insert |
| `Replace()` | delete every row, then insert |
| `ReplaceWhere(interval=..., matching=...)` | delete the rows in the run's interval, or matching the frame's values, then insert |
| `Upsert()` | upsert by key |
| `InsertNew()` | insert the rows whose key is not in the table |
| `Manual()` | whatever `Changes` the transform returns |

A table written with `Manual()` takes `Changes` from the transform, which builds anything else: a history table, a
dedupe against rows already there (the destination read as an input), a watermark."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar, Literal, Self, overload, override

import pandas as pd

from basis.core import messages
from basis.core.interval import Interval
from basis.core.messages import Destination, EmailResource, Mail, SlackResource
from basis.core.tables import IDENTIFIER, Changes, Column
from basis.job.inputs import Query
from basis.job.rows import Row

TABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,127}(\.[A-Za-z_][A-Za-z0-9_$]{0,127})?$")


@dataclass(frozen=True)
class Append:
    pass


@dataclass(frozen=True)
class Replace:
    pass


@dataclass(frozen=True)
class ReplaceWhere:
    """Delete the rows whose `interval` column is in the run's interval, and whose `matching` columns equal the
    values of a row of the frame, then insert the frame. Each row the frame inserts must be one the delete covers, or
    a rerun would insert it twice."""

    interval: str | None = None
    matching: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.interval is None and not self.matching:
            msg = "ReplaceWhere names the rows it replaces: interval=COLUMN, matching=(COLUMN, ...), or both"
            raise ValueError(msg)


@dataclass(frozen=True)
class Upsert:
    pass


@dataclass(frozen=True)
class InsertNew:
    pass


@dataclass(frozen=True)
class Manual:
    pass


type FrameWrite = Append | Replace | ReplaceWhere | Upsert | InsertNew
"""The modes that build changes from a frame."""
type Write = FrameWrite | Manual


@dataclass(frozen=True)
class Target:
    """Where a table is written, and how: `basis.core.tables.Target`, with the row and the write mode that make it."""

    row: type[Row]
    database: str
    table: str
    write: Write
    key: tuple[str, ...] | None = field(default=None)

    @property
    def key_columns(self) -> list[Column]:
        return [self.row.column(name) for name in self.key or ()]

    @property
    def columns(self) -> tuple[Column, ...]:
        return self.row.columns

    def column(self, name: str) -> Column:
        return self.row.column(name)


class Named:
    """An output named by its attribute on a job's `Outputs`: the one rule for naming each kind."""

    noun: ClassVar[str] = "an output"
    """What it is, in the refusal of one that is not on an `Outputs`."""

    _name: str | None = None

    @property
    def name(self) -> str:
        if self._name is None:
            msg = f"{self.noun} is named by its attribute on a job's Outputs class; this one is not on one"
            raise TypeError(msg)
        return self._name

    def __set_name__(self, owner: type, name: str) -> None:
        if not IDENTIFIER.match(name):
            msg = f"{owner.__qualname__}.{name}: an output's name is lower-case letters, digits and underscores"
            raise TypeError(msg)
        if self._name not in {None, name}:
            msg = f"{owner.__qualname__}.{name} is already declared as {self._name!r}: declare each output once per name"
            raise TypeError(msg)
        self._name = name


class Effect(Named):
    """An output that is not a table: one a run makes only once every database has committed, as `basis.job.effects`
    gives each kind. Its value is checked when the transform's `Outputs` is made."""

    given: ClassVar[bool] = True
    """Whether the transform gives it a value; a copy, which basis makes from its query alone, has none."""

    def checked(self, value: object) -> object:
        """The value the transform gave it, refused where it is not of its kind."""
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError


class FrameEffect(Effect):
    """An effect whose value is a frame of its row."""

    row: type[Row]

    @override
    def checked(self, value: object) -> pd.DataFrame:
        if not isinstance(value, pd.DataFrame):
            msg = f"{self.name} is written from a frame, so its value is a DataFrame, not a {type(value).__name__}"
            raise TypeError(msg)
        return value

    @overload
    def __get__(self, instance: None, owner: type) -> Self: ...
    @overload
    def __get__(self, instance: Outputs, owner: type) -> pd.DataFrame: ...
    def __get__(self, instance: Outputs | None, owner: type) -> Self | pd.DataFrame:
        if instance is None:
            return self
        return self.checked(instance.values[self.name])  # noqa: PD011 -- the outputs by name, no frame's values


@dataclass(frozen=True)
class RowCount:
    """The table's rows, within `within`, a share, of the median of the job's last `last` published runs' rows of it,
    as `within=0.5` allows half as many to half as many again. A job with no published run before has nothing to
    compare, and passes; a replay reads no history, and passes too."""

    within: float
    last: int = 10

    def __post_init__(self) -> None:
        if not self.within > 0:
            msg = f"a row count's band is a share of the median above 0, as within=0.5, not {self.within}"
            raise ValueError(msg)
        if self.last < 1:
            msg = f"last is the number of published runs, 1 or more, not {self.last}"
            raise ValueError(msg)


@dataclass(frozen=True)
class NullRate:
    """The share of the table's rows whose `column` is null, at most `at_most`, from 0 to 1."""

    column: str
    at_most: float

    def __post_init__(self) -> None:
        if not 0 <= self.at_most <= 1:
            msg = f"a share of nulls is from 0 to 1, not {self.at_most}"
            raise ValueError(msg)


@dataclass(frozen=True)
class Total:
    """The total of the table's float `column`, within `within` of the total of an input's column, `matches`, as
    `trades.quantity`: what reconciles a load with what it read."""

    column: str
    matches: str
    within: float = 0.0

    def __post_init__(self) -> None:
        dataset, _, column = self.matches.partition(".")
        if not dataset or not column:
            msg = f"matches names DATASET.COLUMN, an input and its column, as 'trades.quantity', not {self.matches!r}"
            raise ValueError(msg)
        if self.within < 0:
            msg = f"within is 0 or more, not {self.within}"
            raise ValueError(msg)


type Expectation = RowCount | NullRate | Total
"""What a table's rows are expected to be, before anything is written (`basis.job.expectations`)."""


def _expected(row: type[Row], table: str, expect: tuple[Expectation, ...]) -> None:
    columns = {column.name: column for column in row.columns}
    for expectation in expect:
        match expectation:
            case NullRate(column=name) if name not in columns:
                msg = f"{table}: expects {name}'s share of nulls, and its row has no column {name}"
                raise ValueError(msg)
            case Total(column=name) if name not in columns or columns[name].kind != "float":
                msg = f"{table}: expects {name}'s total, and {name} is no float column of its row"
                raise ValueError(msg)
            case RowCount() | NullRate() | Total():
                pass
            case _:
                msg = f"{table}: an expectation is an etl.RowCount, etl.NullRate or etl.Total, not {expectation!r}"
                raise TypeError(msg)


class Table[V: (pd.DataFrame, Changes)](Named):
    """A declared output, named by its attribute on the job's `Outputs`. On an instance, its value is the frame the
    transform returned, or for `Manual()`, the `Changes`."""

    @overload
    def __init__(self: Table[Changes], row: type[Row], *, database: str, table: str, write: Manual, key: tuple[str, ...] | None = None) -> None: ...
    @overload
    def __init__(self: Table[pd.DataFrame], row: type[Row], *, database: str, table: str, write: FrameWrite, key: tuple[str, ...] | None = None, expect: tuple[Expectation, ...] = ()) -> None: ...
    def __init__(self, row: type[Row], *, database: str, table: str, write: Write, key: tuple[str, ...] | None = None, expect: tuple[Expectation, ...] = ()) -> None:
        self.target = Target(row, database, table, write, key)
        _validate(self.target)
        _expected(row, table, tuple(expect))
        self.expect = tuple(expect)
        """What the table's rows are expected to be, each a reason not to write where they are not."""

    @overload
    def __get__(self, instance: None, owner: type) -> Self: ...
    @overload
    def __get__(self, instance: Outputs, owner: type) -> V: ...
    def __get__(self, instance: Outputs | None, owner: type) -> Self | V:
        if instance is None:
            return self
        value = instance.values[self.name]  # noqa: PD011 -- the outputs by name, no frame's values
        expected = Changes if isinstance(self.target.write, Manual) else pd.DataFrame
        if not isinstance(value, expected):
            msg = f"{self.name} holds a {type(value).__name__}, not a {expected.__name__}"
            raise TypeError(msg)
        return value  # ty: ignore[invalid-return-type] -- the isinstance above is V's runtime check; ty cannot relate a constrained TypeVar to a class object


def _validate_key(target: Target, where: str) -> None:
    """A key's columns are neither JSON, which no database compares, nor nullable."""
    if target.key is not None and (compared := [column.name for column in target.key_columns if column.json]):
        msg = f"{where}: its key's columns may not be JSON, which no database compares, but {compared} are etl.Json()"
        raise ValueError(msg)
    if target.key is not None and (nullable := [column.name for column in target.key_columns if column.nullable]):
        msg = f"{where}: its key's columns may not be null, but {nullable} are `| None`"
        raise ValueError(msg)


def _validate(target: Target) -> None:
    where = target.table
    if not TABLE_NAME.match(target.table):
        msg = f"{where}: a table is TABLE or SCHEMA.TABLE, letters, digits, underscores and $, never quoted"
        raise ValueError(msg)
    if not IDENTIFIER.match(target.database):
        msg = f"{where}: database {target.database!r} is lower-case letters, digits and underscores, as in BASIS_DATABASES__{target.database.upper()}__KIND"
        raise ValueError(msg)
    names = target.row.names()
    if target.key is not None and not target.key:
        msg = f"{where}: a key names at least one column; leave key out for a table without one"
        raise ValueError(msg)
    if target.key is not None and (unknown := [name for name in target.key if name not in names]):
        msg = f"{where}: its key names columns its row does not have: {unknown}"
        raise ValueError(msg)
    _validate_key(target, where)
    match target.write:
        case Upsert() | InsertNew() if target.key is None:
            msg = f"{where}: {type(target.write).__name__}() needs the table's key, as key=('id',)"
            raise ValueError(msg)
        case ReplaceWhere(interval=interval, matching=matching):
            if interval is not None and target.row.column(interval).kind not in {"date", "datetime"}:
                msg = f"{where}: ReplaceWhere(interval={interval!r}) needs a date or datetime column"
                raise ValueError(msg)
            if unknown := [name for name in matching if name not in names]:
                msg = f"{where}: ReplaceWhere(matching=...) names columns its row does not have: {unknown}"
                raise ValueError(msg)
            if compared := [name for name in matching if target.row.column(name).json]:
                msg = f"{where}: ReplaceWhere(matching=...) columns may not be JSON, which no database compares: {compared}"
                raise ValueError(msg)
            if nullable := [name for name in matching if target.row.column(name).nullable]:
                msg = f"{where}: ReplaceWhere(matching=...) columns may not be null, since null matches nothing: {nullable}"
                raise ValueError(msg)
        case _:
            pass


class Message[V: (str, Mail)](Effect):
    """A message the job sends, named by its attribute on the job's `Outputs`: a Slack channel's (`Slack`) or an
    email (`Email`). The transform returns its text, or None for none this run; only `basis run --publish` sends it,
    once every database has committed, recording it in the ledger of the database `ledger` names, which the run's
    settings must let it publish to."""

    destination: Destination
    noun = "a message"

    def __init__(self, resource: SlackResource | EmailResource, *, ledger: str) -> None:
        if not IDENTIFIER.match(ledger):
            msg = f"ledger={ledger!r}: a message names the database whose ledger records it, lower-case letters, digits and underscores, as in BASIS_DATABASES__{ledger.upper()}__KIND"
            raise ValueError(msg)
        self.resource = resource
        self.ledger = ledger

    @property
    def to(self) -> str:
        return self.resource.name

    @override
    def describe(self) -> str:
        return f"{self.destination}: {self.to}"

    @override
    def checked(self, value: object) -> V | None:
        raise NotImplementedError

    @overload
    def __get__(self, instance: None, owner: type) -> Self: ...
    @overload
    def __get__(self, instance: Outputs, owner: type) -> V | None: ...
    def __get__(self, instance: Outputs | None, owner: type) -> Self | V | None:
        if instance is None:
            return self
        return self.checked(instance.values[self.name])  # noqa: PD011 -- the outputs by name, no frame's values


class Slack(Message[str]):
    """A message to a Slack channel, through its webhook, as `BASIS_SLACK__CHANNEL__WEBHOOK` configures it: its value
    is the text, which Slack reads as `mrkdwn`."""

    destination = "slack"

    def __init__(self, channel: str, *, ledger: str, deadline_s: float) -> None:
        super().__init__(messages.slack(channel, deadline_s=deadline_s), ledger=ledger)

    @override
    def checked(self, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            msg = f"{self.name} posts to Slack, so its value is its text or None, not a {type(value).__name__}"
            raise TypeError(msg)
        return messages.checked_text(value)


class Email(Message[Mail]):
    """An email to the people `BASIS_EMAIL__TO__TO` lists, through the relay the rest of the block names: its value is
    an `etl.Mail(subject=..., text=...)`."""

    destination = "email"

    def __init__(self, to: str, *, ledger: str, deadline_s: float) -> None:
        super().__init__(messages.email(to, deadline_s=deadline_s), ledger=ledger)

    @override
    def checked(self, value: object) -> Mail | None:
        if value is None or isinstance(value, Mail):
            return value
        msg = f"{self.name} is an email, so its value is an etl.Mail(subject=..., text=...) or None, not a {type(value).__name__}"
        raise TypeError(msg)


class ExcelReport(FrameEffect):
    """A workbook the job writes for people to a shared folder, as `BASIS_FOLDERS__FOLDER__PATH` gives it on each
    machine: its value is a frame of `row`, written as the sheet `sheet` of the workbook at `path` inside the folder,
    which may format the interval's ends, as `reports/{start:%Y-%m-%d}.xlsx`. Only `basis run --publish` writes it,
    once every database has committed, to a temporary file renamed into place, so a reader never sees half of it, and
    a rerun writes it again, whole. A repo that writes one takes `basis-core[excel]`."""

    noun = "a report"

    def __init__(self, row: type[Row], *, folder: str, path: str, sheet: str) -> None:
        if not IDENTIFIER.match(folder):
            msg = f"folder {folder!r}: a folder's name is lower-case letters, digits and underscores, as in BASIS_FOLDERS__{folder.upper()}__PATH"
            raise ValueError(msg)
        if not path.lower().endswith(".xlsx"):
            msg = f"{path}: basis writes workbooks as .xlsx: name it so"
            raise ValueError(msg)
        if not sheet or len(sheet) > MAX_SHEET or any(character in sheet for character in "[]:*?/\\"):
            msg = f"sheet {sheet!r}: a sheet's name is 1 to {MAX_SHEET} characters, none of []:*?/\\, as Excel allows"
            raise ValueError(msg)
        self.row = row
        self.folder = folder
        self.path = path
        self.sheet = sheet

    def location(self, interval: Interval | None) -> str:
        """The workbook's path in its folder for a run, its interval's ends formatted in."""
        if "{" not in self.path:
            return self.path
        if interval is None:
            msg = f"{self.path} formats the interval, and this run has none: give it one"
            raise ValueError(msg)
        return self.path.format(start=interval.start, end=interval.end)

    @override
    def describe(self) -> str:
        return f"{self.folder}: {self.path} [{self.sheet}]"


class MongoUpsert(FrameEffect):
    """Documents the job upserts into a Mongo collection, as `BASIS_MONGO__MONGO__*` configures the database: its value
    is a frame of `row`, each row a document of its columns, matched by `key`'s, whose fields it sets, inserting it
    where none matches, so a rerun changes nothing that is already so. `fields` names each column's field where the
    collection names it otherwise, as `{"accountid": "accountId"}`, since a column's name is lower case. Only `basis run
    --publish` upserts, once every database has committed; Mongo has no transaction and no ledger here
    (`basis.core.mongo`), so a run that publishes first checks that no field it writes is named otherwise there, and
    that the collection has a unique index on the key. A repo that writes Mongo takes `basis-core[mongo]`."""

    noun = "an upsert"

    def __init__(self, row: type[Row], *, mongo: str, collection: str, key: tuple[str, ...], fields: Mapping[str, str] | None = None) -> None:
        if not IDENTIFIER.match(mongo):
            msg = f"mongo {mongo!r}: a Mongo database's name is lower-case letters, digits and underscores, as in BASIS_MONGO__{mongo.upper()}__URI"
            raise ValueError(msg)
        if not collection or "$" in collection:
            msg = f"collection {collection!r}: name the collection, without $"
            raise ValueError(msg)
        names = row.names()
        unknown = [name for name in key if name not in names]
        if not key or unknown:
            msg = f"{collection}: an upsert's key names columns of its row, at least one, as key=('id',), not {unknown or list(key)}"
            raise ValueError(msg)
        if bad := [column.name for column in row.columns if column.name in key and (column.nullable or column.json)]:
            msg = f"{collection}: an upsert's key columns may be neither null nor JSON, but {bad} are"
            raise ValueError(msg)
        if unknown := sorted(set(fields or {}) - set(names)):
            msg = f"{collection}: fields names each column's field where the collection names it otherwise, by the row's column, and the row has no {', '.join(unknown)}"
            raise ValueError(msg)
        self.row = row
        self.mongo = mongo
        self.collection = collection
        self.key = key
        self.fields: dict[str, str] = dict(fields or {})
        """Each column's field in the collection, where it is not the column's own name, as `{"accountid": "accountId"}`."""

    def field(self, column: str) -> str:
        """`column`'s field in the collection."""
        return self.fields.get(column, column)

    @override
    def describe(self) -> str:
        return f"{self.mongo}: {self.collection}"


LAKE_TABLE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
"""A lake table's folder in its container: lower-case letters, digits and underscores, starting with a letter."""


class Lake(FrameEffect):
    """Rows the job archives to an Azure Data Lake container as Parquet, as `BASIS_LAKES__LAKE__*` configures it on
    each environment: its value is a frame of `row`, written as the run's interval's partition of the folder `table`,
    as `CONTAINER/trades/date=2026-11-07/`. `partition` says how partitions are named: `date`, for a job whose runs
    are each one UTC day, or `interval`, `start=20261101T000000Z/end=20261201T000000Z`, for any interval. Only `basis
    run --publish` archives, once every database has committed, recording it in `ledger`'s database as a publish is,
    so a run for an interval another run archived archives nothing, unless told `--again` (`basis.core.lake`)."""

    noun = "an archive"

    def __init__(self, row: type[Row], *, lake: str, table: str, partition: Literal["date", "interval"], ledger: str) -> None:
        if not IDENTIFIER.match(lake):
            msg = f"lake {lake!r}: a lake's name is lower-case letters, digits and underscores, as in BASIS_LAKES__{lake.upper()}__ACCOUNT"
            raise ValueError(msg)
        if not LAKE_TABLE.match(table):
            msg = f"table {table!r}: a lake table's folder is lower-case letters, digits and underscores, starting with a letter"
            raise ValueError(msg)
        if partition not in {"date", "interval"}:
            msg = f"partition {partition!r}: a lake table is partitioned by 'date' or by 'interval'"
            raise ValueError(msg)
        if not IDENTIFIER.match(ledger):
            msg = f"ledger {ledger!r}: name the database whose ledger records the archive, as in BASIS_DATABASES__{ledger.upper()}__*"
            raise ValueError(msg)
        self.row = row
        self.lake = lake
        self.table = table
        self.partition: Literal["date", "interval"] = partition
        self.ledger = ledger

    @override
    def describe(self) -> str:
        return f"{self.lake}: {self.table}/ by {self.partition}"


class LakeCopy(Effect):
    """A query's rows, copied to an Azure Data Lake container as Parquet with no frame and no transform, for a table
    too large for a run's memory: the database's rows stream in batches from its driver, each conformed to the query's
    row and written as it comes, as the run's interval's partition of the folder `table`, as `etl.Lake` writes one,
    recorded in `ledger`'s database. Only `basis run --publish` copies, once every database has committed, and a
    resume streams it again from the query. The transform gives it no value, and the run's snapshot saves none of its
    rows, so a replay copies nothing."""

    noun = "a copy"
    given = False

    def __init__(self, query: Query, *, lake: str, table: str, partition: Literal["date", "interval"], ledger: str) -> None:
        if not isinstance(query, Query):
            msg = "a copy is of an etl.Query, as etl.LakeCopy(etl.Query(Trade, database='markets', sql='sql/trades.sql', empty='fail'), ...)"
            raise TypeError(msg)
        archive = Lake(query.row, lake=lake, table=table, partition=partition, ledger=ledger)  # the same rules as an archive's
        self.query = query
        self.row = query.row
        self.lake = archive.lake
        self.table = archive.table
        self.partition: Literal["date", "interval"] = archive.partition
        self.ledger = archive.ledger

    @override
    def __set_name__(self, owner: type, name: str) -> None:
        super().__set_name__(owner, name)
        self.query.__set_name__(owner, name)  # its query is named as the output is, in what it says of its file

    @override
    def checked(self, value: object) -> None:
        if value is not None:
            msg = f"{self.name} is a copy, which basis makes from its query, so the transform gives it nothing"
            raise TypeError(msg)

    @override
    def describe(self) -> str:
        return f"{self.query.database}: {self.query.sql} to {self.lake}: {self.table}/ by {self.partition}"


MAX_SHEET = 31
"""The longest sheet name Excel takes."""


type OutputValue = pd.DataFrame | Changes | str | Mail | None
"""What the transform gives an output: a table's frame or `Changes`, or a message's text or mail, or None for none."""


class Outputs:
    """Base for a job's `Outputs`. Each class attribute that is a `Table` is one the job writes, and each that is a
    `Slack` or an `Email` a message it sends. The transform returns an instance with every output, by name:
    `Outputs(positions=frame, alert=None)`."""

    def __init__(self, **values: OutputValue) -> None:
        declared, effects = self.declared(), {name: effect for name, effect in self.declared_of(Effect).items() if effect.given}
        every = set(declared) | set(effects)
        if missing := sorted(every - set(values)):
            msg = f"{type(self).__qualname__} is missing outputs {missing}: give every output it declares, None for a message it does not send"
            raise TypeError(msg)
        if unknown := sorted(set(values) - every):
            msg = f"{type(self).__qualname__} declares no outputs named {unknown}; it declares {sorted(every)}"
            raise TypeError(msg)
        for name, effect in effects.items():
            effect.checked(values[name])
        for name, table in declared.items():
            expected = Changes if isinstance(table.target.write, Manual) else pd.DataFrame
            if not isinstance(values[name], expected):
                msg = f"{name} is written with {type(table.target.write).__name__}(), so its value is a {expected.__name__}, not a {type(values[name]).__name__}"
                raise TypeError(msg)
        self.values: Mapping[str, OutputValue] = values

    @classmethod
    def declared_of[E: Named](cls, kind: type[E]) -> dict[str, E]:
        """Each output of `kind` the job declares, by its name."""
        return {name: value for klass in reversed(cls.__mro__) for name, value in vars(klass).items() if isinstance(value, kind)}

    @classmethod
    def declared(cls) -> dict[str, Table[pd.DataFrame] | Table[Changes]]:
        """Each table the job writes, by its output's name."""
        return cls.declared_of(Table)

    @classmethod
    def declared_reports(cls) -> dict[str, ExcelReport]:
        """Each workbook the job writes, by its output's name."""
        return cls.declared_of(ExcelReport)

    @classmethod
    def declared_upserts(cls) -> dict[str, MongoUpsert]:
        """Each Mongo collection the job upserts into, by its output's name."""
        return cls.declared_of(MongoUpsert)

    @classmethod
    def declared_lakes(cls) -> dict[str, Lake]:
        """Each lake table the job archives to, by its output's name."""
        return cls.declared_of(Lake)

    @classmethod
    def declared_messages(cls) -> dict[str, Slack | Email]:
        """Each message the job sends, by its output's name."""
        return {name: value for name, value in cls.declared_of(Message).items() if isinstance(value, Slack | Email)}


def table_value(outputs: Outputs, name: str) -> pd.DataFrame | Changes:
    """The frame or `Changes` the transform gave the table `name`: a function, not a method, so no output's name hides it."""
    value = outputs.values[name]  # noqa: PD011 -- the outputs by name, no frame's values
    if not isinstance(value, pd.DataFrame | Changes):
        msg = f"{name} is a table, so its value is a DataFrame or Changes, not a {type(value).__name__}"
        raise TypeError(msg)
    return value
