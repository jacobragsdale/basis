"""The databases a run reads and writes: SQL Server and Oracle, each named by the settings that configure it."""

from basis.core.databases.base import Counts, Database, LedgerEntry, Reader, TooManyRowsError, Writer
from basis.core.databases.mssql import MssqlDatabase
from basis.core.databases.oracle import OracleDatabase
from basis.core.settings import MssqlSettings, Settings

__all__ = ["Counts", "Database", "LedgerEntry", "Reader", "TooManyRowsError", "Writer", "open_database"]


def open_database(settings: Settings, name: str) -> Database:
    """The database a job names, as the settings configure it. Opening connects to nothing."""
    found = settings.database(name)
    return MssqlDatabase(name, found) if isinstance(found, MssqlSettings) else OracleDatabase(name, found)
