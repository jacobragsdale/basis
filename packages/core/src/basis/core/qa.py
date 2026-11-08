"""The QA tier, which `basis check --full` runs: each part's runs against the QA databases, from the desktop, the one
place without Docker where a repo's code meets a real database before production. It keeps the safeguards of an
end-to-end suite: it refuses to run unless every place its settings point carries the QA marker, and it writes only to the
tables and the store the repo allows. All of it is declared in the repo's `pyproject.toml`, and nothing is inferred:

    [tool.basis.qa]
    settings = ".env.qa"   # the settings file the tier reads, alone: never the process's environment
    marker = "qa"          # what the name of each QA server or database, and of each QA store, contains
    stores = ["//files/runs-qa/pricing"]   # the stores the tier may save runs in; the settings name one of them
    writes = { warehouse = ["daily_positions", "etl.publishes"] }   # by database, the tables it may write: ledgers too

    [tool.basis.qa.job]    # a part's own runs, which the part reads
    positions = { params = "jobs/positions/params/default.json", interval = "2026-11-05..2026-11-06" }

A setting carries the marker when what it points at has a name that contains it, ignoring case:

- a SQL Server database's connection string, and its `WRITE` block's: the one server it names (`Server`, `Data
  Source`, `Address`, `Addr` or `Network Address`), or the one database (`Database` or `Initial Catalog`);
- an Oracle database's DSN, which both its logins use;
- `BASIS_SNAPSHOT_ROOT`, which must be one of `stores`, as written, each of which must carry it.

Every database the settings configure must carry it, whether a run uses it or not, and each that has a `WRITE` block
must have its ledger in `writes`, since every publish writes the ledger. Each part's plan then says what its runs
would write, and while anything is refused, no part runs. The databases the parts get refuse, at the moment of
writing, a table `writes` does not list, and refuse statements of a part's own through a write login outright, since
which tables those write cannot be told. A connection string's login can still reach another database by a
three-part name in a query, which no check of its settings can see: give QA's logins access to QA alone."""

import tomllib
from collections.abc import Callable, Generator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import pandas as pd
from pydantic import ConfigDict, Field, ValidationError

from basis.core import invalid
from basis.core.databases import Counts, Database, LedgerEntry, Reader, Statements, Writer, open_database
from basis.core.destination import TableSpec
from basis.core.errors import BasisError
from basis.core.interval import Interval
from basis.core.model import Model
from basis.core.settings import LEDGER, MssqlSettings, OracleSettings, Settings, locate, read
from basis.core.storage import Root
from basis.core.sync import Problem
from basis.core.tables import IDENTIFIER, Changes, Target
from basis.core.verbs import Part, Qa, QaPlan, QaRun

KEY = "qa"
"""The `[tool.basis]` table that declares the tier."""
_SECTION = f"[tool.basis.{KEY}]"
_EXAMPLE = f'{_SECTION} settings = ".env.qa", marker = "qa", stores = ["//files/runs-qa/NAME"], writes = {{ warehouse = ["TABLE", "etl.publishes"] }}'
_SERVER = ("server", "data source", "address", "addr", "network address")
_DATABASE = ("database", "initial catalog")


class QaError(BasisError, PermissionError):
    """A write the QA tier refuses, raised before anything is written."""


class Declaration(Model):
    """`[tool.basis.qa]`, but for the parts' own tables."""

    model_config = ConfigDict(title=f"pyproject.toml's {_SECTION}")

    settings: Annotated[str, Field(min_length=1)]
    """The settings file, relative to the repo, such as `.env.qa`."""
    marker: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]+$")]
    """What every QA name contains: two or more letters, digits, `_`, `.` or `-`, which no production name contains."""
    stores: Annotated[tuple[str, ...], Field(min_length=1)]
    writes: dict[Annotated[str, Field(pattern=IDENTIFIER.pattern)], tuple[Annotated[str, Field(pattern=LEDGER)], ...]]
    """By database, as the settings name it, each table the tier may write."""


@dataclass(frozen=True)
class Declared:
    """What the repo declares of its QA tier."""

    pyproject: Path
    declaration: Declaration
    parts: dict[str, dict[str, object]] = field(default_factory=dict[str, dict[str, object]])
    """Each part's own table, by the part's name."""

    def settings_file(self, folder: Path) -> Path:
        """The settings file, in `folder`: the repo's, or the clone's own folder for a check in another worktree."""
        return folder / self.declaration.settings

    def tables(self) -> dict[str, frozenset[str]]:
        return {database: frozenset(table.lower() for table in tables) for database, tables in self.declaration.writes.items()}


def declared(pyproject: Path, parts: Mapping[str, Part]) -> Declared | None:
    """The QA tier `pyproject` declares, or None when it declares none. A key basis does not read is refused: one of
    the four, or the table of a declared part that has QA runs."""
    tool: object = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool")
    basis = tool.get("basis") if isinstance(tool, dict) else None
    table = basis.get(KEY) if isinstance(basis, dict) else None
    if table is None:
        return None
    if not isinstance(table, dict):
        msg = f"{pyproject}'s {_SECTION} is not a table: give {_EXAMPLE}"
        raise BasisError(msg)
    entries = {str(key): value for key, value in table.items()}
    runs = {name for name, part in parts.items() if part.qa is not None}
    fields = set(Declaration.model_fields)
    if unknown := sorted(set(entries) - fields - runs):
        reads = f"{', '.join(sorted(fields))}, and a table for each part with QA runs ({', '.join(sorted(runs)) or 'none here'})"
        msg = f"{pyproject}'s {_SECTION} has {', '.join(unknown)}, which basis does not read: remove it; basis reads {reads}"
        raise BasisError(msg)
    own: dict[str, dict[str, object]] = {}
    for name in sorted(runs & set(entries)):
        value = entries[name]
        if not isinstance(value, dict):
            msg = f"{pyproject}'s [tool.basis.{KEY}.{name}] is not a table: give each of the part's runs in it"
            raise BasisError(msg)
        own[name] = {str(key): entry for key, entry in value.items()}
    try:
        declaration = Declaration.model_validate({key: value for key, value in entries.items() if key in fields})
    except ValidationError as error:
        msg = f"{invalid.explain(error)}, as {_EXAMPLE}"
        raise BasisError(msg) from error
    if unmarked := [store for store in declaration.stores if not carries(store, declaration.marker)]:
        msg = f"{pyproject}'s {_SECTION} stores has {', '.join(unmarked)}, whose name does not contain the marker {declaration.marker!r}: list only QA's stores, each named so"
        raise BasisError(msg)
    return Declared(pyproject, declaration, own)


def carries(name: str, marker: str) -> bool:
    return marker.lower() in name.lower()


def keywords(connection_string: str) -> list[tuple[str, str]]:
    """A connection string's keywords and values, in order, each keyword in lower case: `KEY=VALUE` pairs split at
    `;`, where a value in braces, `{...}` with `}}` for a brace, or in double quotes, may hold a `;`. Empty segments
    and segments with no `=` are skipped, so neither hides the keyword after it."""
    pairs: list[tuple[str, str]] = []
    text = connection_string
    while text := text.lstrip("; \t"):  # an empty segment, `;;`, is nothing
        equals, semicolon = text.find("="), text.find(";")
        if equals == -1:
            break
        if semicolon != -1 and semicolon < equals:  # a segment with no `=` is no keyword, and hides none after it
            text = text[semicolon + 1 :]
            continue
        key, text = text[:equals], text[equals + 1 :].lstrip()
        if text[:1] in {"{", '"'}:
            value, text = _quoted(text)
            _, _, text = text.partition(";")
        else:
            value, _, text = text.partition(";")
        pairs.append((key.strip().lower(), value.strip()))
    return pairs


def _quoted(text: str) -> tuple[str, str]:
    """A value in braces or double quotes at the start of `text`, unescaped, and what follows it."""
    close = "}" if text[0] == "{" else '"'
    index, value = 1, list[str]()
    while index < len(text):
        if text[index] == close:
            if text[index + 1 : index + 2] == close:
                value.append(close)
                index += 2
                continue
            return "".join(value), text[index + 1 :]
        value.append(text[index])
        index += 1
    return "".join(value), ""


@dataclass(frozen=True)
class Names:
    """What a SQL Server connection string points at: each server and each database it names, which the QA tier needs
    exactly one and at most one of."""

    servers: tuple[str, ...]
    databases: tuple[str, ...]

    @classmethod
    def of(cls, connection_string: str) -> Names:
        pairs = keywords(connection_string)
        return cls(tuple(value for key, value in pairs if key in _SERVER), tuple(value for key, value in pairs if key in _DATABASE))


def targets(settings: Settings) -> dict[str, Names | str]:
    """What each database's settings point at, by the variable that sets it: a SQL Server connection string's names,
    and an Oracle DSN. The snapshot root is checked against `stores`, each of which carries the marker."""
    found: dict[str, Names | str] = {}
    for name, database in sorted(settings.databases.items()):
        variable = f"BASIS_DATABASES__{name.upper()}"
        match database:
            case MssqlSettings():
                found[f"{variable}__CONNECTION_STRING"] = Names.of(database.connection_string.get_secret_value())
                if database.write is not None:
                    found[f"{variable}__WRITE__CONNECTION_STRING"] = Names.of(database.write.connection_string.get_secret_value())
            case OracleSettings():
                found[f"{variable}__DSN"] = database.dsn
    return found


def problems(settings: Settings, declared: Declared, *, source: Path) -> list[Problem]:
    """Why the settings read from `source` are not safe for the QA tier: a place they point that does not carry the
    marker, a store `stores` does not list, or a ledger `writes` does not. Empty when they are safe."""
    marker = declared.declaration.marker
    where = f"in {source.name}"
    found: list[Problem] = []
    fix = f"point it at QA, or if it is QA's, change {_SECTION} marker to what QA's names contain"
    for variable, target in targets(settings).items():
        if isinstance(target, str):
            if not carries(target, marker):
                found.append((f"{variable} {where} is {target}, which does not contain the QA marker {marker!r}, so the QA tier will not use it", fix))
            continue
        if len(target.servers) != 1 or len(target.databases) > 1:
            named = f"{len(target.servers)} servers and {len(target.databases)} databases"
            found.append((f"{variable} {where} names {named}, and the QA tier needs one server and at most one database to know where it points", "give each once"))
        elif not any(carries(name, marker) for name in [*target.servers, *target.databases]):
            named = f"server {target.servers[0]}" + "".join(f" and database {name}" for name in target.databases)
            found.append((f"{variable} {where} names {named}, and neither contains the QA marker {marker!r}, so the QA tier will not use it", fix))
    stores = declared.declaration.stores
    if settings.snapshot_root is None:
        found.append((f"{source.name} sets no BASIS_SNAPSHOT_ROOT, the QA store the tier saves its runs in", f"set it to one of {_SECTION} stores: {', '.join(stores)}"))
    elif settings.snapshot_root not in stores:
        found.append((f"BASIS_SNAPSHOT_ROOT {where} is {settings.snapshot_root}, which {_SECTION} stores does not list", f"set it to one of {', '.join(stores)}, or list it there if it is QA's"))
    tables = declared.tables()
    for name, database in sorted(settings.databases.items()):
        if database.write is not None and database.write.ledger is not None and database.write.ledger.lower() not in tables.get(name, frozenset()):
            variable = f"BASIS_DATABASES__{name.upper()}__WRITE__LEDGER"
            reason = f"{variable} {where} is {database.write.ledger}, which {_SECTION} writes does not list for {name}, and every publish writes it"
            found.append((reason, f"add it to writes.{name} if it is QA's ledger"))
    return found


def store(settings: Settings, folder: Path) -> Root | Problem:
    """The QA store the settings name, which a relative path finds in `folder`, as the settings file is; or why it
    cannot be opened, and the fix."""
    if settings.snapshot_root is None:
        return "the QA settings name no store", "set BASIS_SNAPSHOT_ROOT in them to one of [tool.basis.qa] stores"
    location = locate(settings.snapshot_root, folder)
    try:
        return Root.at(location)
    except (OSError, ValueError) as error:  # pyarrow's errors for a store it cannot open are OSError and ValueError
        return f"the QA store {location} cannot be opened ({' '.join(str(error).split())})", "fix BASIS_SNAPSHOT_ROOT in the QA settings, or reach the store from this machine"


def context(declared: Declared, settings: Settings, *, store: Root, repo: Path, commit: str | None, part: str) -> Qa:  # noqa: PLR0913 -- what the tier found, by keyword
    """What the tier gives the part `part`: settings `problems` found safe, the QA store, and databases that refuse
    what `writes` does not list."""
    tables = declared.tables()

    def databases(name: str) -> Database:
        return _Guarded(open_database(settings, name), tables.get(name, frozenset()))

    return Qa(repo, commit, settings, store, tables, declared.parts.get(part, {}), databases)


@dataclass(frozen=True)
class Outcome:
    """What the QA tier did."""

    skipped: str | None = None
    """Why it did not run, when it did not."""
    refused: tuple[Problem, ...] = ()
    """Each reason it ran nothing."""
    runs: tuple[tuple[str, QaRun], ...] = ()
    """Each run it made, with its part's name."""
    store: str | None = None

    @property
    def failures(self) -> list[Problem]:
        return [*self.refused, *(run.failure for _, run in self.runs if run.failure is not None)]


def tier(repo: Path, parts: Mapping[str, Part], *, commit: str | None, folder: Path) -> Outcome:
    """Run each part's QA runs, where `repo` declares the tier and its settings file is in `folder` (the repo, or the
    clone's own folder for a check in another worktree), once every part's plan and every setting is safe."""
    found = declared(repo / "pyproject.toml", parts)
    if found is None:
        return Outcome(skipped=f"pyproject.toml declares no {_SECTION}: declare one to run the repo's parts against QA, as `uv run basis docs reference` shows")
    planning = {name: part.qa for name, part in parts.items() if part.qa is not None}
    if not planning:
        return Outcome(skipped=f"no part of this repo ({', '.join(parts)}) has QA runs")
    source = found.settings_file(folder)
    if not source.is_file():
        return Outcome(skipped=f"this machine has no {source}, the settings file {_SECTION} names: copy .env.example to it, and fill in QA's databases and store")
    return _safely(found, read(source), planning, source=source, repo=repo, folder=folder, commit=commit)


def _safely(  # noqa: PLR0913 -- what the tier found, by keyword
    found: Declared, settings: Settings, planning: Mapping[str, Callable[[Qa], QaPlan]], *, source: Path, repo: Path, folder: Path, commit: str | None
) -> Outcome:
    """Each part's runs, once the settings read from `source` are safe, the store opens, and no plan is refused."""
    if refused := problems(settings, found, source=source):
        return Outcome(refused=tuple(refused))
    opened = store(settings, folder)
    if not isinstance(opened, Root):
        return Outcome(refused=(opened,))
    plans = {name: plan(context(found, settings, store=opened, repo=repo, commit=commit, part=name)) for name, plan in planning.items()}
    if refused := [problem for plan in plans.values() for problem in plan.problems]:
        return Outcome(refused=tuple(refused), store=settings.snapshot_root)
    return Outcome(runs=tuple((name, run) for name, plan in plans.items() for run in plan.run()), store=settings.snapshot_root)


@dataclass(frozen=True)
class _Guarded:
    """A database whose writer refuses, before it writes, a table `tables` does not list, and which refuses statements
    of a part's own through the write login, since no table a statement writes can be told from it."""

    database: Database
    tables: frozenset[str]

    @property
    def name(self) -> str:
        return self.database.name

    @property
    def writable(self) -> bool:
        return self.database.writable

    def reading(self) -> AbstractContextManager[Reader]:
        return self.database.reading()

    @contextmanager
    def writing(self) -> Generator[Writer]:
        with self.database.writing() as writer:
            yield _GuardedWriter(writer, self.name, self.tables)

    def statements(self, *, writes: bool) -> AbstractContextManager[Statements]:
        if writes:
            msg = f"the QA tier refused statements of the part's own through {self.name}'s write login, since it cannot tell which tables they write: write through the part's declared outputs"
            raise QaError(msg)
        return self.database.statements(writes=False)


@dataclass(frozen=True)
class _GuardedWriter:
    writer: Writer
    database: str
    tables: frozenset[str]

    def fetch(self, sql: str, interval: Interval | None, *, what: str) -> pd.DataFrame:
        return self.writer.fetch(sql, interval, what=what)

    def describe(self, table: str) -> TableSpec | None:
        return self.writer.describe(table)

    def published(self, run_id: str) -> bool:
        return self.writer.published(run_id)

    def overlapping(self, job: str, output: str, interval: Interval) -> list[str]:
        return self.writer.overlapping(job, output, interval)

    def apply(self, target: Target, changes: Changes, spec: TableSpec) -> Counts:
        if target.table.lower() not in self.tables:
            msg = f"the QA tier refused to write {target.table} in {self.database}, which {_SECTION} writes does not list: add it to writes.{self.database} if it is QA's"
            raise QaError(msg)
        return self.writer.apply(target, changes, spec)

    def record(self, entry: LedgerEntry) -> None:
        self.writer.record(entry)

    def commit(self) -> None:
        self.writer.commit()
