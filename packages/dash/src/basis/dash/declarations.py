"""What a Dash app reads, each declared once, by name, with everything stated and nothing guessed:

    class Position(bd.Row):
        account: str
        par: Annotated[float, bd.format.money("USD", 0)]


    WAREHOUSE = bd.database("warehouse", dialect="mssql", deadline_s=30, max_concurrent=4)
    POSITIONS = bd.query("positions", WAREHOUSE, "sql/positions.sql", Position, binds={"as_of": date}, ttl_s=900, stale_s=86_400)
    SECTORS = bd.view("sectors", domain.by_sector, Sector, positions=POSITIONS)

| Declared with | Reads | Configured by |
|---|---|---|
| `database(name, dialect=, deadline_s=, max_concurrent=)` | a SQL database, through its read login alone | basis-core's `BASIS_DATABASES__NAME__*` |
| `folder(name)` | a shared folder | basis-core's `BASIS_FOLDERS__NAME__PATH` |
| `query(name, database, sql, Row, binds=, ttl_s=, stale_s=)` | the rows a SQL file selects, binding only `binds` | its database |
| `file(name, folder, path, Row, ttl_s=, stale_s=)` | a CSV or Parquet file in a folder | its folder |
| `view(name, function, Row, **inputs)` | a pure function's frame, of datasets and params | its inputs |

- **A dataset's row** is a `bd.Row`, basis-core's, whose refusals name the fix in `bd.` words; every frame a dataset
  gives is conformed to it.
- **A query binds only what it declares**: `binds` names each param and its type (`str`, `float`, `int`, `bool` or
  `date`), and nothing else, so no list, and no `IN (...)` built from input. The SQL file is in its database's
  declared dialect, Oracle's `:as_of` or SQL Server's `%(as_of)s`. Building the app opens every file and checks its
  binds against the declared params, before any environment is read (`check`): a file that is missing, binds a name
  it does not declare, leaves a declared param unbound, or binds in the other dialect's form, is refused.
- **Freshness is declared, never assumed**: a query and a file state `ttl_s`, how long a value is served before it is
  read again, and `stale_s`, how much longer a value past it may still be shown when a reload fails. There is no
  default for either, and a dataset that does not say is refused where it is declared, with the fix. A view's
  freshness is its inputs'.
- **A view's params** are its inputs' and its function's own: the function takes each input's frame by the input's
  name, and any other parameter is a param, annotated with its type. A param two of them name has one type.
- **An app reads**: a database gives a reader inside a transaction that only reads, and no writer.
- **An app's datasets** are those every resource of which the app lists in `resources`; two of them never share a
  name. Each dataset is declared once in a module an app's code imports, which reads and opens nothing: the SQL file
  is read when the app is built."""

import inspect
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath
from typing import ClassVar, get_type_hints, override

from basis.core import resources
from basis.core import rows as core_rows
from basis.core.dialects import Dialect, named_binds, of
from basis.core.jsontext import dumps as json_text
from basis.core.paths import OutsideError, inside
from basis.core.rows import Json, MaxLength, Zone
from basis.dash.errors import DeclarationError

__all__ = ["Database", "Dataset", "File", "Folder", "Json", "MaxLength", "Query", "Row", "View", "Zone", "check", "database", "datasets", "file", "folder", "json_text", "query", "view"]


class Row(core_rows.Row, spelling="bd"):
    """Base for a dataset's columns, basis-core's `Row`, whose refusals name the fix as a Dash app spells it, as
    `bd.Zone('UTC')`: subclass it and annotate one attribute per column."""


BIND_TYPES: tuple[type, ...] = (str, float, int, bool, date)
"""The types a param may have: what a query binds, and a cache key holds."""
DIALECTS: tuple[Dialect, ...] = ("mssql", "oracle")
FILES = (".csv", ".parquet")
"""The files `file` reads, by their suffix."""


@dataclass(frozen=True, eq=False)
class Database:
    name: str
    dialect: Dialect
    """The dialect its SQL files are written in, which its settings' `kind` must be."""
    deadline_s: float
    """Seconds each query may take, from the moment a thread is free to run it."""
    max_concurrent: int
    """The most queries it runs at once, each on a thread of its own: the rest wait their turn."""


@dataclass(frozen=True, eq=False)
class Folder:
    name: str


type Resource = Database | Folder


@dataclass(frozen=True, eq=False, kw_only=True)
class Dataset:
    """A dataset an app loads by name, with `bd.load(DATASET, **params)`."""

    kind: ClassVar[str] = "dataset"
    name: str
    row: type[core_rows.Row]
    params: Mapping[str, type]
    """Each param a load gives, by name, with its type."""
    module: str = field(compare=False)
    """The module that declared it."""

    def __post_init__(self) -> None:
        _declared.append(self)

    def resources(self) -> tuple[Resource, ...]:
        """What it reads, directly or through its inputs."""
        raise NotImplementedError


@dataclass(frozen=True, eq=False, kw_only=True)
class Query(Dataset):
    kind: ClassVar[str] = "query"
    database: Database
    sql: Path
    """The SQL file, absolute: declared relative to the folder of the module that declared it."""
    ttl_s: float
    stale_s: float

    @override
    def resources(self) -> tuple[Resource, ...]:
        return (self.database,)


@dataclass(frozen=True, eq=False, kw_only=True)
class File(Dataset):
    kind: ClassVar[str] = "file"
    folder: Folder
    path: str
    """The file's path inside its folder."""
    ttl_s: float
    stale_s: float

    @override
    def resources(self) -> tuple[Resource, ...]:
        return (self.folder,)


@dataclass(frozen=True, eq=False, kw_only=True)
class View(Dataset):
    kind: ClassVar[str] = "view"
    function: Callable[..., object]
    inputs: Mapping[str, Dataset]
    """Each input, by the name of the function's parameter that takes its frame."""

    @override
    def resources(self) -> tuple[Resource, ...]:
        found: dict[int, Resource] = {}
        for given in self.inputs.values():
            found.update((id(resource), resource) for resource in given.resources())
        return tuple(found.values())


_declared: list[Dataset] = []
"""Every dataset declared in the process, in order, which an app's are found among."""


def database(name: str, *, dialect: Dialect, deadline_s: float, max_concurrent: int) -> Database:
    """A SQL database, whose settings are basis-core's `BASIS_DATABASES__NAME__*`, read through its read login alone, in
    `dialect`, each query within `deadline_s`, and at most `max_concurrent` at once."""
    named = _named(name, "database")
    if dialect not in DIALECTS:
        msg = f"database {name}'s dialect is {dialect!r}: give dialect='mssql' or dialect='oracle', the dialect its SQL files are written in"
        raise DeclarationError(msg)
    if isinstance(max_concurrent, bool) or not isinstance(max_concurrent, int) or max_concurrent < 1:
        msg = f"database {name} runs at most {max_concurrent!r} queries at once: give it max_concurrent, a whole number of 1 or more"
        raise DeclarationError(msg)
    try:
        seconds = resources.seconds(name, deadline_s)
    except resources.ResourceError as error:
        raise DeclarationError(str(error)) from error
    return Database(named, dialect, seconds, max_concurrent)


def folder(name: str) -> Folder:
    """A shared folder, whose path on each machine is basis-core's setting `BASIS_FOLDERS__NAME__PATH`."""
    return Folder(_named(name, "folder"))


def query(name: str, database: Database, sql: str, row: type[core_rows.Row], *, binds: Mapping[str, type] | None = None, ttl_s: float | None = None, stale_s: float | None = None) -> Query:
    """The rows the SQL file `sql`, relative to the folder of the module that declares it, selects from `database`,
    binding each of `binds`, as `{"as_of": date}`, and nothing else, as `row`'s columns."""
    named = _named(name, "query")
    if not isinstance(database, Database):
        msg = f"query {name} reads {database!r}: give the database it reads, declared with bd.database(NAME, ...)"
        raise DeclarationError(msg)
    if not isinstance(sql, str) or PurePosixPath(sql).suffix != ".sql":
        msg = f"query {name} reads {sql!r}: give its SQL file, relative to the module that declares it, as sql/{name}.sql"
        raise DeclarationError(msg)
    ttl, stale = _freshness(name, ttl_s, stale_s)
    return Query(name=named, row=_row(name, row), params=_params(name, binds or {}), module=_caller(), database=database, sql=_caller_folder() / sql, ttl_s=ttl, stale_s=stale)


def file(name: str, folder: Folder, path: str, row: type[core_rows.Row], *, ttl_s: float | None = None, stale_s: float | None = None) -> File:
    """The CSV or Parquet file at `path` inside `folder`, as `row`'s columns: a CSV's every field read as text, which
    the row types, so nothing is inferred."""
    named = _named(name, "file")
    if not isinstance(folder, Folder):
        msg = f"file {name} reads from {folder!r}: give the folder it is in, declared with bd.folder(NAME)"
        raise DeclarationError(msg)
    try:
        inside(path, folder.name)
    except OutsideError as error:
        msg = f"file {name}: {error}"
        raise DeclarationError(msg) from error
    if PurePosixPath(path).suffix.lower() not in FILES:
        msg = f"file {name} reads {path!r}: give a file it reads, whose name ends {' or '.join(FILES)}"
        raise DeclarationError(msg)
    ttl, stale = _freshness(name, ttl_s, stale_s)
    return File(name=named, row=_row(name, row), params={}, module=_caller(), folder=folder, path=path, ttl_s=ttl, stale_s=stale)


def view(name: str, function: Callable[..., object], row: type[core_rows.Row], /, **inputs: Dataset) -> View:
    """The frame `function` makes of `inputs`, each a dataset, by the name of the parameter that takes its frame, and of
    its other parameters, each a param annotated with its type; as `row`'s columns."""
    named = _named(name, "view")
    if not callable(function):
        msg = f"view {name} computes with {function!r}: give a function of its inputs' frames, as domain.by_sector"
        raise DeclarationError(msg)
    if not inputs or not all(isinstance(given, Dataset) for given in inputs.values()):
        msg = f"view {name} has no inputs, or one that is not a dataset: give each, as positions=POSITIONS"
        raise DeclarationError(msg)
    params: dict[str, type] = {}
    for given in inputs.values():
        for param, kind in given.params.items():
            _one_type(name, param, params.setdefault(param, kind), kind)
    for param, kind in _own_params(name, function, set(inputs)).items():
        _one_type(name, param, params.setdefault(param, kind), kind)
    return View(name=named, row=_row(name, row), params=params, module=_caller(), function=function, inputs=dict(inputs))


def datasets(resources_: Sequence[object]) -> list[Dataset]:
    """The datasets declared in the process every resource of which is one of `resources_`, by identity: an app's."""
    held = {id(resource) for resource in resources_}
    return [dataset for dataset in _declared if all(id(resource) in held for resource in dataset.resources())]


def check(app_name: str, resources_: Sequence[object]) -> list[Dataset]:
    """An app's resources and datasets, checked as the app is built, before any environment is read: each resource is
    one `bd` declares, named once of its kind; its datasets are named once; and each query's SQL file exists and binds
    exactly its declared params, in its database's dialect. The app's datasets, or `DeclarationError` naming every
    problem."""
    problems: list[str] = []
    seen: dict[tuple[type, str], int] = {}
    for resource in resources_:
        if not isinstance(resource, Database | Folder):
            problems.append(f"app {app_name} lists {resource!r} in its resources: list what it reads, each declared with bd.database or bd.folder")
            continue
        seen[type(resource), resource.name] = seen.get((type(resource), resource.name), 0) + 1
    problems.extend(f"app {app_name} lists more than one {kind.__name__.lower()} named {name}: declare each once" for (kind, name), count in seen.items() if count > 1)
    found = datasets(resources_)
    names = [dataset.name for dataset in found]
    problems.extend(f"app {app_name} has more than one dataset named {name}: give each its own name" for name in sorted({name for name in names if names.count(name) > 1}))
    problems.extend(problem for dataset in found if isinstance(dataset, Query) for problem in _sql_problems(dataset))
    if problems:
        raise DeclarationError("; ".join(problems))
    return found


def _sql_problems(declared: Query) -> list[str]:
    try:
        text = declared.sql.read_text(encoding="utf-8")
    except OSError:
        return [f"query {declared.name} reads {declared.sql}, which cannot be read: write it, or give its path relative to {declared.module}'s folder"]
    dialect = declared.database.dialect
    bound = named_binds(text)
    problems: list[str] = []
    for other, names in bound.items():
        if other != dialect:
            problems.append(
                f"query {declared.name} binds {', '.join(sorted(names))} in {of(other).title}'s form, and database {declared.database.name} is {of(dialect).title}:"
                f" write each as {of(dialect).bind('NAME')}"
            )
    names = bound.get(dialect, set())
    if extra := sorted(names - set(declared.params)):
        problems.append(f"query {declared.name} binds {', '.join(extra)}, which it does not declare: declare each in binds=, with its type, or filter in a view")
    if unbound := sorted(set(declared.params) - names):
        problems.append(f"query {declared.name} declares {', '.join(unbound)}, which its SQL never binds: bind each, as {of(dialect).bind(unbound[0])}, or leave it out of binds=")
    return problems


def _named(name: str, kind: str) -> str:
    if not isinstance(name, str) or not resources.NAME.fullmatch(name):
        msg = f"{kind} {name!r}: a name is lower-case letters, digits and underscores, starting with a letter, as positions"
        raise DeclarationError(msg)
    return name


def _row(name: str, row: object) -> type[core_rows.Row]:
    if not (isinstance(row, type) and issubclass(row, core_rows.Row) and row.columns):
        msg = f"{name}'s row is {row!r}: give a class of its columns, declared as class Position(bd.Row)"
        raise DeclarationError(msg)
    return row


def _freshness(name: str, ttl_s: float | None, stale_s: float | None) -> tuple[float, float]:
    if ttl_s is None or stale_s is None:
        msg = (
            f"{name} does not say how fresh it must be: give ttl_s, the seconds a value is served before it is read again,"
            " and stale_s, the seconds past that it may still be shown when a reload fails, as ttl_s=900, stale_s=86_400"
        )
        raise DeclarationError(msg)
    for what, value, least in (("ttl_s", ttl_s, "above 0"), ("stale_s", stale_s, "of 0 or more")):
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0 or (what == "ttl_s" and value == 0):
            msg = f"{name}'s {what} is {value!r}: give it seconds {least}"
            raise DeclarationError(msg)
    return float(ttl_s), float(stale_s)


def _params(name: str, binds: Mapping[str, type]) -> dict[str, type]:
    params: dict[str, type] = {}
    for param, kind in binds.items():
        if not isinstance(param, str) or not resources.NAME.fullmatch(param):
            msg = f"{name} binds {param!r}: a param's name is lower-case letters, digits and underscores, starting with a letter"
            raise DeclarationError(msg)
        params[param] = _bind_type(name, param, kind)
    return params


def _bind_type(name: str, param: str, kind: object) -> type:
    if not (isinstance(kind, type) and kind in BIND_TYPES):
        msg = f"{name}'s param {param} is {kind!r}: give one of {', '.join(found.__name__ for found in BIND_TYPES)}, one value each, never a list"
        raise DeclarationError(msg)
    return kind


def _own_params(name: str, function: Callable[..., object], inputs: set[str]) -> dict[str, type]:
    signature = inspect.signature(function)
    taken = {parameter.name for parameter in signature.parameters.values()}
    if missing := sorted(inputs - taken):
        msg = f"view {name}'s function takes no parameter {', '.join(missing)}: name a parameter after each input, which takes its frame"
        raise DeclarationError(msg)
    try:
        hints = get_type_hints(function)
    except (NameError, TypeError) as error:
        msg = f"view {name}'s function's annotations cannot be read ({error}): annotate each parameter with a type it imports"
        raise DeclarationError(msg) from error
    own: dict[str, type] = {}
    for parameter in signature.parameters.values():
        if parameter.kind in {parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD, parameter.POSITIONAL_ONLY}:
            msg = f"view {name}'s function takes {parameter}: give it named parameters alone, one per input and one per param"
            raise DeclarationError(msg)
        if parameter.name in inputs:
            continue
        if parameter.name not in hints:
            msg = f"view {name}'s function takes {parameter.name}, which is no input: annotate it with its type, a param, or give it an input of that name"
            raise DeclarationError(msg)
        own[parameter.name] = _bind_type(name, parameter.name, hints[parameter.name])
    return own


def _one_type(name: str, param: str, first: type, then: type) -> None:
    if first is not then:
        msg = f"view {name}'s param {param} is a {first.__name__} in one input and a {then.__name__} in another: give it one type"
        raise DeclarationError(msg)


def _caller() -> str:
    """The module that called the declaring function: past it and this one."""
    frame = inspect.currentframe()
    for _ in range(2):
        frame = frame.f_back if frame is not None else None
    name = frame.f_globals.get("__name__") if frame is not None else None
    return name if isinstance(name, str) else "__main__"


def _caller_folder() -> Path:
    """The folder of the module that called the declaring function, which its SQL file's path is relative to."""
    frame = inspect.currentframe()
    for _ in range(2):
        frame = frame.f_back if frame is not None else None
    found = frame.f_globals.get("__file__") if frame is not None else None
    return Path(found).resolve().parent if isinstance(found, str) else Path.cwd()
