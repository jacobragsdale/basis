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
from typing import Self, overload, override

import pandas as pd

from basis.core import messages
from basis.core.messages import Destination, EmailResource, Mail, SlackResource
from basis.core.tables import IDENTIFIER, Changes, Column
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


class Table[V: (pd.DataFrame, Changes)]:
    """A declared output, named by its attribute on the job's `Outputs`. On an instance, its value is the frame the
    transform returned, or for `Manual()`, the `Changes`."""

    @overload
    def __init__(self: Table[Changes], row: type[Row], *, database: str, table: str, write: Manual, key: tuple[str, ...] | None = None) -> None: ...
    @overload
    def __init__(self: Table[pd.DataFrame], row: type[Row], *, database: str, table: str, write: FrameWrite, key: tuple[str, ...] | None = None) -> None: ...
    def __init__(self, row: type[Row], *, database: str, table: str, write: Write, key: tuple[str, ...] | None = None) -> None:
        self.target = Target(row, database, table, write, key)
        self._name: str | None = None
        _validate(self.target)

    @property
    def name(self) -> str:
        if self._name is None:
            msg = "an output is named by its attribute on a job's Outputs class; this one is not on one"
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
    if target.key is not None and (nullable := [column.name for column in target.key_columns if column.nullable]):
        msg = f"{where}: its key's columns may not be null, but {nullable} are `| None`"
        raise ValueError(msg)
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
            if nullable := [name for name in matching if target.row.column(name).nullable]:
                msg = f"{where}: ReplaceWhere(matching=...) columns may not be null, since null matches nothing: {nullable}"
                raise ValueError(msg)
        case _:
            pass


class Message[V: (str, Mail)]:
    """A message the job sends, named by its attribute on the job's `Outputs`: a Slack channel's (`Slack`) or an
    email (`Email`). The transform returns its text, or None for none this run; only `basis run --publish` sends it,
    once every database has committed, recording it in the ledger of the database `ledger` names, which the run's
    settings must let it publish to."""

    destination: Destination

    def __init__(self, resource: SlackResource | EmailResource, *, ledger: str) -> None:
        if not IDENTIFIER.match(ledger):
            msg = f"ledger={ledger!r}: a message names the database whose ledger records it, lower-case letters, digits and underscores, as in BASIS_DATABASES__{ledger.upper()}__KIND"
            raise ValueError(msg)
        self.resource = resource
        self.ledger = ledger
        self._name: str | None = None

    @property
    def to(self) -> str:
        return self.resource.name

    @property
    def name(self) -> str:
        if self._name is None:
            msg = "a message is named by its attribute on a job's Outputs class; this one is not on one"
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

    def describe(self) -> str:
        return f"{self.destination}: {self.to}"

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


type OutputValue = pd.DataFrame | Changes | str | Mail | None
"""What the transform gives an output: a table's frame or `Changes`, or a message's text or mail, or None for none."""


class Outputs:
    """Base for a job's `Outputs`. Each class attribute that is a `Table` is one the job writes, and each that is a
    `Slack` or an `Email` a message it sends. The transform returns an instance with every output, by name:
    `Outputs(positions=frame, alert=None)`."""

    def __init__(self, **values: OutputValue) -> None:
        declared, sent = self.declared(), self.messages()
        if missing := sorted((set(declared) | set(sent)) - set(values)):
            msg = f"{type(self).__qualname__} is missing outputs {missing}: give every output it declares, None for a message it does not send"
            raise TypeError(msg)
        if unknown := sorted(set(values) - set(declared) - set(sent)):
            msg = f"{type(self).__qualname__} declares no outputs named {unknown}; it declares {sorted(set(declared) | set(sent))}"
            raise TypeError(msg)
        for name, table in declared.items():
            expected = Changes if isinstance(table.target.write, Manual) else pd.DataFrame
            if not isinstance(values[name], expected):
                msg = f"{name} is written with {type(table.target.write).__name__}(), so its value is a {expected.__name__}, not a {type(values[name]).__name__}"
                raise TypeError(msg)
        for name, message in sent.items():
            message.checked(values[name])
        self.values: Mapping[str, OutputValue] = values

    def table(self, name: str) -> pd.DataFrame | Changes:
        """The frame or `Changes` the transform gave the table `name`."""
        value = self.values[name]
        if not isinstance(value, pd.DataFrame | Changes):
            msg = f"{name} is a table, so its value is a DataFrame or Changes, not a {type(value).__name__}"
            raise TypeError(msg)
        return value

    @classmethod
    def declared(cls) -> dict[str, Table[pd.DataFrame] | Table[Changes]]:
        """Each table the job writes, by its output's name."""
        return {name: value for klass in reversed(cls.__mro__) for name, value in vars(klass).items() if isinstance(value, Table)}

    @classmethod
    def messages(cls) -> dict[str, Slack | Email]:
        """Each message the job sends, by its output's name."""
        return {name: value for klass in reversed(cls.__mro__) for name, value in vars(klass).items() if isinstance(value, Slack | Email)}
