"""What a job reads: `Query`, rows a SQL file selects from a database; `Fetch`, rows another service answers a GET
with; and `File`, a Parquet or CSV file. Each is declared once, as a class attribute of the job's `Inputs`, whose
attribute names the dataset: on the class it is the declaration, and on an instance the frame, so `inputs.trades` is a
`DataFrame` that ty checks and editors complete. `Fetch` is the one way a job reaches another service.

A declaration opens nothing, so job code can import it. Paths are relative to the file that declares them. A query
binds only the run's interval, as `interval_start` and `interval_end`, never a param, and a fetch's path only the
interval's ends: a replay with other params reads the same snapshot. Each declares what an empty result means,
`empty="fail"` or `empty="allow"`, because an empty feed that has not landed yet and a day with nothing to report look
the same.

A transform that needs the time or new ids declares a `Clock` or `Ids` on its `Inputs` too, and calls it:
`inputs.clock.now()`, `inputs.ids.new()`, or `inputs.ids.new(count)` for a column's. A run keeps each value they give in
its record, and a replay of the run is given the same values again, in the same order, so the transform stays a
function of what the run saved."""

import inspect
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol, Self, overload, override
from urllib.parse import quote
from uuid import UUID

import pandas as pd

from basis.core import resources
from basis.core.interval import Interval
from basis.core.resources import HttpResource
from basis.core.tables import IDENTIFIER
from basis.job.rows import Row

type Empty = Literal["fail", "allow"]


class Dataset:
    """A declared input, named by its attribute on the job's `Inputs`."""

    row: type[Row]
    empty: Empty
    base: Path
    """The folder of the file that declares it, which its path is relative to."""

    def __init__(self, row: type[Row], *, empty: Empty, base: Path) -> None:
        self.row = row
        self.empty = empty
        self.base = base
        self._name: str | None = None

    @property
    def name(self) -> str:
        if self._name is None:
            msg = "a dataset is named by its attribute on a job's Inputs class; this one is not on one"
            raise TypeError(msg)
        return self._name

    def __set_name__(self, owner: type, name: str) -> None:
        if not IDENTIFIER.match(name):
            msg = f"{owner.__qualname__}.{name}: a dataset's name is lower-case letters, digits and underscores"
            raise TypeError(msg)
        if self._name not in {None, name}:
            msg = f"{owner.__qualname__}.{name} is already declared as {self._name!r}: declare each dataset once per name"
            raise TypeError(msg)
        self._name = name

    @overload
    def __get__(self, instance: None, owner: type) -> Self: ...
    @overload
    def __get__(self, instance: Inputs, owner: type) -> pd.DataFrame: ...
    def __get__(self, instance: Inputs | None, owner: type) -> Self | pd.DataFrame:
        if instance is None:
            return self
        return instance.frames[self.name]

    def describe(self) -> str:
        raise NotImplementedError


class Query(Dataset):
    """Rows that a SQL file, in the database's own dialect, selects from a named database: `:interval_start` and
    `:interval_end` bind the interval in Oracle, `%(interval_start)s` and `%(interval_end)s` in SQL Server."""

    def __init__(self, row: type[Row], *, database: str, sql: str, empty: Empty) -> None:
        super().__init__(row, empty=empty, base=_caller_folder())
        if not IDENTIFIER.match(database):
            msg = f"database {database!r}: a database's name is lower-case letters, digits and underscores, as in BASIS_DATABASES__{database.upper()}__KIND"
            raise ValueError(msg)
        self.database = database
        self.sql = sql

    @property
    def path(self) -> Path:
        return self.base / self.sql

    def text(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @override
    def describe(self) -> str:
        return f"{self.database}: {self.sql}"


class Fetch(Dataset):
    """Rows another service answers a GET with: a JSON list of objects, one per row, each of the row's columns by its
    name, which a run reads as it reads a CSV's text, so a decimal or a time may come as a string, and a number keeps
    the digits it was sent with. `http` names the service, as `BASIS_HTTP__NAME__URL` configures it, and `path` is the
    service's own, after its base URL, binding only the run's interval: `{start}` and `{end}` give its ends in ISO
    8601, as `2026-11-05T00:00:00+00:00`, or in the format the path names, as `prices/{start:%Y-%m-%d}`, each
    percent-encoded, so that its `+` and `:` reach the service as they are. The GET has `deadline_s` and a span of its
    own, and its rows are saved in the run's snapshot like any input's, so a replay never calls the service."""

    def __init__(self, row: type[Row], *, http: str, path: str, deadline_s: float, empty: Empty) -> None:
        super().__init__(row, empty=empty, base=_caller_folder())
        self.resource: HttpResource = resources.http(http, deadline_s=deadline_s)
        self.path = path

    @property
    def http(self) -> str:
        return self.resource.name

    def location(self, interval: Interval | None) -> str:
        """The path of the run's GET, its interval's ends formatted in, and percent-encoded."""
        if "{" not in self.path:
            return self.path
        if interval is None:
            msg = f"{self.path} formats the interval, and this run has none: give it one"
            raise ValueError(msg)
        return self.path.format(start=_End.of(interval.start), end=_End.of(interval.end))

    @override
    def describe(self) -> str:
        return f"{self.http}: GET {self.path}"


class _End(datetime):
    """An interval's end as a fetch's path gives it: in ISO 8601 unless the path names a format, and percent-encoded,
    as a path segment or a query's value needs it."""

    @classmethod
    def of(cls, moment: datetime) -> _End:
        return cls.combine(moment.date(), moment.timetz())

    @override
    def __format__(self, spec: str) -> str:
        return quote(super().__format__(spec) if spec else self.isoformat(), safe="")


class File(Dataset):
    """A Parquet or CSV file, by path or URL. `{start}` and `{end}` in the path format the interval's ends, as
    `rates/{start:%Y-%m-%d}.csv`. A CSV's values are text, which the row converts, so `001234` stays a string where
    the row says so; an empty field is null."""

    def __init__(self, row: type[Row], *, path: str, empty: Empty) -> None:
        super().__init__(row, empty=empty, base=_caller_folder())
        if not path.endswith((".csv", ".parquet")):
            msg = f"{path}: a file dataset is .csv or .parquet"
            raise ValueError(msg)
        self.path = path

    def location(self, interval: Interval | None) -> str:
        """The path for a run: absolute, or a URL."""
        path = self.path
        if "{" in path:
            if interval is None:
                msg = f"{path} formats the interval, and this run has none: give it one"
                raise ValueError(msg)
            path = path.format(start=interval.start, end=interval.end)
        return path if "://" in path else str(self.base / path)

    @override
    def describe(self) -> str:
        return f"file: {self.path}"


def _caller_folder() -> Path:
    """The folder of the module that declares the dataset, in a class body or at module level: two frames up, past
    `Query.__init__` or `File.__init__`."""
    frame = inspect.currentframe()
    for _ in range(2):
        frame = frame.f_back if frame is not None else None
    file = frame.f_globals.get("__file__") if frame is not None else None
    return Path(file).resolve().parent if isinstance(file, str) else Path.cwd()


class NewIds(Protocol):
    """What a transform gets for an `Ids`."""

    @overload
    def new(self) -> UUID: ...
    @overload
    def new(self, count: int) -> list[UUID]: ...
    def new(self, count: int | None = None) -> UUID | list[UUID]:
        """A new UUIDv7, or `count` of them, for a column's ids."""
        ...


@dataclass(frozen=True)
class Handles:
    """What a run gives a transform besides its frames: each clock and ids its `Inputs` declare, by name."""

    clocks: Mapping[str, resources.Clock] = field(default_factory=dict[str, resources.Clock])
    ids: Mapping[str, NewIds] = field(default_factory=dict[str, NewIds])


class _Given:
    """A clock or ids, named by its attribute on a job's `Inputs`, which must be a resource's name, as basis-core's
    `resources.NAME` says, checked when the class is made."""

    kind = "resource"

    def __init__(self) -> None:
        self._name: str | None = None

    @property
    def name(self) -> str:
        if self._name is None:
            msg = f"a {self.kind} is named by its attribute on a job's Inputs class; this one is not on one"
            raise TypeError(msg)
        return self._name

    def __set_name__(self, owner: type, name: str) -> None:
        try:
            resources.named(name, self.kind)
        except resources.ResourceError as error:
            error.add_note(f"declared as {owner.__qualname__}.{name}: rename it")
            raise
        if self._name not in {None, name}:
            msg = f"{owner.__qualname__}.{name} is already declared as {self._name!r}: declare each {self.kind} once per name"
            raise TypeError(msg)
        self._name = name

    def _missing(self, owner: type) -> LookupError:
        return LookupError(f"{owner.__qualname__}.{self.name} is a {self.kind} the run gives, and these inputs have none: build them with one, as basis.job.testing.inputs does")


class Clock(_Given):
    """The time, which a transform reads as `inputs.NAME.now()`, in UTC: the system's clock in a run, and in a replay
    the times its run read, in order."""

    kind = "clock"

    @property
    def resource(self) -> resources.ClockResource:
        return resources.ClockResource(self.name)

    @overload
    def __get__(self, instance: None, owner: type) -> Self: ...
    @overload
    def __get__(self, instance: Inputs, owner: type) -> resources.Clock: ...
    def __get__(self, instance: Inputs | None, owner: type) -> Self | resources.Clock:
        if instance is None:
            return self
        if (found := instance.handles.clocks.get(self.name)) is None:
            raise self._missing(owner)
        return found


class Ids(_Given):
    """New ids, UUIDv7s, which a transform makes as `inputs.NAME.new()`, or `inputs.NAME.new(count)` for a column's:
    random in a run, and in a replay the ids its run made, in order."""

    kind = "ids"

    @property
    def resource(self) -> resources.IdsResource:
        return resources.IdsResource(self.name)

    @overload
    def __get__(self, instance: None, owner: type) -> Self: ...
    @overload
    def __get__(self, instance: Inputs, owner: type) -> NewIds: ...
    def __get__(self, instance: Inputs | None, owner: type) -> Self | NewIds:
        if instance is None:
            return self
        if (found := instance.handles.ids.get(self.name)) is None:
            raise self._missing(owner)
        return found


class Inputs:
    """Base for a job's `Inputs`. Each class attribute that is a `Query`, a `Fetch` or a `File` is a dataset the job
    reads, and each that is a `Clock` or `Ids` gives it the time or new ids."""

    def __init__(self, frames: Mapping[str, pd.DataFrame], *, interval: Interval | None, handles: Handles | None = None) -> None:
        self.frames = dict(frames)
        """Each dataset's frame, by its name."""
        self.interval = interval
        """The run's interval, or None for a run without one."""
        self.handles = handles if handles is not None else Handles()
        """Each clock's and ids' handle, by its name."""

    @classmethod
    def declared(cls) -> dict[str, Dataset]:
        return {name: value for klass in reversed(cls.__mro__) for name, value in vars(klass).items() if isinstance(value, Dataset)}

    @classmethod
    def declared_resources(cls) -> dict[str, Clock | Ids]:
        """Each clock and ids the inputs declare, by name."""
        return {name: value for klass in reversed(cls.__mro__) for name, value in vars(klass).items() if isinstance(value, Clock | Ids)}
