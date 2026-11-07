"""SQL Server through mssql-python, Microsoft's driver, which bundles its ODBC driver. A query binds the interval as
`%(interval_start)s` and `%(interval_end)s`. Reads arrive as Arrow, typed by the driver, and each connection fetches
char data as UTF-8 where that is exact, which the driver's default is not (`fetches_char_as_utf8`).

A write stages each frame an upsert, an insert of new rows, or a delete by values needs in a `#temp` table, which any
login may create, shaped by `SELECT TOP 0 ... INTO` from the destination itself, so the stage has the destination's
types exactly. The driver's bulk copy is not used: it opens a connection of its own, outside the run's transaction.
Upserts are an UPDATE then an INSERT of the keys still missing, never `MERGE`, whose history of defects in SQL Server
is long; the INSERT locks the keys it checks, so two writers never insert one key twice.

Each statement a reader or a writer runs has a span, and so does each commit and rollback (`base.span`). `statements`
runs a caller's own SQL, binding `%(name)s` by name: each statement's deadline is the driver's query timeout, in whole
seconds, which SQL Server enforces by cancelling the statement on the server too.

A failure of the driver's is the kind of `basis.core.errors.ServiceError` its SQLSTATE says (`kind_of`), as each call
through the connection or a cursor of it raises it (`Connection`, `Cursor`). The driver keeps no SQLSTATE and no SQL
Server error number on its errors, only the phrase it gives each SQLSTATE, so the phrase names it; and two SQLSTATEs
hold more than one kind, which only SQL Server's message tells apart: a `23000` is a `Conflict` where it is a duplicate
key and `Rejected` otherwise, and a `42000` is `Denied` where a permission was denied or the login failed, as SQL
Server's error 4060 says of a database the login cannot open, and `Failed` otherwise. Each connection sets its
session's language to English (`SET LANGUAGE us_english`), so the messages are English whatever language a login
defaults to."""

import locale
import logging
import math
import re
import sys
import time
from collections.abc import Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass

import mssql_python
import polars as pl
import pyarrow as pa
from mssql_python.exceptions import sqlstate_to_exception

from basis.core.databases.base import (
    FETCH_ROWS,
    LEDGER_COLUMNS,
    Bind,
    Counts,
    LedgerEntry,
    TooManyRowsError,
    binds_named,
    check_count,
    end_reading,
    for_writing,
    ledger_values,
    lower_cased,
    naive_in_zone,
    slices,
    span,
    statement,
)
from basis.core.databases.failures import Boundary, ending, quietly
from basis.core.destination import DestColumn, TableSpec
from basis.core.errors import Conflict, Denied, Failed, Rejected, ServiceError, Timeout, Unavailable
from basis.core.interval import Interval
from basis.core.logs import ms_since
from basis.core.settings import MssqlSettings, publish_refusal, unpublishable
from basis.core.tables import Changes, Delete, Target

log = logging.getLogger(__name__)
LOGIN_TIMEOUT_S = 30
"""The longest a login waits, however long its statements may take."""
_BIND = r"%\({name}\)s"
_STAGE = "#etl_stage"
_MATCH = "#etl_match"


def connect_to(database: str, connection_string: str, timeout_s: int, *, autocommit: bool) -> Connection:
    """A connection to `database` with a deadline on its login, and one on every statement, whose session's language
    is English, so `kind_of` reads its messages whatever the login's default language is."""
    boundary = Boundary(database, _DRIVER, kind_of)
    try:
        raw = boundary.call(mssql_python.connect, connection_string, timeout=min(LOGIN_TIMEOUT_S, timeout_s), autocommit=autocommit)
    except ServiceError as error:
        error.add_note(f"while connecting to SQL Server {target_of(connection_string)}")
        raise
    connection = Connection(raw, boundary)
    try:
        connection.timeout = timeout_s
        if fetches_char_as_utf8(sys.platform, locale.getencoding()):
            boundary.call(raw.setdecoding, mssql_python.SQL_CHAR, encoding="utf-8", ctype=mssql_python.SQL_CHAR)
        with closing(connection.cursor()) as cursor, statement(database, _KIND, _ENGLISH):
            cursor.execute(_ENGLISH)  # a SET, which begins no transaction, with autocommit on or off
    except BaseException:
        connection.close()
        raise
    return connection


_ENGLISH = "SET LANGUAGE us_english"


@dataclass(frozen=True)
class Cursor:
    """A cursor of the driver's, each of whose calls raises the driver's errors as their kinds (`kind_of`). Closing
    it never raises: a cursor that cannot close, as on a connection already lost, is logged, and the error of what it
    ran, if any, is the one raised."""

    raw: mssql_python.Cursor
    boundary: Boundary

    @property
    def rowcount(self) -> int:
        return self.raw.rowcount

    def execute(self, sql: str, *binds: object) -> None:
        self.boundary.call(self.raw.execute, sql, *binds)

    def executemany(self, sql: str, rows: Sequence[Sequence[object]]) -> None:
        self.boundary.call(self.raw.executemany, sql, rows)

    def fetchone(self) -> mssql_python.Row | None:
        return self.boundary.call(self.raw.fetchone)

    def fetchall(self) -> list[mssql_python.Row]:
        return self.boundary.call(self.raw.fetchall)

    def arrow_reader(self, *, batch_size: int) -> Batches:
        return Batches(self.boundary.call(self.raw.arrow_reader, batch_size=batch_size), self.boundary)

    def close(self) -> None:
        quietly(self.boundary.database, self.raw.close, "close a cursor")


@dataclass(frozen=True)
class Batches:
    """The record batches a query's cursor reads as Arrow, each fetched through the boundary."""

    raw: Iterable[pa.RecordBatch]
    """The driver's reader, which `pyarrow.RecordBatchReader`'s schema and close are read from, as a test's is."""
    boundary: Boundary

    @property
    def schema(self) -> pa.Schema:
        return getattr(self.raw, "schema")  # noqa: B009 -- a reader's, which `raw`'s type, all a reader and a test's share, leaves out

    def __iter__(self) -> Iterator[pa.RecordBatch]:
        return self.boundary.each(self.raw)

    def close(self) -> None:
        quietly(self.boundary.database, getattr(self.raw, "close"), "close a query's reader")  # noqa: B009 -- as `schema` is


@dataclass
class Connection:
    """A connection of the driver's, each of whose calls raises the driver's errors as their kinds, as its cursors'
    do. Closing it never raises, as a cursor's does not."""

    raw: mssql_python.Connection
    boundary: Boundary

    @property
    def timeout(self) -> int:
        return self.raw.timeout

    @timeout.setter
    def timeout(self, seconds: int) -> None:
        self.boundary.call(setattr, self.raw, "timeout", seconds)

    def cursor(self) -> Cursor:
        return Cursor(self.boundary.call(self.raw.cursor), self.boundary)

    def commit(self) -> None:
        self.boundary.call(self.raw.commit)

    def rollback(self) -> None:
        self.boundary.call(self.raw.rollback)

    def close(self) -> None:
        quietly(self.boundary.database, self.raw.close, "close its connection")


def fetches_char_as_utf8(platform: str, encoding: str) -> bool:
    """Whether a connection fetches char data (`char`, `varchar`, `text`, and `json`, which this driver gets as
    `varchar(max)`) as UTF-8. Under the driver's default, UTF-16, mssql-python 1.17.0 drops some characters outside
    Unicode's Basic Multilingual Plane, emoji among them, from `varchar(max)` values, with no error: through Arrow, and
    through `fetch*` where one straddles the driver's chunks of 4,095 UTF-16 units (docs/spikes/sqlserver-json.md).

    Fetched as char, the data comes in the charset of the process's locale, and in UTF-8 for a charset the driver does
    not convert to, as glibc's C and POSIX locales' `ANSI_X3.4-1968`: so on Linux it is exact under a UTF-8 locale, as
    in basis's image (`LANG=C.UTF-8`), and under the C locale, where `LC_ALL=C` stops Python coercing it to C.UTF-8.
    Under ISO-8859-1, Arrow raises and `fetch*` returns bytes, so any other charset keeps the default. Elsewhere it is
    unverified, and keeps the default until the spike's checklist for the port shows it exact: on Windows, where it
    comes in the ANSI code page and is likely wrong, and on macOS. It is decided as each connection opens, from the
    locale then, so a later `setlocale` in the process does not change a connection already open."""
    return platform == "linux" and (encoding.replace("-", "").lower() == "utf8" or encoding == "ANSI_X3.4-1968")


_KINDS: dict[str, type[ServiceError]] = {
    "HYT00": Timeout,  # a statement's timeout
    "HY008": Timeout,  # a statement cancelled, as its timeout cancels it
    "08001": Unavailable,  # no connection could be made
    "08003": Unavailable,  # the connection is gone
    "08004": Unavailable,  # the server refused the connection
    "08007": Unavailable,  # the connection failed in a transaction
    "08S01": Unavailable,  # the connection was lost
    "HYT01": Unavailable,  # the login's timeout
    "28000": Denied,  # the login failed
    "40001": Conflict,  # the deadlock's victim
    "23000": Rejected,  # a constraint, which a duplicate key makes a Conflict
    "42000": Failed,  # a syntax error or a denied permission, which is Denied
}
"""The kind each SQLSTATE the driver raises is, but as the message tells apart. Any other is `Failed`."""
_PHRASES: dict[str, str] = {found.driver_error: state for state in _KINDS if (found := sqlstate_to_exception(state, "")) is not None}
"""Each SQLSTATE by the phrase the driver gives it, which is all its errors keep of it."""
_DUPLICATE = re.compile(r"duplicate key", re.IGNORECASE)
_DENIED = re.compile(r"permission (?:was )?denied|login failed", re.IGNORECASE)
"""A `42000` that is `Denied`: a permission denied, or error 4060's `Cannot open database "D" requested by the login.
The login failed.`"""


def kind_of(error: BaseException) -> type[ServiceError]:
    """The kind of failure a driver's error is, by its SQLSTATE and, for those that hold two kinds, its message."""
    if not isinstance(error, mssql_python.Error):
        return Failed
    state, message = _PHRASES.get(error.driver_error), error.ddbc_error
    if state == "23000" and _DUPLICATE.search(message):
        return Conflict
    if state == "42000" and _DENIED.search(message):
        return Denied
    return _KINDS.get(state, Failed) if state is not None else Failed


_DRIVER = (mssql_python.Error,)


def target_of(connection_string: str) -> str:
    """The server and database a connection string names, for errors: never its credentials."""
    parts = {key.strip().lower(): value.strip() for key, _, value in (part.partition("=") for part in connection_string.split(";"))}
    return f"{parts.get('server', '?')}, database {parts.get('database', '?')}"


@dataclass(frozen=True)
class MssqlDatabase:
    name: str
    settings: MssqlSettings

    @property
    def writable(self) -> bool:
        return publish_refusal(self.name, self.settings) is None

    @contextmanager
    def reading(self) -> Generator[MssqlReader]:
        with self._read_transaction(level=logging.INFO) as connection:
            yield MssqlReader(self.name, connection, self.settings.max_rows)

    @contextmanager
    def writing(self) -> Generator[MssqlWriter]:
        write = self.settings.write
        if write is None or write.ledger is None:
            reason, fix = unpublishable(self.name, self.settings)
            msg = f"database {self.name!r} {reason}: {fix}"
            raise LookupError(msg)
        start = time.perf_counter()
        with closing(connect_to(self.name, write.connection_string.get_secret_value(), self.settings.timeout_s, autocommit=False)) as connection:
            log.info("connected", extra={"database": self.name, "access": "write", "connect_ms": ms_since(start)})
            writer = MssqlWriter(self.name, connection, self.settings.max_rows, write.ledger)
            with ending(self.name, lambda: writer.committed or _rolled_back(self.name, connection), "roll back"):
                yield writer

    @contextmanager
    def statements(self, *, writes: bool) -> Generator[MssqlStatements]:
        if not writes:
            with self._read_transaction(level=logging.DEBUG) as connection:
                yield MssqlStatements(self.name, connection, self.settings.timeout_s, self.settings.max_rows, writes=False)
            return
        if (write := self.settings.write) is None:
            msg = f"database {self.name!r} has no write settings: set BASIS_DATABASES__{self.name.upper()}__WRITE__CONNECTION_STRING"
            raise LookupError(msg)
        start = time.perf_counter()
        with closing(connect_to(self.name, write.connection_string.get_secret_value(), self.settings.timeout_s, autocommit=False)) as connection:
            log.debug("connected", extra={"database": self.name, "access": "write", "connect_ms": ms_since(start)})
            statements = MssqlStatements(self.name, connection, self.settings.timeout_s, self.settings.max_rows, writes=True)
            with ending(self.name, lambda: statements.committed or connection.rollback(), "roll back"):
                yield statements

    @contextmanager
    def _read_transaction(self, *, level: int) -> Generator[Connection]:
        """A connection through the read login, in a transaction at the settings' isolation, rolled back at the end."""
        start = time.perf_counter()
        # With autocommit off, the driver's implicit transaction would begin at the SET itself, under the default
        # level, and SQL Server refuses to switch a begun transaction to snapshot: so autocommit stays on, and the
        # transaction begins explicitly, after the level is set.
        with closing(connect_to(self.name, self.settings.connection_string.get_secret_value(), self.settings.timeout_s, autocommit=True)) as connection:
            log.log(level, "connected", extra={"database": self.name, "access": "read", "connect_ms": ms_since(start)})
            with closing(connection.cursor()) as cursor:
                isolation = "SNAPSHOT" if self.settings.isolation == "snapshot" else "READ COMMITTED"
                for sql in (f"SET TRANSACTION ISOLATION LEVEL {isolation}", "BEGIN TRANSACTION"):
                    with statement(self.name, _KIND, sql):
                        cursor.execute(sql)
            try:
                yield connection
            finally:
                end_reading(self.name, lambda: _roll_back(self.name, connection))


def _roll_back(database: str, connection: Connection) -> None:
    with closing(connection.cursor()) as cursor, span(database, _KIND, "rollback", _ROLL_BACK):
        cursor.execute(_ROLL_BACK)


def _rolled_back(database: str, connection: Connection) -> None:
    with span(database, _KIND, "rollback"):
        connection.rollback()


_KIND = "mssql"
_ROLL_BACK = "IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION"


@dataclass
class MssqlReader:
    database: str
    connection: Connection
    max_rows: int

    def fetch(self, sql: str, interval: Interval | None, *, what: str) -> pl.DataFrame:
        binds = binds_named(sql, _BIND, interval, what=what)
        with closing(self.connection.cursor()) as cursor, statement(self.database, _KIND, sql) as traced:
            if binds:
                cursor.execute(sql, binds)
            else:
                cursor.execute(sql)
            reader = cursor.arrow_reader(batch_size=10_000)
            frames: list[pl.DataFrame] = []
            rows = 0
            for batch in reader:
                rows += batch.num_rows
                if rows > self.max_rows:
                    reader.close()
                    raise TooManyRowsError(what, self.max_rows)
                frames.append(_frame(batch))
            traced.set_attribute("db.response.returned_rows", rows)
            return pl.concat(frames) if frames else _frame(reader.schema.empty_table())

    def describe(self, table: str) -> TableSpec | None:
        with closing(self.connection.cursor()) as cursor, statement(self.database, _KIND, _DESCRIBE):
            cursor.execute(_DESCRIBE, {"table": table})
            rows = cursor.fetchall()
        if not rows:
            return None
        return TableSpec("mssql", table, {column.name: column for column in (_column(tuple(row)) for row in rows)})


_DESCRIBE = """
SELECT c.name, t.name, c.max_length, c.precision, c.scale, c.is_nullable, c.is_identity, c.is_computed, c.default_object_id
FROM sys.columns AS c JOIN sys.types AS t ON c.user_type_id = t.user_type_id
WHERE c.object_id = OBJECT_ID(%(table)s)
ORDER BY c.column_id
"""


def _column(row: Sequence[object]) -> DestColumn:
    name, type_name, max_length, precision, scale, nullable, identity, computed, default = row
    kind = str(type_name).upper()
    length = int(str(max_length))
    chars = None if length == -1 or kind in {"TEXT", "NTEXT"} else length // 2 if kind in {"NCHAR", "NVARCHAR"} else length
    numeric = kind in {"DECIMAL", "NUMERIC"}
    return DestColumn(
        name=str(name).lower(),
        type=kind,
        length=chars if kind in {"CHAR", "VARCHAR", "NCHAR", "NVARCHAR", "TEXT", "NTEXT"} else None,
        precision=int(str(precision)) if numeric else None,
        scale=int(str(scale)) if numeric else None,
        nullable=bool(nullable),
        generated=bool(identity) or bool(computed) or kind in {"TIMESTAMP", "ROWVERSION"},
        defaulted=int(str(default)) != 0,
    )


def _frame(arrow: object) -> pl.DataFrame:
    frame = pl.from_arrow(arrow)  # the driver's Arrow objects are untyped; from_arrow checks them
    if not isinstance(frame, pl.DataFrame):
        msg = "the driver returned a column, not a table"
        raise TypeError(msg)
    return frame


@dataclass
class MssqlWriter(MssqlReader):
    ledger: str
    committed: bool = False

    def published(self, run_id: str) -> bool:
        sql = f"SELECT COUNT(*) FROM {self.ledger} WHERE run_id = %(run_id)s"
        with closing(self.connection.cursor()) as cursor, statement(self.database, _KIND, sql):
            cursor.execute(sql, {"run_id": run_id})
            row = cursor.fetchone()
        return row is not None and int(str(row[0])) > 0

    def overlapping(self, job: str, output: str, interval: Interval) -> list[str]:
        binds = {"job": job, "output": output} | interval.binds()
        sql = (
            f"SELECT DISTINCT run_id FROM {self.ledger} WITH (UPDLOCK, HOLDLOCK)"
            " WHERE job = %(job)s AND output = %(output)s AND interval_start < %(interval_end)s AND interval_end > %(interval_start)s"
        )
        with closing(self.connection.cursor()) as cursor, statement(self.database, _KIND, sql):
            cursor.execute(sql, binds)
            return sorted(str(row[0]) for row in cursor.fetchall())

    def apply(self, target: Target, changes: Changes, spec: TableSpec) -> Counts:
        del spec  # SQL Server converts a bit, a date and a decimal as bound
        deleted = upserted = inserted_new = inserted = 0
        if changes.delete is not None:
            deleted = self._delete(target, changes.delete)
        if changes.upsert is not None and changes.upsert.height:
            upserted = self._upsert(target, for_writing(target, changes.upsert))
        if changes.insert_new is not None and changes.insert_new.height:
            inserted_new = self._insert_new(target, for_writing(target, changes.insert_new))
        if changes.insert is not None and changes.insert.height:
            inserted = self._insert(target.table, for_writing(target, changes.insert))
            check_count(f"the insert into {target.table}", changes.insert.height, inserted)
        return Counts(deleted, upserted, inserted_new, inserted)

    def _execute(self, sql: str, binds: tuple[object, ...] = ()) -> int:
        with closing(self.connection.cursor()) as cursor, statement(self.database, _KIND, sql):
            if binds:
                cursor.execute(sql, binds)
            else:
                cursor.execute(sql)
            return max(cursor.rowcount, 0)

    def _insert(self, table: str, frame: pl.DataFrame) -> int:
        sql = f"INSERT INTO {table} ({', '.join(frame.columns)}) VALUES ({', '.join('?' * frame.width)})"
        written = 0
        with closing(self.connection.cursor()) as cursor:
            for part in slices(frame):  # each batch a statement, and a span, of its own
                with statement(self.database, _KIND, sql) as traced:
                    cursor.executemany(sql, part.rows())
                    traced.set_attribute("db.operation.batch.size", part.height)
                written += max(cursor.rowcount, 0)
        return written

    def _stage(self, name: str, table: str, frame: pl.DataFrame) -> None:
        self._execute(f"SELECT TOP 0 {', '.join(frame.columns)} INTO {name} FROM {table}")
        staged = self._insert(name, frame)
        check_count(f"staging for {table}", frame.height, staged)

    def _delete(self, target: Target, delete: Delete) -> int:
        conditions: list[str] = []
        binds: list[object] = []
        if delete.within is not None:
            zone = target.column(delete.within.column).zone
            conditions.append(f"t.{delete.within.column} >= ? AND t.{delete.within.column} < ?")
            binds.extend([naive_in_zone(delete.within.start, zone), naive_in_zone(delete.within.end, zone)])
        if delete.matching is not None:
            self._stage(_MATCH, target.table, for_writing(target, delete.matching))
            matches = " AND ".join(f"t.{name} = m.{name}" for name in delete.matching.columns)
            conditions.append(f"EXISTS (SELECT 1 FROM {_MATCH} AS m WHERE {matches})")
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        deleted = self._execute(f"DELETE t FROM {target.table} AS t{where}", tuple(binds))
        if delete.matching is not None:
            self._execute(f"DROP TABLE {_MATCH}")
        return deleted

    def _upsert(self, target: Target, frame: pl.DataFrame) -> int:
        key = target.key or ()
        self._stage(_STAGE, target.table, frame)
        on = " AND ".join(f"t.{name} = s.{name}" for name in key)
        updated = 0
        if values := [name for name in frame.columns if name not in key]:
            updated = self._execute(f"UPDATE t SET {', '.join(f't.{name} = s.{name}' for name in values)} FROM {target.table} AS t INNER JOIN {_STAGE} AS s ON {on}")
        inserted = self._insert_missing(target.table, frame.columns, on)
        self._execute(f"DROP TABLE {_STAGE}")
        check_count(f"the upsert into {target.table}", frame.height, updated + inserted)
        return updated + inserted

    def _insert_new(self, target: Target, frame: pl.DataFrame) -> int:
        self._stage(_STAGE, target.table, frame)
        inserted = self._insert_missing(target.table, frame.columns, " AND ".join(f"t.{name} = s.{name}" for name in target.key or ()))
        self._execute(f"DROP TABLE {_STAGE}")
        return inserted

    def _insert_missing(self, table: str, columns: list[str], on: str) -> int:
        names = ", ".join(columns)
        selected = ", ".join(f"s.{name}" for name in columns)
        return self._execute(f"INSERT INTO {table} ({names}) SELECT {selected} FROM {_STAGE} AS s WHERE NOT EXISTS (SELECT 1 FROM {table} AS t WITH (UPDLOCK, HOLDLOCK) WHERE {on})")

    def record(self, entry: LedgerEntry) -> None:
        values = ledger_values(entry)
        self._execute(f"INSERT INTO {self.ledger} ({', '.join(LEDGER_COLUMNS)}) VALUES ({', '.join('?' * len(values))})", values)

    def commit(self) -> None:
        with span(self.database, _KIND, "commit"):
            self.connection.commit()
        self.committed = True


@dataclass
class MssqlStatements:
    database: str
    connection: Connection
    limit_s: int
    """The settings' `timeout_s`, which bounds every statement's own."""
    max_rows: int
    writes: bool
    committed: bool = False

    def query(self, sql: str, binds: Mapping[str, Bind], *, timeout_s: float) -> pa.Table:
        with closing(self._cursor(timeout_s)) as cursor:
            _execute(cursor, sql, binds)
            reader = cursor.arrow_reader(batch_size=FETCH_ROWS)
            batches: list[pa.RecordBatch] = []
            rows = 0
            for batch in reader:
                rows += batch.num_rows
                if rows > self.max_rows:
                    reader.close()
                    what = f"a query of database {self.database}"
                    raise TooManyRowsError(what, self.max_rows)
                batches.append(batch)
            return lower_cased(pa.Table.from_batches(batches, schema=reader.schema))

    def execute(self, sql: str, binds: Mapping[str, Bind], *, timeout_s: float) -> int:
        self._refuse_reads("change rows")
        with closing(self._cursor(timeout_s)) as cursor:
            _execute(cursor, sql, binds)
            return max(cursor.rowcount, 0)

    def commit(self) -> None:
        self._refuse_reads("commit")
        self.connection.commit()
        self.committed = True

    def _cursor(self, timeout_s: float) -> Cursor:
        self.connection.timeout = max(1, math.ceil(min(timeout_s, self.limit_s)))  # whole seconds, for the next cursor's statements
        return self.connection.cursor()

    def _refuse_reads(self, what: str) -> None:
        if not self.writes:
            msg = f"database {self.database}'s transaction only reads, so it cannot {what}: open it with writes=True, through the write login"
            raise RuntimeError(msg)


def _execute(cursor: Cursor, sql: str, binds: Mapping[str, Bind]) -> None:
    if binds:
        cursor.execute(sql, dict(binds))
    else:
        cursor.execute(sql)
