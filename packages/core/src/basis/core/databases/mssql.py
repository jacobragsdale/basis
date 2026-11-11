"""SQL Server through mssql-python, Microsoft's driver, which bundles its ODBC driver. A query binds the interval as
`%(interval_start)s` and `%(interval_end)s`. Reads arrive as Arrow, typed by the driver, and each connection fetches
char data as UTF-8 where that is exact, which the driver's default is not (`fetches_char_as_utf8`).

A write stages each frame an upsert, an insert of new rows, or a delete by values needs in a `#temp` table, which any
login may create, shaped by `SELECT TOP 0 ... INTO` from the destination itself, so the stage has the destination's
types exactly, and counts the stage with `SELECT COUNT_BIG(*)`. The driver's bulk copy is not used: it opens a
connection of its own, outside the run's transaction. Upserts are an UPDATE then an INSERT of the keys still missing,
never `MERGE`, whose history of defects in SQL Server is long; the INSERT locks the keys it checks, so two writers never
insert one key twice.

Rows are inserted with each column's type given (`_sizes`), since mssql-python 1.17.0 types a column by one value of
it, its longest, and binds the whole column as `varchar` when that value is ASCII: the server then converts every value
through the database's code page, and `日本` arrives as `??`, with no error. A string binds as `nvarchar`, sized in
UTF-16 units, and a decimal as its text. Rows go in the frame's order: each run of rows whose strings all bind as
`nvarchar(n)` as one `executemany`, and each row holding a string longer than 4,000 units, which only `nvarchar(max)`
binds, as a statement of its own, since the driver streams such a value, and in a batch then runs each row by itself
and counts only the last (docs/spikes/sqlserver-json.md, section 3).

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

import codecs
import itertools
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
import numpy as np
import pandas as pd
import pyarrow as pa
from mssql_python.exceptions import sqlstate_to_exception

from basis.core import frames
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
    number_text,
    rows_of,
    slices,
    span,
    statement,
)
from basis.core.databases.failures import Boundary, ending, quietly
from basis.core.destination import DestColumn, TableSpec, Unit, binding
from basis.core.errors import Conflict, Denied, Failed, Rejected, ServiceError, Timeout, Unavailable
from basis.core.interval import Interval
from basis.core.logs import ms_since
from basis.core.settings import MssqlSettings, publish_refusal, unpublishable
from basis.core.tables import Changes, Delete, Kind, Target

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

    def setinputsizes(self, sizes: list[int | tuple[object, ...]]) -> None:
        """Each parameter's SQL type, size and decimal digits, for the next statement alone."""
        self.boundary.call(self.raw.setinputsizes, sizes)

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

    def fetch(self, sql: str, interval: Interval | None, *, what: str) -> pd.DataFrame:
        binds = binds_named(sql, _BIND, interval, what=what)
        with closing(self.connection.cursor()) as cursor, statement(self.database, _KIND, sql) as traced:
            if binds:
                cursor.execute(sql, binds)
            else:
                cursor.execute(sql)
            reader = cursor.arrow_reader(batch_size=FETCH_ROWS)
            batches: list[pa.RecordBatch] = []
            rows = 0
            for batch in reader:
                rows += batch.num_rows
                if rows > self.max_rows:
                    reader.close()
                    raise TooManyRowsError(what, self.max_rows)
                batches.append(batch)
            traced.set_attribute("db.response.returned_rows", rows)
            return frames.from_arrow(frames.floats(pa.Table.from_batches(batches, schema=reader.schema), what=what))

    def describe(self, table: str) -> TableSpec | None:
        with closing(self.connection.cursor()) as cursor, statement(self.database, _KIND, _DESCRIBE):
            cursor.execute(_DESCRIBE, {"table": table})
            rows = cursor.fetchall()
        if not rows:
            return None
        return TableSpec("mssql", table, {column.name: column for column in (_column(tuple(row)) for row in rows)})


_DESCRIBE = """
SELECT c.name, COALESCE(TYPE_NAME(c.system_type_id), t.name), c.max_length, c.precision, c.scale, c.is_nullable,
    c.is_identity, c.is_computed, c.default_object_id, CAST(COLLATIONPROPERTY(c.collation_name, 'CodePage') AS int)
FROM sys.columns AS c JOIN sys.types AS t ON c.user_type_id = t.user_type_id
WHERE c.object_id = OBJECT_ID(%(table)s)
ORDER BY c.column_id
"""
"""Each column's type is its system type's, so a column of an alias type, as `CREATE TYPE code FROM varchar(50)`, or
`sysname`, is the `varchar` or `nvarchar` it is; a CLR type, as `hierarchyid`, has no system type's name, and keeps
its own."""


def _column(row: Sequence[object]) -> DestColumn:
    name, type_name, max_length, precision, scale, nullable, identity, computed, default, code_page = row
    kind = str(type_name).upper()
    length = int(str(max_length))
    numeric = kind in {"DECIMAL", "NUMERIC"}
    unit, encoding, charset = _storage(kind, code_page)
    bounded = encoding is not None and length != -1 and kind not in {"TEXT", "NTEXT"}
    return DestColumn(
        name=str(name).lower(),
        type=kind,
        length=(length // 2 if unit == "UTF-16 units" else length) if bounded else None,
        precision=int(str(precision)) if numeric else None,
        scale=int(str(scale)) if numeric or kind in {"DATETIME2", "DATETIMEOFFSET", "TIME"} else None,  # a time's: its digits of a second
        nullable=bool(nullable),
        generated=bool(identity) or bool(computed) or kind in {"TIMESTAMP", "ROWVERSION"},
        defaulted=int(str(default)) != 0,
        unit=unit,
        encoding=encoding,
        charset=charset,
    )


def _storage(kind: str, code_page: object) -> tuple[Unit, str | None, str | None]:
    """How a column of `kind` measures and stores text: an `nchar`, `nvarchar` or `ntext` in UTF-16 units, and a
    `char`, `varchar` or `text` in bytes of its collation's code page."""
    if kind in {"NCHAR", "NVARCHAR", "NTEXT"}:
        return "UTF-16 units", "utf-16-le", "UTF-16"
    if kind in {"CHAR", "VARCHAR", "TEXT"}:
        return ("bytes", "utf-8", "UTF-8") if code_page == _UTF8 else ("bytes", _codec(f"cp{code_page}"), f"code page {code_page}")
    return "characters", None, None


def _codec(name: str) -> str | None:
    """`name`, where Python has the codec, which it has for every code page SQL Server 2022 has; None otherwise, and a
    column in it refuses text beyond ASCII."""
    try:
        codecs.lookup(name)
    except LookupError:
        return None
    return name


_UTF8 = 65001
"""The code page of a `_UTF8` collation, whose `char` and `varchar` hold any Unicode text, and measure it in UTF-8's
bytes. Every other code page SQL Server has is one of Python's codecs, as `cp1252` and `cp932`, and the server replaces
a character it cannot hold with another, as `?`, without an error, so `check_values` refuses one first. Python's
`cp1252` refuses U+0081, U+008D, U+008F, U+0090 and U+009D, which SQL Server keeps, and holds nothing SQL Server loses
(docs/spikes/sqlserver-json.md)."""


INLINE_UNITS = 4_000
"""The longest string, in UTF-16 units, that binds as `nvarchar(n)`. A longer one binds only as `nvarchar(max)`, whose
value mssql-python 1.17.0 streams: an `executemany` holding one runs each row by itself, and counts only the last."""


def _units(target: Target, part: pa.Table, texts: set[str]) -> pd.DataFrame:
    """The length of each of `part`'s strings in UTF-16 units, a column for each string column and each number bound
    as text, each in one pass."""
    strings = frames.from_arrow(part.select([name for name in part.column_names if target.column(name).kind == "str" or name in texts]))
    return pd.DataFrame({name: frames.utf16_units(strings[name]) for name in strings.columns}, index=strings.index)


def _long(units: pd.DataFrame, rows: int) -> np.ndarray:
    """Which of the rows `units` measures hold a string longer than `INLINE_UNITS`; a null is no string."""
    found = np.zeros(rows, dtype=bool)
    for name in units.columns:
        found |= units[name].fillna(0).to_numpy(dtype=np.int64) > INLINE_UNITS
    return found


def _runs(long: np.ndarray) -> Iterator[tuple[int, int, bool]]:
    """Each run of rows alike, long or not, as its first row, the row after its last, and whether its rows are long."""
    edges = [0, *(np.flatnonzero(long[1:] != long[:-1]) + 1).tolist(), len(long)]
    for start, end in itertools.pairwise(edges):
        yield start, end, bool(long[start])


def _bound(target: Target, part: pa.Table, spec: TableSpec) -> tuple[pa.Table, set[str]]:
    """`part` as it binds, and the floats it binds as text: each into a decimal or an integer column as its text, which
    the server converts to the column's type, its scale rounding it (`destination.binding`), as the driver binds most
    decimals in a batch itself; into a float's column, a float."""
    texts: set[str] = set()
    for index, name in enumerate(part.column_names):
        if target.column(name).kind == "float" and (found := spec.columns.get(name)) is not None and (how := binding(found)) != "float":
            part = part.set_column(index, name, number_text(part.column(index), how))
            texts.add(name)
    return part, texts


def _most(units: pd.DataFrame) -> dict[str, int | None]:
    """The longest string of each column `units` measures, or None for a column of nulls."""
    return {str(name): None if pd.isna(found := units[name].max()) else int(found) for name in units.columns}


def _sizes(target: Target, columns: list[str], longest: Mapping[str, int | None], texts: set[str]) -> list[int | tuple[object, ...]]:
    """Each column's SQL type, size and decimal digits, as a statement binds them: a string as `nvarchar`, sized to the
    longest of it the statement binds, in UTF-16 units, or `nvarchar(max)` past `INLINE_UNITS`; a float bound as text
    as `varchar`, sized as a string is; and every other kind as the driver types it."""
    return [_number(longest.get(name)) if name in texts else _text(longest.get(name)) if (kind := target.column(name).kind) == "str" else _BINDS[kind] for name in columns]


def _number(length: int | None) -> tuple[int, int, int]:
    return (int(mssql_python.SQL_VARCHAR), max(length or 0, 1), 0)


def _text(units: int | None) -> tuple[int, int, int]:
    if units is not None and units > INLINE_UNITS:
        return (_NVARCHAR, 0, 0)  # nvarchar(max), which the driver streams
    return (_NVARCHAR, max(units or 0, 1), 0)


_NVARCHAR = int(mssql_python.SQL_WVARCHAR)
_BINDS: dict[Kind, tuple[int, int, int]] = {
    "float": (int(mssql_python.SQL_DOUBLE), 15, 0),
    "bool": (int(mssql_python.SQL_BIT), 1, 0),
    "date": (int(mssql_python.SQL_DATE), 10, 0),
    "datetime": (int(mssql_python.SQL_TIMESTAMP), 26, 6),  # naive, to the microsecond, as `for_writing` leaves it
}
"""How a statement binds each kind but a string, and a float bound as text, whose sizes are their own."""


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

    def recorded(self, run_id: str) -> dict[str, Counts]:
        sql = f"SELECT output, deleted, upserted, inserted_new, inserted FROM {self.ledger} WHERE run_id = %(run_id)s"
        with closing(self.connection.cursor()) as cursor, statement(self.database, _KIND, sql):
            cursor.execute(sql, {"run_id": run_id})
            rows = cursor.fetchall()
        return {str(row[0]): Counts(*(int(str(value)) for value in row[1:])) for row in rows}

    def lock(self, job: str, output: str) -> None:
        """An application lock, `sp_getapplock`, owned by the transaction, which any login may take; it waits as long
        as the statement's timeout allows."""
        sql = "SET NOCOUNT ON; DECLARE @granted int; EXEC @granted = sp_getapplock @Resource = %(resource)s, @LockMode = 'Exclusive', @LockOwner = 'Transaction'; SELECT @granted"
        with closing(self.connection.cursor()) as cursor, statement(self.database, _KIND, sql):
            cursor.execute(sql, {"resource": f"basis {self.ledger} {job} {output}"[:255]})
            row = cursor.fetchone()
        if row is None or int(str(row[0])) < 0:
            detail = f"database {self.database} did not grant the lock of {job}'s {output} (sp_getapplock returned {row[0] if row is not None else None})"
            raise Conflict(detail, resource=self.database)

    def overlapping(self, job: str, output: str, interval: Interval | None) -> list[str]:
        binds: dict[str, object] = {"job": job, "output": output}
        sql = f"SELECT DISTINCT run_id FROM {self.ledger} WHERE job = %(job)s AND output = %(output)s"
        if interval is not None:
            binds |= interval.binds()
            sql += " AND interval_start < %(interval_end)s AND interval_end > %(interval_start)s"
        with closing(self.connection.cursor()) as cursor, statement(self.database, _KIND, sql):
            cursor.execute(sql, binds)
            return sorted(str(row[0]).strip() for row in cursor.fetchall())

    def apply(self, target: Target, changes: Changes, spec: TableSpec) -> Counts:
        deleted = upserted = inserted_new = inserted = 0
        if changes.delete is not None:
            deleted = self._delete(target, changes.delete, spec)
        if changes.upsert is not None and len(changes.upsert):
            upserted = self._upsert(target, for_writing(target, changes.upsert), spec)
        if changes.insert_new is not None and len(changes.insert_new):
            inserted_new = self._insert_new(target, for_writing(target, changes.insert_new), spec)
        if changes.insert is not None and len(changes.insert):
            inserted = self._insert(target, target.table, for_writing(target, changes.insert), spec)
            check_count(f"the insert into {target.table}", len(changes.insert), inserted)
        return Counts(deleted, upserted, inserted_new, inserted)

    def _execute(self, sql: str, binds: tuple[object, ...] = ()) -> int:
        with closing(self.connection.cursor()) as cursor, statement(self.database, _KIND, sql):
            if binds:
                cursor.execute(sql, binds)
            else:
                cursor.execute(sql)
            return max(cursor.rowcount, 0)

    def _insert(self, target: Target, table: str, values: pa.Table, spec: TableSpec) -> int:
        """Insert `values`, whose columns are `target`'s, into `table`, and the rows its statements counted."""
        with closing(self.connection.cursor()) as cursor:
            return sum(self._send(cursor, target, table, part, spec) for part in slices(values))

    def _send(self, cursor: Cursor, target: Target, table: str, part: pa.Table, spec: TableSpec) -> int:
        """Insert one slice, in its own order, and the rows its statements counted: each run of rows whose strings all
        bind as `nvarchar(n)` as one `executemany`, and each run of the others one row to a statement, every column's
        type given (`_sizes`). Each run is a span: a batch's records its `db.operation.batch.size`, and a run of rows
        one by one its `basis.statements`, so a trace shows what each row costs."""
        sql = f"INSERT INTO {table} ({', '.join(part.column_names)}) VALUES ({', '.join('?' * part.num_columns)})"
        bound, texts = _bound(target, part, spec)
        units = _units(target, bound, texts)
        written = 0
        for start, end, alone in _runs(_long(units, part.num_rows)):
            rows, lengths = bound.slice(start, end - start), units.iloc[start:end]
            with statement(self.database, _KIND, sql) as traced:
                if alone:
                    for row, longest in zip(rows_of(rows), lengths.to_dict("records"), strict=True):
                        cursor.setinputsizes(_sizes(target, rows.column_names, {str(name): None if pd.isna(found) else int(found) for name, found in longest.items()}, texts))
                        cursor.execute(sql, row)
                        written += max(cursor.rowcount, 0)
                    traced.set_attribute("basis.statements", end - start)
                else:
                    cursor.setinputsizes(_sizes(target, rows.column_names, _most(lengths), texts))
                    cursor.executemany(sql, rows_of(rows))
                    written += max(cursor.rowcount, 0)
                    traced.set_attribute("db.operation.batch.size", end - start)
        return written

    def _stage(self, name: str, target: Target, values: pa.Table, spec: TableSpec) -> None:
        """Stage `values` in the `#temp` table `name`, counted by the server: the stage is this session's alone, so its
        count is exactly the rows staged."""
        self._execute(f"SELECT TOP 0 {', '.join(values.column_names)} INTO {name} FROM {target.table}")
        with closing(self.connection.cursor()) as cursor:
            for part in slices(values):
                self._send(cursor, target, name, part, spec)
        sql = f"SELECT COUNT_BIG(*) FROM {name}"
        with closing(self.connection.cursor()) as cursor, statement(self.database, _KIND, sql):
            cursor.execute(sql)
            row = cursor.fetchone()
        check_count(f"staging for {target.table}", values.num_rows, row[0] if row is not None else 0)

    def _delete(self, target: Target, delete: Delete, spec: TableSpec) -> int:
        conditions: list[str] = []
        binds: list[object] = []
        if delete.within is not None:
            zone = target.column(delete.within.column).zone
            conditions.append(f"t.{delete.within.column} >= ? AND t.{delete.within.column} < ?")
            binds.extend([naive_in_zone(delete.within.start, zone), naive_in_zone(delete.within.end, zone)])
        if delete.matching is not None:
            self._stage(_MATCH, target, for_writing(target, delete.matching), spec)
            matches = " AND ".join(f"t.{name} = m.{name}" for name in delete.matching.columns)
            conditions.append(f"EXISTS (SELECT 1 FROM {_MATCH} AS m WHERE {matches})")
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        deleted = self._execute(f"DELETE t FROM {target.table} AS t{where}", tuple(binds))
        if delete.matching is not None:
            self._execute(f"DROP TABLE {_MATCH}")
        return deleted

    def _upsert(self, target: Target, rows: pa.Table, spec: TableSpec) -> int:
        key = target.key or ()
        self._stage(_STAGE, target, rows, spec)
        on = " AND ".join(f"t.{name} = s.{name}" for name in key)
        if not (values := [name for name in rows.column_names if name not in key]):  # every column is the key: a row the table has is upserted already
            self._insert_missing(target.table, rows.column_names, on)
            self._execute(f"DROP TABLE {_STAGE}")
            return int(rows.num_rows)
        updated = self._execute(f"UPDATE t SET {', '.join(f't.{name} = s.{name}' for name in values)} FROM {target.table} AS t INNER JOIN {_STAGE} AS s ON {on}")
        inserted = self._insert_missing(target.table, rows.column_names, on)
        self._execute(f"DROP TABLE {_STAGE}")
        check_count(f"the upsert into {target.table}", rows.num_rows, updated + inserted)
        return updated + inserted

    def _insert_new(self, target: Target, rows: pa.Table, spec: TableSpec) -> int:
        self._stage(_STAGE, target, rows, spec)
        inserted = self._insert_missing(target.table, rows.column_names, " AND ".join(f"t.{name} = s.{name}" for name in target.key or ()))
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
