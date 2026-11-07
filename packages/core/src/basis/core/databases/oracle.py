"""Oracle through python-oracledb. A query binds the interval as `:interval_start` and `:interval_end`. Reads arrive as Arrow, with
`NUMBER(p,s)` as decimals; an unconstrained `NUMBER` arrives as a float, so a row that needs it exact casts it in its
query, as `CAST(x AS NUMBER(18,4))`.

Thin mode, the default, needs no client libraries. Thick mode loads Oracle Client libraries, which native network
encryption and older servers need. A write binds whole frames to batched statements, with no staging table: an
upsert and an insert of new rows are a `MERGE` per row, from `dual`, and a delete by values is a `DELETE` per row.

Each statement a reader or a writer runs has a span, and so does each commit and rollback (`base.span`). `statements`
runs a caller's own SQL, binding `:name` by name: each round trip's deadline is the driver's call timeout, in
milliseconds, after which the driver breaks the call off.

A failure of the driver's is the kind of `basis.core.errors.ServiceError` its Oracle or driver code says (`kind_of`),
as each call through the connection or a cursor of it raises it (`Connection`, `Cursor`). Thin mode reports a call
past its timeout as `DPY-4024`, or, when the database does not answer the break either, as `DPY-4011` saying its
socket timed out: both are a `Timeout`, and any other `DPY-4011`, a connection closed, is
`Unavailable`. A login the database accepts and never answers is `Unavailable` too, as SQL Server's is."""

import logging
import math
import threading
import time
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from concurrent.futures import Future, wait
from contextlib import closing, contextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any

import oracledb
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
from basis.core.databases.failures import Boundary, ending, failures, quietly
from basis.core.destination import DestColumn, TableSpec
from basis.core.errors import Conflict, Denied, Failed, Rejected, ServiceError, Timeout, Unavailable
from basis.core.interval import Interval
from basis.core.logs import ms_since
from basis.core.settings import OracleSettings, publish_refusal, unpublishable
from basis.core.tables import Changes, Delete, Target

log = logging.getLogger(__name__)
CONNECT_TIMEOUT_S = 30
"""The longest a login waits, however long its round trips may take."""
_BIND = r"(?<![:\w]):{name}\b"
_KIND = "oracle"
_TIMEOUTS = frozenset({1013, 3156})
"""A call cancelled, as its timeout cancels it, and a call past its timeout, in thick mode."""
_UNAVAILABLE = frozenset({1033, 1034, 1089, 1090, 1092, 3113, 3114, 3135, 12153, 12170, 12203})
"""The instance starting, stopping or gone, a connection lost, and the TNS errors of reaching it, besides 12500 to 12599."""
_DENIED = frozenset({1017, 1031, 1035, 1045, 1950, 28000, 28001})
"""A login refused, locked or expired, and a privilege missing: to the session, to a statement, or to a tablespace."""
_CONFLICTS = frozenset({1, 60, 8177})
"""A unique constraint, a deadlock's victim, and a serializable transaction's conflict."""
_REJECTED = frozenset({1400, 1407, 2290, 2291, 2292})
"""A null where none may be, a check constraint, and a foreign key either way."""


class HungLoginError(TimeoutError):
    """A database that accepted the connection and did not log it in within the login's timeout."""


def kind_of(error: BaseException) -> type[ServiceError]:
    """The kind of failure a driver's error is, by its Oracle error number or its driver code."""
    if isinstance(error, HungLoginError):
        return Unavailable
    found = error.args[0] if isinstance(error, oracledb.Error) and error.args else None
    if not isinstance(found, oracledb._Error):  # noqa: SLF001 -- the driver's documented error object, which each of its errors holds
        return Failed
    code, full = found.code, found.full_code
    if full == "DPY-4024" or code in _TIMEOUTS or (full == "DPY-4011" and "timed out" in found.message):
        return Timeout
    if full.startswith("DPY-6") or full in {"DPY-4011", "DPY-1001"} or code in _UNAVAILABLE or 12500 <= code < 12600:  # noqa: PLR2004 -- the TNS errors of reaching a database
        return Unavailable
    kinds: tuple[tuple[frozenset[int], type[ServiceError]], ...] = ((_DENIED, Denied), (_CONFLICTS, Conflict), (_REJECTED, Rejected))
    return next((kind for codes, kind in kinds if code in codes), Failed)


_DRIVER = (oracledb.Error, HungLoginError)


def connect(settings: OracleSettings, *, database: str, user: str, password: str) -> Connection:
    """A connection with a deadline on its login and on every round trip. Thin mode bounds only the TCP connect: a
    database that accepts TCP but never answers the login would hang the run, so the login runs in a thread that the
    run stops waiting for."""
    if settings.mode == "thick":
        oracledb.init_oracle_client(lib_dir=str(settings.client_lib_dir) if settings.client_lib_dir is not None else None)
    login = partial(oracledb.connect, user=user, password=password, dsn=settings.dsn)
    connected: Future[oracledb.Connection] = Future()
    threading.Thread(target=_resolve, args=(connected, login), daemon=True).start()
    with failures(database, _DRIVER, kind_of):
        if not wait([connected], timeout=(deadline := min(CONNECT_TIMEOUT_S, settings.timeout_s))).done:
            msg = f"Oracle at {settings.dsn} accepted a connection but did not log in within {deadline}s: the listener or the database is hung"
            raise HungLoginError(msg)
        raw = connected.result()
    connection = Connection(raw, Boundary(database, _DRIVER, kind_of))
    connection.call_timeout = settings.timeout_s * 1000
    return connection


@dataclass(frozen=True)
class Cursor:
    """A cursor of the driver's, each of whose calls raises the driver's errors as their kinds (`kind_of`). Closing
    it never raises: a cursor that cannot close, as on a connection already lost, is logged, and the error of what it
    ran, if any, is the one raised."""

    raw: oracledb.Cursor
    boundary: Boundary

    @property
    def rowcount(self) -> int:
        return self.raw.rowcount

    def execute(self, sql: str, binds: object = None) -> None:
        self.boundary.call(self.raw.execute, sql, binds)

    def executemany(self, sql: str, rows: object) -> None:
        self.boundary.call(self.raw.executemany, sql, rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.boundary.call(self.raw.fetchone)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.boundary.call(self.raw.fetchall)

    def close(self) -> None:
        quietly(self.boundary.database, self.raw.close, "close a cursor")


@dataclass
class Connection:
    """A connection of the driver's, each of whose calls raises the driver's errors as their kinds, as its cursors'
    and its batches' do. Closing it never raises, as a cursor's does not."""

    raw: oracledb.Connection
    boundary: Boundary

    @property
    def call_timeout(self) -> int:
        return self.raw.call_timeout

    @call_timeout.setter
    def call_timeout(self, milliseconds: int) -> None:
        self.boundary.call(setattr, self.raw, "call_timeout", milliseconds)

    def cursor(self) -> Cursor:
        return Cursor(self.boundary.call(self.raw.cursor), self.boundary)

    def fetch_df_batches(self, sql: str, binds: Mapping[str, object] | None, *, size: int) -> Iterator[oracledb.DataFrame]:
        """The query's rows in batches of `size`, its `NUMBER(p,s)`s as decimals, each batch fetched through the
        boundary."""
        given = dict(binds) if binds is not None else None
        return self.boundary.each(self.boundary.call(self.raw.fetch_df_batches, sql, given, size=size, fetch_decimals=True))

    def commit(self) -> None:
        self.boundary.call(self.raw.commit)

    def rollback(self) -> None:
        self.boundary.call(self.raw.rollback)

    def close(self) -> None:
        quietly(self.boundary.database, self.raw.close, "close its connection")


def _resolve[T](future: Future[T], call: Callable[[], T]) -> None:
    try:
        future.set_result(call())
    except Exception as error:  # noqa: BLE001 -- handed to the waiting caller, which raises it
        future.set_exception(error)


@dataclass(frozen=True)
class OracleDatabase:
    name: str
    settings: OracleSettings

    @property
    def writable(self) -> bool:
        return publish_refusal(self.name, self.settings) is None

    @contextmanager
    def reading(self) -> Generator[OracleReader]:
        with self._read_transaction(level=logging.INFO) as connection:
            yield OracleReader(self.name, connection, self.settings.max_rows)

    @contextmanager
    def writing(self) -> Generator[OracleWriter]:
        write = self.settings.write
        if write is None or write.ledger is None:
            reason, fix = unpublishable(self.name, self.settings)
            msg = f"database {self.name!r} {reason}: {fix}"
            raise LookupError(msg)
        start = time.perf_counter()
        with closing(connect(self.settings, database=self.name, user=write.user, password=write.password.get_secret_value())) as connection:
            log.info("connected", extra={"database": self.name, "access": "write", "connect_ms": ms_since(start)})
            writer = OracleWriter(self.name, connection, self.settings.max_rows, write.ledger)
            with ending(self.name, lambda: writer.committed or _roll_back(self.name, connection), "roll back"):
                yield writer

    @contextmanager
    def statements(self, *, writes: bool) -> Generator[OracleStatements]:
        if not writes:
            with self._read_transaction(level=logging.DEBUG) as connection:
                yield OracleStatements(self.name, connection, self.settings.timeout_s, self.settings.max_rows, writes=False)
            return
        if (write := self.settings.write) is None:
            msg = f"database {self.name!r} has no write settings: set BASIS_DATABASES__{self.name.upper()}__WRITE__USER and __PASSWORD"
            raise LookupError(msg)
        start = time.perf_counter()
        with closing(connect(self.settings, database=self.name, user=write.user, password=write.password.get_secret_value())) as connection:
            log.debug("connected", extra={"database": self.name, "access": "write", "connect_ms": ms_since(start)})
            statements = OracleStatements(self.name, connection, self.settings.timeout_s, self.settings.max_rows, writes=True)
            with ending(self.name, lambda: statements.committed or connection.rollback(), "roll back"):
                yield statements

    @contextmanager
    def _read_transaction(self, *, level: int) -> Generator[Connection]:
        """A connection through the read login, in a transaction at the settings' isolation, rolled back at the end."""
        start = time.perf_counter()
        with closing(connect(self.settings, database=self.name, user=self.settings.user, password=self.settings.password.get_secret_value())) as connection:
            log.log(level, "connected", extra={"database": self.name, "access": "read", "connect_ms": ms_since(start)})
            if self.settings.isolation == "read_only":
                with closing(connection.cursor()) as cursor, statement(self.name, _KIND, _READ_ONLY):
                    cursor.execute(_READ_ONLY)
            try:
                yield connection
            finally:
                end_reading(self.name, lambda: _roll_back(self.name, connection))


_READ_ONLY = "SET TRANSACTION READ ONLY"


def _roll_back(database: str, connection: Connection) -> None:
    with span(database, _KIND, "rollback"):
        connection.rollback()


@dataclass
class OracleReader:
    database: str
    connection: Connection
    max_rows: int

    def fetch(self, sql: str, interval: Interval | None, *, what: str) -> pl.DataFrame:
        binds = binds_named(sql, _BIND, interval, what=what)
        frames: list[pl.DataFrame] = []
        rows = 0
        try:
            with statement(self.database, _KIND, sql) as traced:
                for batch in self.connection.fetch_df_batches(sql, binds or None, size=10_000):
                    frame = pl.DataFrame(batch)
                    rows += frame.height
                    if rows > self.max_rows:
                        raise TooManyRowsError(what, self.max_rows)
                    frames.append(frame)
                traced.set_attribute("db.response.returned_rows", rows)
        except ServiceError as error:
            if "ORA-01466" in str(error):
                error.add_note(
                    "a table this query reads changed its definition (a TRUNCATE counts) seconds before the run's read-only transaction began:"
                    " run again, or set the database's ISOLATION=read_committed"
                )
            raise
        return pl.concat(frames) if len(frames) > 1 else frames[0]

    def describe(self, table: str) -> TableSpec | None:
        owner, _, name = table.upper().rpartition(".")
        with closing(self.connection.cursor()) as cursor, statement(self.database, _KIND, _DESCRIBE):
            cursor.execute(_DESCRIBE, {"owner": owner or None, "name": name})
            rows = cursor.fetchall()
        if not rows:
            return None
        return TableSpec("oracle", table, {column.name: column for column in (_column(tuple(row)) for row in rows)})


_DESCRIBE = """
SELECT column_name, data_type, char_length, data_precision, data_scale, nullable, data_default, identity_column, virtual_column
FROM all_tab_cols
WHERE owner = COALESCE(:owner, SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')) AND table_name = :name AND hidden_column = 'NO'
ORDER BY column_id
"""


def _column(row: Sequence[object]) -> DestColumn:
    name, data_type, char_length, precision, scale, nullable, default, identity, virtual = row
    kind = str(data_type).upper().split("(", 1)[0] + (" WITH TIME ZONE" if "WITH TIME ZONE" in str(data_type).upper() else "")
    text = kind in {"CHAR", "NCHAR", "VARCHAR2", "NVARCHAR2"}
    return DestColumn(
        name=str(name).lower(),
        type=kind,
        length=int(str(char_length)) if text else None,
        precision=int(str(precision)) if precision is not None else None,
        scale=int(str(scale)) if scale is not None else None,
        nullable=nullable == "Y",
        generated=identity == "YES" or virtual == "YES",
        defaulted=default is not None and bool(str(default).strip()),
    )


@dataclass
class OracleWriter(OracleReader):
    ledger: str
    committed: bool = False

    def published(self, run_id: str) -> bool:
        sql = f"SELECT COUNT(*) FROM {self.ledger} WHERE run_id = :run_id"
        with closing(self.connection.cursor()) as cursor, statement(self.database, _KIND, sql):
            cursor.execute(sql, {"run_id": run_id})
            row = cursor.fetchone()
        return row is not None and int(str(row[0])) > 0

    def overlapping(self, job: str, output: str, interval: Interval) -> list[str]:
        binds = {"job": job, "output": output} | interval.binds()
        lock = f"LOCK TABLE {self.ledger} IN SHARE ROW EXCLUSIVE MODE"
        sql = f"SELECT run_id FROM {self.ledger} WHERE job = :job AND output = :output AND interval_start < :interval_end AND interval_end > :interval_start"
        with closing(self.connection.cursor()) as cursor:
            # Oracle has no range locks: a row lock covers only ledger rows that exist, so a second run of the same new
            # interval would pass. Locking the table holds every other append to this database until this one commits.
            with statement(self.database, _KIND, lock):
                cursor.execute(lock)
            with statement(self.database, _KIND, sql):
                cursor.execute(sql, binds)
                return sorted({str(row[0]) for row in cursor.fetchall()})

    def apply(self, target: Target, changes: Changes, spec: TableSpec) -> Counts:
        deleted = upserted = inserted_new = inserted = 0
        if changes.delete is not None:
            deleted = self._delete(target, changes.delete, spec)
        if changes.upsert is not None and changes.upsert.height:
            upserted = self._merge(target, _bound(target, changes.upsert, spec), update=True)
            check_count(f"the upsert into {target.table}", changes.upsert.height, upserted)
        if changes.insert_new is not None and changes.insert_new.height:
            inserted_new = self._merge(target, _bound(target, changes.insert_new, spec), update=False)
        if changes.insert is not None and changes.insert.height:
            frame = _bound(target, changes.insert, spec)
            inserted = self._many(f"INSERT INTO {target.table} ({', '.join(frame.columns)}) VALUES ({_positions(frame.width)})", frame)
            check_count(f"the insert into {target.table}", changes.insert.height, inserted)
        return Counts(deleted, upserted, inserted_new, inserted)

    def _many(self, sql: str, frame: pl.DataFrame) -> int:
        written = 0
        with closing(self.connection.cursor()) as cursor:
            for part in slices(frame):  # each batch a statement, and a span, of its own
                with statement(self.database, _KIND, sql) as traced:
                    cursor.executemany(sql, part)
                    traced.set_attribute("db.operation.batch.size", part.height)
                written += cursor.rowcount
        return written

    def _execute(self, sql: str, binds: Sequence[object] = ()) -> int:
        with closing(self.connection.cursor()) as cursor, statement(self.database, _KIND, sql):
            cursor.execute(sql, list(binds))
            return cursor.rowcount

    def _delete(self, target: Target, delete: Delete, spec: TableSpec) -> int:
        conditions: list[str] = []
        within: list[object] = []
        position = len(delete.matching.columns) if delete.matching is not None else 0
        if delete.within is not None:
            zone = target.column(delete.within.column).zone
            conditions.append(f"{delete.within.column} >= :{position + 1} AND {delete.within.column} < :{position + 2}")
            within = [naive_in_zone(delete.within.start, zone), naive_in_zone(delete.within.end, zone)]
        if delete.matching is None:
            where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
            return self._execute(f"DELETE FROM {target.table}{where}", within)
        matching = _bound(target, delete.matching.unique(), spec)
        matches = [f"{name} = :{index}" for index, name in enumerate(matching.columns, 1)]
        if within:
            matching = matching.with_columns(pl.lit(within[0]).alias("#start"), pl.lit(within[1]).alias("#end"))
        return self._many(f"DELETE FROM {target.table} WHERE {' AND '.join(matches + conditions)}", matching)

    def _merge(self, target: Target, frame: pl.DataFrame, *, update: bool) -> int:
        key = target.key or ()
        source = ", ".join(f":{index} AS {name}" for index, name in enumerate(frame.columns, 1))
        on = " AND ".join(f"t.{name} = s.{name}" for name in key)
        sql = f"MERGE INTO {target.table} t USING (SELECT {source} FROM dual) s ON ({on})"
        if update and (values := [name for name in frame.columns if name not in key]):
            sql += f" WHEN MATCHED THEN UPDATE SET {', '.join(f't.{name} = s.{name}' for name in values)}"
        sql += f" WHEN NOT MATCHED THEN INSERT ({', '.join(frame.columns)}) VALUES ({', '.join(f's.{name}' for name in frame.columns)})"
        return self._many(sql, frame)

    def record(self, entry: LedgerEntry) -> None:
        values = ledger_values(entry)
        self._execute(f"INSERT INTO {self.ledger} ({', '.join(LEDGER_COLUMNS)}) VALUES ({_positions(len(values))})", [int(value) if isinstance(value, bool) else value for value in values])

    def commit(self) -> None:
        with span(self.database, _KIND, "commit"):
            self.connection.commit()
        self.committed = True


@dataclass
class OracleStatements:
    database: str
    connection: Connection
    limit_s: int
    """The settings' `timeout_s`, which bounds every round trip's own."""
    max_rows: int
    writes: bool
    committed: bool = False

    def query(self, sql: str, binds: Mapping[str, Bind], *, timeout_s: float) -> pa.Table:
        self._deadline(timeout_s)
        batches: list[pa.Table] = []
        rows = 0
        for batch in self.connection.fetch_df_batches(sql, dict(binds) or None, size=FETCH_ROWS):
            table = pa.table(batch)
            rows += table.num_rows
            if rows > self.max_rows:
                what = f"a query of database {self.database}"
                raise TooManyRowsError(what, self.max_rows)
            batches.append(table)
        return lower_cased(pa.concat_tables(batches) if batches else pa.table({}))

    def execute(self, sql: str, binds: Mapping[str, Bind], *, timeout_s: float) -> int:
        self._refuse_reads("change rows")
        self._deadline(timeout_s)
        with closing(self.connection.cursor()) as cursor:
            cursor.execute(sql, dict(binds))
            return cursor.rowcount

    def commit(self) -> None:
        self._refuse_reads("commit")
        self.connection.commit()
        self.committed = True

    def _deadline(self, timeout_s: float) -> None:
        self.connection.call_timeout = max(1, math.ceil(min(timeout_s, self.limit_s) * 1000))  # milliseconds, per round trip

    def _refuse_reads(self, what: str) -> None:
        if not self.writes:
            msg = f"database {self.database}'s transaction only reads, so it cannot {what}: open it with writes=True, through the write login"
            raise RuntimeError(msg)


def _bound(target: Target, frame: pl.DataFrame, spec: TableSpec) -> pl.DataFrame:
    """A frame as Oracle binds it: datetimes naive in their zone, and booleans as 0 and 1 where the column is a NUMBER."""
    frame = for_writing(target, frame)
    numbers = [name for name in frame.columns if target.column(name).kind == "bool" and (found := spec.columns.get(name)) is not None and found.type == "NUMBER"]
    return frame.with_columns(pl.col(numbers).cast(pl.Int8)) if numbers else frame


def _positions(count: int) -> str:
    return ", ".join(f":{index}" for index in range(1, count + 1))
