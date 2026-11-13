"""The fakes a Dash app runs on in `basis dev` and in its tests: a database whose every query answers from a CSV beside
the app, `fixtures/QUERY.csv`, synthetic data in git, read through the query's row, so a fixture that does not match
its row fails as a real answer would.

    # positions/dev.py
    from basis.dash import fixtures

    from positions.data import DESK, WAREHOUSE


    def fakes() -> dict[object, object]:
        return {WAREHOUSE: fixtures.database(WAREHOUSE), DESK: fixtures.folder("fixtures/desk")}

The app declares them, `bd.App(..., fakes="positions.dev:fakes")`; `basis dev` serves the app on them, and
`bd.testing.client(app)` builds it on them, unless a test gives its own. A fake stands in for every resource the app
lists, or none, so a fake never meets a real database; `basis dev --env dev` serves the app on the dev databases
instead, read-only as ever.

- **A fixture's rows** are filtered by each bind whose name is a column of the row, as `as_of`: a query's answer is
  the fixture's rows for the day bound. A bind that is no column filters nothing.
- **A fixture is checked when the fake is made**: each query of the database has its file, and the file matches its
  row, or `DeclarationError` says which to write or fix.
- **It fails on demand**, as basis-core's fakes do: `fails(Unavailable, query="positions", times=1)` fails that
  query's next load with the kind a real database's driver error becomes, with the same detail, so every failure path
  of a page is tested without a mock.
- **It only reads**, as the app's databases do: it has no statement that writes.

A path is relative to the repo: the folder that holds the package of the module that names it."""

import importlib
import inspect
import io
import threading
from collections.abc import Generator, Hashable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pyarrow as pa

from basis.core import frames
from basis.core.databases import Bind
from basis.core.databases.failures import failure
from basis.core.errors import DATABASE_KINDS, ServiceError
from basis.core.fakes import Failures
from basis.core.rows import SchemaError, conform
from basis.dash.app import App
from basis.dash.declarations import RESOURCES, Database, Query, datasets
from basis.dash.errors import DeclarationError

FOLDER = "fixtures"
"""Where fixtures are, in the repo, by default."""


class FixtureError(RuntimeError):
    """What a fixture database is asked for that it cannot answer, which only a test makes."""


@dataclass
class FixtureDatabase:
    """A database whose queries answer from their fixtures: `database` makes one."""

    declared: Database
    folder: Path
    fixtures: dict[str, pd.DataFrame]
    """Each query's rows, by its name, as its row types them."""
    queries: list[tuple[str, dict[str, Bind]]] = field(default_factory=list[tuple[str, dict[str, Bind]]])
    """Each query it answered, by name, with its binds, in order."""
    _sql: dict[str, str] = field(default_factory=dict[str, str])
    _failing: Failures[str | None] = field(default_factory=lambda: Failures[str | None]("a fixture database", DATABASE_KINDS))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def name(self) -> str:
        return self.declared.name

    @property
    def dialect(self) -> str:
        return self.declared.dialect

    def fails(self, kind: type[ServiceError], /, *, query: str | None = None, times: int | None = None) -> None:
        """Fail each load of `query`, by its dataset's name, or of every query, with `kind`, as the database would:
        every time, or the next `times`, after which it answers again."""
        if query is not None and query not in self.fixtures:
            msg = f"database {self.name} has no query {query}: name one of {', '.join(sorted(self.fixtures))}"
            raise FixtureError(msg)
        self._failing.set(query, kind, times)

    @contextmanager
    def statements(self, *, writes: bool) -> Generator[_Statements]:
        if writes:
            msg = f"the fixture database {self.name} only reads, as a Dash app's databases do"
            raise FixtureError(msg)
        yield _Statements(self)

    def answer(self, sql: str, binds: Mapping[str, Bind]) -> pa.Table:
        name = self._query_of(sql)
        with self._lock:
            self.queries.append((name, dict(binds)))
        if (taken := self._failing.take(name, None)) is not None:
            cause = FixtureError(f"the test made the fixture database fail {'every query' if taken[0] is None else taken[0]} with {taken[1].__name__}")
            raise failure(self.name, taken[1], cause) from cause
        rows = self.fixtures[name]
        for bind, value in binds.items():
            if bind in rows.columns:
                rows = rows[rows[bind] == value]
        return frames.to_arrow(rows.reset_index(drop=True))

    def _query_of(self, sql: str) -> str:
        if not self._sql:
            self._sql.update({query.sql.read_text(encoding="utf-8"): query.name for query in _queries(self.declared)})
        if (name := self._sql.get(sql)) is None:
            msg = f"the fixture database {self.name} has no query whose SQL is {sql[:200]!r}: load the app's own datasets alone"
            raise FixtureError(msg)
        return name


@dataclass(frozen=True)
class _Statements:
    database: FixtureDatabase

    def query(self, sql: str, binds: Mapping[str, Bind], *, timeout_s: float) -> pa.Table:  # noqa: ARG002 -- the statements' signature; a fixture never waits
        return self.database.answer(sql, binds)

    def execute(self, sql: str, binds: Mapping[str, Bind], *, timeout_s: float) -> int:  # noqa: ARG002 -- the statements' signature
        msg = f"the fixture database {self.database.name} only reads, as a Dash app's databases do"
        raise FixtureError(msg)

    def commit(self) -> None:
        msg = f"the fixture database {self.database.name} only reads, as a Dash app's databases do"
        raise FixtureError(msg)


def database(declared: Database, folder: str | Path = FOLDER) -> FixtureDatabase:
    """A fake of `declared` whose every query answers from `folder/QUERY.csv`, `folder` relative to the repo."""
    if not isinstance(declared, Database):
        msg = f"fixtures.database fakes a database declared with bd.database, not {declared!r}"
        raise DeclarationError(msg)
    where = _in_repo(folder)
    found: dict[str, pd.DataFrame] = {}
    problems: list[str] = []
    for query in _queries(declared):
        path = where / f"{query.name}.csv"
        try:
            content = path.read_bytes()
        except OSError:
            problems.append(f"query {query.name} has no fixture: write {path}, a CSV of {', '.join(query.row.names())}")
            continue
        try:
            found[query.name] = _read(content, query)
        except SchemaError as error:
            problems.append(" ".join(str(error).split()))
    if problems:
        raise DeclarationError("; ".join(problems))
    return FixtureDatabase(declared, where, found)


def folder(path: str | Path) -> Path:
    """A fake of a folder, or of a lake: the folder `path`, relative to the repo, whose files stand in for the shared
    folder's or the lake container's."""
    return _in_repo(path)


def of(app: App) -> dict[str, object]:
    """The fakes `app` declares, `fakes="module:function"`, by resource name, none for an app that reads nothing, or
    `DeclarationError` saying what to fix."""
    reference = app.fakes
    if reference is None and not app.resources:
        return {}
    if reference is None:
        msg = f"app {app.name} declares no fakes to run on: give its bd.App fakes='module:function', a function of the repo's own that returns a fake of each resource, as fixtures.database(WAREHOUSE)"
        raise DeclarationError(msg)
    module, _, attribute = reference.partition(":")
    try:
        made: object = getattr(importlib.import_module(module), attribute)
    except ModuleNotFoundError as error:
        if error.name is None or not (module == error.name or module.startswith(f"{error.name}.")):
            raise  # the module exists, and something it imports does not
        msg = f"app {app.name}'s fakes are {reference}, and no module {module} exists: give fakes='module:function' the module that makes them"
        raise DeclarationError(msg) from error
    except AttributeError as error:
        msg = f"app {app.name}'s fakes are {reference}, and {module} has no {attribute}: define it there, or name the function that makes them"
        raise DeclarationError(msg) from error
    if not inspect.isfunction(made):
        msg = f"app {app.name}'s fakes are {reference}, a {type(made).__name__}: name a function that takes nothing and returns them"
        raise DeclarationError(msg)
    given: object = made()
    if not isinstance(given, Mapping):
        msg = f"{reference} returns a {type(given).__name__}: return a fake of each resource, by resource, as {{WAREHOUSE: fixtures.database(WAREHOUSE)}}"
        raise DeclarationError(msg)
    return {_named(key, reference): fake for key, fake in given.items()}


def _named(key: Hashable, reference: str) -> str:
    if isinstance(key, RESOURCES):
        return key.name
    if isinstance(key, str):
        return key
    msg = f"{reference} returns a fake for {key!r}: key each by the resource it fakes, as WAREHOUSE"
    raise DeclarationError(msg)


def _queries(declared: Database) -> list[Query]:
    return [found for found in datasets([declared]) if isinstance(found, Query) and found.database is declared]


def _read(content: bytes, query: Query) -> pd.DataFrame:
    """A fixture's rows, every field read as text, which the row types, as a real answer is conformed."""
    text = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False, na_filter=False)
    return conform(text, query.row, what=f"fixture {query.name}.csv", naive="in_zone")


def _in_repo(path: str | Path) -> Path:
    """`path`, absolute, or relative to the folder holding the package of the module that called the caller."""
    given = Path(path)
    if given.is_absolute():
        return given
    frame = inspect.currentframe()
    for _ in range(2):
        frame = frame.f_back if frame is not None else None
    name, file = (frame.f_globals.get("__name__"), frame.f_globals.get("__file__")) if frame is not None else (None, None)
    if not isinstance(name, str) or not isinstance(file, str):
        return Path.cwd() / given
    depth = name.count(".") + (1 if Path(file).name == "__init__.py" else 0)
    return Path(file).resolve().parents[depth] / given
