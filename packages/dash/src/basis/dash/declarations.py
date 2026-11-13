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
| `lake(name)` | a lake's container | basis-core's `BASIS_LAKES__NAME__*` |
| `http(name, deadline_s=)` | another service, answering JSON | basis-core's `BASIS_HTTP__NAME__*`, its login among them |
| `query(name, database, sql, Row, binds=, ttl_s=, stale_s=)` | the rows a SQL file selects, binding only `binds` | its database |
| `file(name, source, path, Row, sheet=, binds=, ttl_s=, stale_s=)` | a CSV, Parquet or a workbook's sheet in a folder, or Parquet in a lake, its path naming its params | its folder or lake |
| `fetch(name, service, path, Row, binds=, rows_at=, ttl_s=, stale_s=)` | the JSON rows a GET answers, its params in the query | its service |
| `view(name, function, Row, **inputs)` | a pure function's frame, of datasets and params | its inputs |

- **A dataset's row** is a `bd.Row`, basis-core's, whose refusals name the fix in `bd.` words; every frame a dataset
  gives is conformed to it.
- **A query binds only what it declares**: `binds` names each param and its type (`str`, `float`, `int`, `bool` or
  `date`), and nothing else, so no list, and no `IN (...)` built from input. The SQL file is in its database's
  declared dialect, Oracle's `:as_of` or SQL Server's `%(as_of)s`. Building the app opens every file and checks its
  binds against the declared params, before any environment is read (`check`): a file that is missing, binds a name
  it does not declare, leaves a declared param unbound, or binds in the other dialect's form, is refused.
- **Freshness is declared, never assumed**: a query, a file and a fetch state `ttl_s`, how long a value is served before it is
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
import string
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
"""The files `file` reads from a folder, by their suffix, besides workbooks."""
WORKBOOKS = (".xlsx", ".xlsm", ".xlsb", ".xls")
"""The workbooks `file` reads a sheet of, from a folder, as `basis.core.folders.EXCEL` lists them, with `basis-core[excel]`."""


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


@dataclass(frozen=True, eq=False)
class Lake:
    name: str


@dataclass(frozen=True, eq=False)
class Service:
    name: str
    deadline_s: float
    """Seconds each call may take, from the first byte sent to the last byte of the reply."""


type Resource = Database | Folder | Lake | Service
type Example = Mapping[str, object] | Callable[[], Mapping[str, object]]
"""A dataset's smoke example: the params `basis smoke --env ENV` loads it with, or a function that gives them, as
`lambda: {"as_of": bd.day("previous")}`, for a date that moves."""
RESOURCES = (Database, Folder, Lake, Service)


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
    smoke: Example | None = field(default=None, compare=False)
    """The params `basis smoke --env ENV` loads it with, in that environment, or None where it loads it not at all."""

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
    reload_s: float | None = None
    """The fewest seconds between two reloads a person asks for (`bd.reload`), or None where none may be asked for."""

    @override
    def resources(self) -> tuple[Resource, ...]:
        return (self.database,)


@dataclass(frozen=True, eq=False, kw_only=True)
class File(Dataset):
    kind: ClassVar[str] = "file"
    folder: Folder | Lake
    path: str
    """The file's path inside its folder or lake, each `{param}` in it a param's value, a date as ISO text."""
    sheet: str | None = None
    """The sheet of a workbook it reads, or None for a CSV or Parquet file."""
    ttl_s: float
    stale_s: float
    reload_s: float | None = None
    """The fewest seconds between two reloads a person asks for (`bd.reload`), or None where none may be asked for."""

    @override
    def resources(self) -> tuple[Resource, ...]:
        return (self.folder,)


@dataclass(frozen=True, eq=False, kw_only=True)
class Fetch(Dataset):
    kind: ClassVar[str] = "fetch"
    service: Service
    path: str
    """The service's own path, after its base URL; each param is sent in the query, by its name."""
    rows_at: str | None
    """The dotted path to the list of rows in the reply, as `result.items`, or None where the reply is the list."""
    ttl_s: float
    stale_s: float
    reload_s: float | None = None

    @override
    def resources(self) -> tuple[Resource, ...]:
        return (self.service,)


@dataclass(frozen=True, eq=False, kw_only=True)
class View(Dataset):
    kind: ClassVar[str] = "view"
    function: Callable[..., object]
    inputs: Mapping[str, Dataset]
    """Each input, by the name of the function's parameter that takes its frame."""
    arguments: tuple[str, ...]
    """The params the function takes, by name: every parameter of its that is no input."""

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


def query(
    name: str,
    database: Database,
    sql: str,
    row: type[core_rows.Row],
    *,
    binds: Mapping[str, type] | None = None,
    ttl_s: float | None = None,
    stale_s: float | None = None,
    reload_s: float | None = None,
    smoke: Example | None = None,
) -> Query:
    """The rows the SQL file `sql`, relative to the folder of the module that declares it, selects from `database`,
    binding each of `binds`, as `{"as_of": date}`, and nothing else, as `row`'s columns. `reload_s` lets a person ask
    for it again (`bd.reload`), at most once in that many seconds. `smoke` is the params `basis smoke --env ENV` loads
    it with."""
    named = _named(name, "query")
    if not isinstance(database, Database):
        msg = f"query {name} reads {database!r}: give the database it reads, declared with bd.database(NAME, ...)"
        raise DeclarationError(msg)
    if not isinstance(sql, str) or PurePosixPath(sql).suffix != ".sql":
        msg = f"query {name} reads {sql!r}: give its SQL file, relative to the module that declares it, as sql/{name}.sql"
        raise DeclarationError(msg)
    ttl, stale = _freshness(name, ttl_s, stale_s)
    params = _params(name, binds or {})
    return Query(
        name=named,
        row=_row(name, row),
        params=params,
        module=_caller(),
        smoke=_smoke(name, params, smoke),
        database=database,
        sql=_caller_folder() / sql,
        ttl_s=ttl,
        stale_s=stale,
        reload_s=_reload(name, reload_s),
    )


def file(
    name: str,
    source: Folder | Lake,
    path: str,
    row: type[core_rows.Row],
    *,
    sheet: str | None = None,
    binds: Mapping[str, type] | None = None,
    ttl_s: float | None = None,
    stale_s: float | None = None,
    reload_s: float | None = None,
    smoke: Example | None = None,
) -> File:
    """The file at `path` inside `source`, a folder or a lake, as `row`'s columns: a CSV's every field read as text,
    which the row types, so nothing is inferred; a Parquet file, or a lake's folder of them, as it is typed; or a
    workbook's `sheet`, from a folder, each cell as text. Each `{param}` in `path` is one of `binds`, as
    `marks/{as_of}.csv`."""
    named = _named(name, "file")
    if not isinstance(source, Folder | Lake):
        msg = f"file {name} reads from {source!r}: give the folder or lake it is in, declared with bd.folder(NAME) or bd.lake(NAME)"
        raise DeclarationError(msg)
    try:
        inside(path, source.name)
    except OutsideError as error:
        msg = f"file {name}: {error}"
        raise DeclarationError(msg) from error
    params = _params(name, binds or {})
    named_in_path = _placeholders(name, path)
    if named_in_path != set(params):
        names, bound = ", ".join(sorted(named_in_path)) or "no param", ", ".join(sorted(params)) or "none"
        msg = f"file {name}'s path names {names}, and it binds {bound}: name each param in its path once, as {{as_of}}, and bind each it names"
        raise DeclarationError(msg)
    suffix = PurePosixPath(path).suffix.lower()
    if isinstance(source, Lake) and suffix not in {".parquet", ""}:
        msg = f"file {name} reads {path!r} from lake {source.name}: give a Parquet file, or a folder of them"
        raise DeclarationError(msg)
    if isinstance(source, Folder) and suffix not in FILES + WORKBOOKS:
        msg = f"file {name} reads {path!r}: give a file it reads, whose name ends {', '.join(FILES + WORKBOOKS)}"
        raise DeclarationError(msg)
    if (suffix in WORKBOOKS) != (sheet is not None):
        msg = f"file {name} reads {path!r}: name the sheet of a workbook it reads, as sheet='Positions', and no sheet of any other file"
        raise DeclarationError(msg)
    ttl, stale = _freshness(name, ttl_s, stale_s)
    return File(
        name=named,
        row=_row(name, row),
        params=params,
        module=_caller(),
        smoke=_smoke(name, params, smoke),
        folder=source,
        path=path,
        sheet=sheet,
        ttl_s=ttl,
        stale_s=stale,
        reload_s=_reload(name, reload_s),
    )


def lake(name: str) -> Lake:
    """A lake's container, whose settings are basis-core's `BASIS_LAKES__NAME__*`."""
    return Lake(_named(name, "lake"))


def http(name: str, *, deadline_s: float) -> Service:
    """Another HTTP service that answers in JSON, whose base URL is basis-core's setting `BASIS_HTTP__NAME__URL`, with
    its login, CA bundle and proxy beside it, each call within `deadline_s`."""
    named = _named(name, "http")
    try:
        seconds = resources.seconds(name, deadline_s)
    except resources.ResourceError as error:
        raise DeclarationError(str(error)) from error
    return Service(named, seconds)


def fetch(
    name: str,
    service: Service,
    path: str,
    row: type[core_rows.Row],
    *,
    binds: Mapping[str, type] | None = None,
    rows_at: str | None = None,
    ttl_s: float | None = None,
    stale_s: float | None = None,
    reload_s: float | None = None,
    smoke: Example | None = None,
) -> Fetch:
    """The rows `service` answers a GET of `path` with, a JSON list of objects, one per row, each of `row`'s columns
    by its name, read as text, as a CSV's is, and typed by the row; `rows_at` names the list where the reply is an
    object that holds it, as `result.items`. Each of `binds` is sent in the query, by its name, a date as ISO text."""
    named = _named(name, "fetch")
    if not isinstance(service, Service):
        msg = f"fetch {name} reads from {service!r}: give the service it calls, declared with bd.http(NAME, deadline_s=...)"
        raise DeclarationError(msg)
    if not isinstance(path, str) or not path.startswith("/") or "?" in path or "#" in path or "{" in path:
        msg = f"fetch {name}'s path is {path!r}: give the service's own path, after its base URL, as /positions, with no query: its params are sent in the query"
        raise DeclarationError(msg)
    if rows_at is not None and not all(part.isidentifier() for part in rows_at.split(".")):
        msg = f"fetch {name}'s rows_at is {rows_at!r}: give the dotted path to the list of rows in the reply, as result.items"
        raise DeclarationError(msg)
    ttl, stale = _freshness(name, ttl_s, stale_s)
    params = _params(name, binds or {})
    return Fetch(
        name=named,
        row=_row(name, row),
        params=params,
        module=_caller(),
        smoke=_smoke(name, params, smoke),
        service=service,
        path=path,
        rows_at=rows_at,
        ttl_s=ttl,
        stale_s=stale,
        reload_s=_reload(name, reload_s),
    )


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
    own = _own_params(name, function, set(inputs))
    for param, kind in own.items():
        _one_type(name, param, params.setdefault(param, kind), kind)
    return View(name=named, row=_row(name, row), params=params, module=_caller(), function=function, inputs=dict(inputs), arguments=tuple(own))


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
        if not isinstance(resource, RESOURCES):
            problems.append(f"app {app_name} lists {resource!r} in its resources: list what it reads, each declared with bd.database, bd.folder, bd.lake or bd.http")
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


def _placeholders(name: str, path: str) -> set[str]:
    try:
        parts = list(string.Formatter().parse(path))
    except ValueError as error:
        msg = f"file {name}'s path {path!r} cannot be read ({error}): name each param as {{as_of}}"
        raise DeclarationError(msg) from error
    found = [field for _, field, spec, conversion in parts if field is not None]
    if any(not field or spec or conversion for _, field, spec, conversion in parts if field is not None) or len(found) != len(set(found)):
        msg = f"file {name}'s path {path!r} names a param with a format, none, or more than once: name each once, as {{as_of}}"
        raise DeclarationError(msg)
    return set(found)


def _smoke(name: str, params: Mapping[str, type], smoke: object) -> Example | None:
    """A dataset's smoke example, which names each of its params, and no other; or a function that gives them, which
    `basis smoke` calls."""
    if smoke is None or callable(smoke):
        return smoke  # ty: ignore[invalid-return-type] -- a function of no arguments, which basis smoke calls and checks what it gives
    if not isinstance(smoke, Mapping) or set(smoke) != set(params):
        wanted = ", ".join(f"{param}=..." for param in params) or "nothing"
        msg = f"{name}'s smoke example is {smoke!r}: give the params basis smoke loads it with, each of its own and no other, as smoke={{{wanted}}}, or a function that returns them"
        raise DeclarationError(msg)
    return {str(param): value for param, value in smoke.items()}


def _reload(name: str, reload_s: float | None) -> float | None:
    if reload_s is not None and (isinstance(reload_s, bool) or not isinstance(reload_s, int | float) or not math.isfinite(reload_s) or reload_s <= 0):
        msg = f"{name}'s reload_s is {reload_s!r}: give the fewest seconds between two reloads a person asks for, above 0"
        raise DeclarationError(msg)
    return None if reload_s is None else float(reload_s)


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
