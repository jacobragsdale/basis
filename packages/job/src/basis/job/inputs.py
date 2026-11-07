"""What a job reads: `Query`, rows a SQL file selects from a database, and `File`, a Parquet or CSV file. Each is
declared once, as a class attribute of the job's `Inputs`, whose attribute names the dataset: on the class it is the
declaration, and on an instance the frame, so `inputs.trades` is a `DataFrame` that ty checks and editors complete.

A declaration opens nothing, so job code can import it. Paths are relative to the file that declares them. A query
binds only the run's interval, as `interval_start` and `interval_end`, never a param: a replay with other params reads the same
snapshot. Each declares what an empty result means, `empty="fail"` or `empty="allow"`, because an empty feed that has
not landed yet and a day with nothing to report look the same."""

import inspect
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self, overload, override

import polars as pl

from basis.core.interval import Interval
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
    def __get__(self, instance: Inputs, owner: type) -> pl.DataFrame: ...
    def __get__(self, instance: Inputs | None, owner: type) -> Self | pl.DataFrame:
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
            msg = f"database {database!r}: a database's name is lower-case letters, digits and underscores, as in ETL_DATABASES__{database.upper()}__KIND"
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


class Inputs:
    """Base for a job's `Inputs`. Each class attribute that is a `Query` or a `File` is a dataset the job reads."""

    def __init__(self, frames: Mapping[str, pl.DataFrame], *, interval: Interval | None) -> None:
        self.frames = dict(frames)
        """Each dataset's frame, by its name."""
        self.interval = interval
        """The run's interval, or None for a run without one."""

    @classmethod
    def declared(cls) -> dict[str, Dataset]:
        return {name: value for klass in reversed(cls.__mro__) for name, value in vars(klass).items() if isinstance(value, Dataset)}
