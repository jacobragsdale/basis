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
seconds, which SQL Server enforces by cancelling the statement on the server too."""

import locale
import logging
import math
import sys
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass

import mssql_python
import polars as pl
import pyarrow as pa

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
from basis.core.destination import DestColumn, TableSpec
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


def connect_to(connection_string: str, timeout_s: int, *, autocommit: bool) -> mssql_python.Connection:
    """A connection with a deadline on its login, and one on every statement."""
    try:
        connection = mssql_python.connect(connection_string, timeout=min(LOGIN_TIMEOUT_S, timeout_s), autocommit=autocommit)
    except mssql_python.Error as error:
        error.add_note(f"while connecting to SQL Server {target_of(connection_string)}")
        raise
    connection.timeout = timeout_s
    if fetches_char_as_utf8(sys.platform, locale.getencoding()):
        connection.setdecoding(mssql_python.SQL_CHAR, encoding="utf-8", ctype=mssql_python.SQL_CHAR)
    return connection


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
        with closing(connect_to(write.connection_string.get_secret_value(), self.settings.timeout_s, autocommit=False)) as connection:
            log.info("connected", extra={"database": self.name, "access": "write", "connect_ms": ms_since(start)})
            writer = MssqlWriter(self.name, connection, self.settings.max_rows, write.ledger)
            try:
                yield writer
            finally:
                if not writer.committed:
                    with span(self.name, _KIND, "rollback"):
                        connection.rollback()

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
        with closing(connect_to(write.connection_string.get_secret_value(), self.settings.timeout_s, autocommit=False)) as connection:
            log.debug("connected", extra={"database": self.name, "access": "write", "connect_ms": ms_since(start)})
            statements = MssqlStatements(self.name, connection, self.settings.timeout_s, self.settings.max_rows, writes=True)
            try:
                yield statements
            finally:
                if not statements.committed:
                    connection.rollback()

    @contextmanager
    def _read_transaction(self, *, level: int) -> Generator[mssql_python.Connection]:
        """A connection through the read login, in a transaction at the settings' isolation, rolled back at the end."""
        start = time.perf_counter()
        # With autocommit off, the driver's implicit transaction would begin at the SET itself, under the default
        # level, and SQL Server refuses to switch a begun transaction to snapshot: so autocommit stays on, and the
        # transaction begins explicitly, after the level is set.
        with closing(connect_to(self.settings.connection_string.get_secret_value(), self.settings.timeout_s, autocommit=True)) as connection:
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


def _roll_back(database: str, connection: mssql_python.Connection) -> None:
    with closing(connection.cursor()) as cursor, span(database, _KIND, "rollback", _ROLL_BACK):
        cursor.execute(_ROLL_BACK)


_KIND = "mssql"
_ROLL_BACK = "IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION"


@dataclass
class MssqlReader:
    database: str
    connection: mssql_python.Connection
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
    connection: mssql_python.Connection
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

    def _cursor(self, timeout_s: float) -> mssql_python.Cursor:
        self.connection.timeout = max(1, math.ceil(min(timeout_s, self.limit_s)))  # whole seconds, for the next cursor's statements
        return self.connection.cursor()

    def _refuse_reads(self, what: str) -> None:
        if not self.writes:
            msg = f"database {self.database}'s transaction only reads, so it cannot {what}: open it with writes=True, through the write login"
            raise RuntimeError(msg)


def _execute(cursor: mssql_python.Cursor, sql: str, binds: Mapping[str, Bind]) -> None:
    if binds:
        cursor.execute(sql, dict(binds))
    else:
        cursor.execute(sql)
