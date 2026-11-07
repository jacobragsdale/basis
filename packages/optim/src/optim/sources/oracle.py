"""Oracle through python-oracledb. Queries bind `:name`. NUMBER columns arrive as floats, or ints where whole.

Thin mode, the default, needs no client libraries. Thick mode loads Oracle Client libraries, which some databases need,
such as for native network encryption or an older server: install Oracle Instant Client in the image and set
`OPTIM_ORACLE__MODE=thick`, with `OPTIM_ORACLE__CLIENT_LIB_DIR` where it is not on the library path."""

import threading
from collections.abc import Callable
from concurrent.futures import Future, wait
from functools import partial
from pathlib import Path
from typing import Literal

import oracledb
from pydantic import SecretStr

from optim.sources import SourceSettings
from optim.sources.sql import SqlSource

CONNECT_TIMEOUT_S = 30  # the longest a login waits, however long its round trips may take
# Statement-level consistency becomes transaction-level: every query in the transaction sees the database as it began.
BEGIN_READ_ONLY = "SET TRANSACTION READ ONLY"


class OracleSettings(SourceSettings):
    dsn: str
    user: str
    password: SecretStr
    mode: Literal["thin", "thick"]
    client_lib_dir: Path | None = None  # thick mode: where Oracle Client is, if not on the library path


def connect(settings: OracleSettings) -> SqlSource:
    """A connection per query, or per read-only transaction, as for SQL Server: a run makes a handful. Not a pool: its
    background thread joins a connection attempt that never ends, so a hung database would keep the process from
    exiting."""
    if settings.mode == "thick":
        # once per process, before the first connection; a second call with the same arguments does nothing
        oracledb.init_oracle_client(lib_dir=str(settings.client_lib_dir) if settings.client_lib_dir else None)
    return SqlSource(partial(_connect, settings), BEGIN_READ_ONLY, settings.max_rows)


def _connect(settings: OracleSettings) -> oracledb.Connection:
    """A connection with a deadline on its login, `timeout_s` and at most `CONNECT_TIMEOUT_S`, and on every round trip.
    Thin mode bounds only the TCP connect: a database that accepts TCP but never answers the login would hang the run,
    so the login runs in a thread that the run stops waiting for."""
    login = partial(oracledb.connect, user=settings.user, password=settings.password.get_secret_value(), dsn=settings.dsn)
    connected: Future[oracledb.Connection] = Future()
    threading.Thread(target=_resolve, args=(connected, login), daemon=True).start()
    if not wait([connected], timeout=(deadline := min(CONNECT_TIMEOUT_S, settings.timeout_s))).done:
        msg = f"Oracle at {settings.dsn} accepted a connection but did not log in within {deadline}s: the listener or the database is hung"
        raise TimeoutError(msg)
    connection = connected.result()
    connection.call_timeout = settings.timeout_s * 1000  # milliseconds, per round trip
    return connection


def _resolve[T](future: Future[T], call: Callable[[], T]) -> None:
    try:
        future.set_result(call())
    except Exception as error:  # noqa: BLE001 -- handed to the waiting caller, which raises it
        future.set_exception(error)
