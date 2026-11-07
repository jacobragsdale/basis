"""Oracle through python-oracledb. A query binds the interval as `:interval_start` and `:interval_end`. Reads arrive as Arrow, with
`NUMBER(p,s)` as decimals; an unconstrained `NUMBER` arrives as a float, so a row that needs it exact casts it in its
query, as `CAST(x AS NUMBER(18,4))`.

Thin mode, the default, needs no client libraries. Thick mode loads Oracle Client libraries, which native network
encryption and older servers need. A write binds whole frames to batched statements, with no staging table: an
upsert and an insert of new rows are a `MERGE` per row, from `dual`, and a delete by values is a `DELETE` per row.

`statements` runs a caller's own SQL, binding `:name` by name: each round trip's deadline is the driver's call
timeout, in milliseconds, after which the driver breaks the call off."""

import logging
import math
import threading
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from concurrent.futures import Future, wait
from contextlib import closing, contextmanager
from dataclasses import dataclass
from functools import partial

import oracledb
import polars as pl
import pyarrow as pa

from basis.core.databases.base import FETCH_ROWS, LEDGER_COLUMNS, Bind, Counts, LedgerEntry, TooManyRowsError, binds_named, check_count, for_writing, ledger_values, lower_cased, naive_in_zone, slices
from basis.core.destination import DestColumn, TableSpec
from basis.core.interval import Interval
from basis.core.logs import ms_since
from basis.core.settings import OracleSettings
from basis.core.tables import Changes, Delete, Target

log = logging.getLogger(__name__)
CONNECT_TIMEOUT_S = 30
"""The longest a login waits, however long its round trips may take."""
_BIND = r"(?<![:\w]):{name}\b"


def connect(settings: OracleSettings, *, user: str, password: str) -> oracledb.Connection:
    """A connection with a deadline on its login and on every round trip. Thin mode bounds only the TCP connect: a
    database that accepts TCP but never answers the login would hang the run, so the login runs in a thread that the
    run stops waiting for."""
    if settings.mode == "thick":
        oracledb.init_oracle_client(lib_dir=str(settings.client_lib_dir) if settings.client_lib_dir is not None else None)
    login = partial(oracledb.connect, user=user, password=password, dsn=settings.dsn)
    connected: Future[oracledb.Connection] = Future()
    threading.Thread(target=_resolve, args=(connected, login), daemon=True).start()
    if not wait([connected], timeout=(deadline := min(CONNECT_TIMEOUT_S, settings.timeout_s))).done:
        msg = f"Oracle at {settings.dsn} accepted a connection but did not log in within {deadline}s: the listener or the database is hung"
        raise TimeoutError(msg)
    connection = connected.result()
    connection.call_timeout = settings.timeout_s * 1000
    return connection


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
        return self.settings.write is not None and self.settings.write.ledger is not None

    @contextmanager
    def reading(self) -> Generator[OracleReader]:
        with self._read_transaction(level=logging.INFO) as connection:
            yield OracleReader(connection, self.settings.max_rows)

    @contextmanager
    def writing(self) -> Generator[OracleWriter]:
        write = self.settings.write
        if write is None:
            msg = f"database {self.name!r} has no write settings: set BASIS_DATABASES__{self.name.upper()}__WRITE__USER, __PASSWORD and __LEDGER"
            raise LookupError(msg)
        if write.ledger is None:
            msg = f"database {self.name!r} has no ledger, which a publish writes in its transaction: set BASIS_DATABASES__{self.name.upper()}__WRITE__LEDGER"
            raise LookupError(msg)
        start = time.perf_counter()
        with closing(connect(self.settings, user=write.user, password=write.password.get_secret_value())) as connection:
            log.info("connected", extra={"database": self.name, "access": "write", "connect_ms": ms_since(start)})
            writer = OracleWriter(connection, self.settings.max_rows, write.ledger)
            try:
                yield writer
            finally:
                if not writer.committed:
                    connection.rollback()

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
        with closing(connect(self.settings, user=write.user, password=write.password.get_secret_value())) as connection:
            log.debug("connected", extra={"database": self.name, "access": "write", "connect_ms": ms_since(start)})
            statements = OracleStatements(self.name, connection, self.settings.timeout_s, self.settings.max_rows, writes=True)
            try:
                yield statements
            finally:
                if not statements.committed:
                    connection.rollback()

    @contextmanager
    def _read_transaction(self, *, level: int) -> Generator[oracledb.Connection]:
        """A connection through the read login, in a transaction at the settings' isolation, rolled back at the end."""
        start = time.perf_counter()
        with closing(connect(self.settings, user=self.settings.user, password=self.settings.password.get_secret_value())) as connection:
            log.log(level, "connected", extra={"database": self.name, "access": "read", "connect_ms": ms_since(start)})
            if self.settings.isolation == "read_only":
                with closing(connection.cursor()) as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
            try:
                yield connection
            finally:
                connection.rollback()


@dataclass
class OracleReader:
    connection: oracledb.Connection
    max_rows: int

    def fetch(self, sql: str, interval: Interval | None, *, what: str) -> pl.DataFrame:
        binds = binds_named(sql, _BIND, interval, what=what)
        frames: list[pl.DataFrame] = []
        rows = 0
        try:
            for batch in self.connection.fetch_df_batches(sql, binds or None, size=10_000, fetch_decimals=True):
                frame = pl.DataFrame(batch)
                rows += frame.height
                if rows > self.max_rows:
                    raise TooManyRowsError(what, self.max_rows)
                frames.append(frame)
        except oracledb.DatabaseError as error:
            if "ORA-01466" in str(error):
                error.add_note(
                    "a table this query reads changed its definition (a TRUNCATE counts) seconds before the run's read-only transaction began:"
                    " run again, or set the database's ISOLATION=read_committed"
                )
            raise
        return pl.concat(frames) if len(frames) > 1 else frames[0]

    def describe(self, table: str) -> TableSpec | None:
        owner, _, name = table.upper().rpartition(".")
        with closing(self.connection.cursor()) as cursor:
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
        with closing(self.connection.cursor()) as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {self.ledger} WHERE run_id = :run_id", {"run_id": run_id})
            row = cursor.fetchone()
        return row is not None and int(str(row[0])) > 0

    def overlapping(self, job: str, output: str, interval: Interval) -> list[str]:
        binds = {"job": job, "output": output} | interval.binds()
        with closing(self.connection.cursor()) as cursor:
            # Oracle has no range locks: a row lock covers only ledger rows that exist, so a second run of the same new
            # interval would pass. Locking the table holds every other append to this database until this one commits.
            cursor.execute(f"LOCK TABLE {self.ledger} IN SHARE ROW EXCLUSIVE MODE")
            cursor.execute(f"SELECT run_id FROM {self.ledger} WHERE job = :job AND output = :output AND interval_start < :interval_end AND interval_end > :interval_start", binds)
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

    def _many(self, statement: str, frame: pl.DataFrame) -> int:
        written = 0
        with closing(self.connection.cursor()) as cursor:
            for part in slices(frame):
                cursor.executemany(statement, part)
                written += cursor.rowcount
        return written

    def _execute(self, statement: str, binds: Sequence[object] = ()) -> int:
        with closing(self.connection.cursor()) as cursor:
            cursor.execute(statement, list(binds))
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
        statement = f"MERGE INTO {target.table} t USING (SELECT {source} FROM dual) s ON ({on})"
        if update and (values := [name for name in frame.columns if name not in key]):
            statement += f" WHEN MATCHED THEN UPDATE SET {', '.join(f't.{name} = s.{name}' for name in values)}"
        statement += f" WHEN NOT MATCHED THEN INSERT ({', '.join(frame.columns)}) VALUES ({', '.join(f's.{name}' for name in frame.columns)})"
        return self._many(statement, frame)

    def record(self, entry: LedgerEntry) -> None:
        values = ledger_values(entry)
        self._execute(f"INSERT INTO {self.ledger} ({', '.join(LEDGER_COLUMNS)}) VALUES ({_positions(len(values))})", [int(value) if isinstance(value, bool) else value for value in values])

    def commit(self) -> None:
        self.connection.commit()
        self.committed = True


@dataclass
class OracleStatements:
    database: str
    connection: oracledb.Connection
    limit_s: int
    """The settings' `timeout_s`, which bounds every round trip's own."""
    max_rows: int
    writes: bool
    committed: bool = False

    def query(self, sql: str, binds: Mapping[str, Bind], *, timeout_s: float) -> pa.Table:
        self._deadline(timeout_s)
        batches: list[pa.Table] = []
        rows = 0
        for batch in self.connection.fetch_df_batches(sql, dict(binds) or None, size=FETCH_ROWS, fetch_decimals=True):
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
