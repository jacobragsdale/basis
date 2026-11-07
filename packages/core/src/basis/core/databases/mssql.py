"""SQL Server through mssql-python, Microsoft's driver, which bundles its ODBC driver. A query binds the interval as
`%(interval_start)s` and `%(interval_end)s`. Reads arrive as Arrow, typed by the driver.

A write stages each frame an upsert, an insert of new rows, or a delete by values needs in a `#temp` table, which any
login may create, shaped by `SELECT TOP 0 ... INTO` from the destination itself, so the stage has the destination's
types exactly. The driver's bulk copy is not used: it opens a connection of its own, outside the run's transaction.
Upserts are an UPDATE then an INSERT of the keys still missing, never `MERGE`, whose history of defects in SQL Server
is long; the INSERT locks the keys it checks, so two writers never insert one key twice."""

import logging
import time
from collections.abc import Generator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass

import mssql_python
import polars as pl

from basis.core.databases.base import LEDGER_COLUMNS, Counts, LedgerEntry, TooManyRowsError, binds_named, check_count, for_writing, ledger_values, naive_in_zone, slices
from basis.core.destination import DestColumn, TableSpec
from basis.core.interval import Interval
from basis.core.logs import ms_since
from basis.core.settings import MssqlSettings
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
    return connection


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
        return self.settings.write is not None

    @contextmanager
    def reading(self) -> Generator[MssqlReader]:
        start = time.perf_counter()
        # With autocommit off, the driver's implicit transaction would begin at the SET itself, under the default
        # level, and SQL Server refuses to switch a begun transaction to snapshot: so autocommit stays on, and the
        # transaction begins explicitly, after the level is set.
        with closing(connect_to(self.settings.connection_string.get_secret_value(), self.settings.timeout_s, autocommit=True)) as connection:
            log.info("connected", extra={"database": self.name, "access": "read", "connect_ms": ms_since(start)})
            with closing(connection.cursor()) as cursor:
                level = "SNAPSHOT" if self.settings.isolation == "snapshot" else "READ COMMITTED"
                cursor.execute(f"SET TRANSACTION ISOLATION LEVEL {level}")
                cursor.execute("BEGIN TRANSACTION")
            try:
                yield MssqlReader(connection, self.settings.max_rows)
            finally:
                with closing(connection.cursor()) as cursor:
                    cursor.execute("IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION")

    @contextmanager
    def writing(self) -> Generator[MssqlWriter]:
        if self.settings.write is None:
            msg = f"database {self.name!r} has no write settings: set ETL_DATABASES__{self.name.upper()}__WRITE__CONNECTION_STRING and __LEDGER"
            raise LookupError(msg)
        start = time.perf_counter()
        with closing(connect_to(self.settings.write.connection_string.get_secret_value(), self.settings.timeout_s, autocommit=False)) as connection:
            log.info("connected", extra={"database": self.name, "access": "write", "connect_ms": ms_since(start)})
            writer = MssqlWriter(connection, self.settings.max_rows, self.settings.write.ledger)
            try:
                yield writer
            finally:
                if not writer.committed:
                    connection.rollback()


@dataclass
class MssqlReader:
    connection: mssql_python.Connection
    max_rows: int

    def fetch(self, sql: str, interval: Interval | None, *, what: str) -> pl.DataFrame:
        binds = binds_named(sql, _BIND, interval, what=what)
        with closing(self.connection.cursor()) as cursor:
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
            return pl.concat(frames) if frames else _frame(reader.schema.empty_table())

    def describe(self, table: str) -> TableSpec | None:
        with closing(self.connection.cursor()) as cursor:
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
        with closing(self.connection.cursor()) as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {self.ledger} WHERE run_id = %(run_id)s", {"run_id": run_id})
            row = cursor.fetchone()
        return row is not None and int(str(row[0])) > 0

    def overlapping(self, job: str, output: str, interval: Interval) -> list[str]:
        binds = {"job": job, "output": output} | interval.binds()
        with closing(self.connection.cursor()) as cursor:
            cursor.execute(
                f"SELECT DISTINCT run_id FROM {self.ledger} WITH (UPDLOCK, HOLDLOCK)"
                " WHERE job = %(job)s AND output = %(output)s AND interval_start < %(interval_end)s AND interval_end > %(interval_start)s",
                binds,
            )
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

    def _execute(self, statement: str, binds: tuple[object, ...] = ()) -> int:
        with closing(self.connection.cursor()) as cursor:
            if binds:
                cursor.execute(statement, binds)
            else:
                cursor.execute(statement)
            return max(cursor.rowcount, 0)

    def _insert(self, table: str, frame: pl.DataFrame) -> int:
        statement = f"INSERT INTO {table} ({', '.join(frame.columns)}) VALUES ({', '.join('?' * frame.width)})"
        written = 0
        with closing(self.connection.cursor()) as cursor:
            for part in slices(frame):
                cursor.executemany(statement, part.rows())
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
        self.connection.commit()
        self.committed = True
