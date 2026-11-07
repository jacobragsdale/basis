"""The databases a run reads and writes: SQL Server and Oracle, each named by the settings that configure it."""

from etl_framework.databases.base import Counts, Database, LedgerEntry, Reader, TooManyRowsError, Writer
from etl_framework.databases.mssql import MssqlDatabase
from etl_framework.databases.oracle import OracleDatabase
from etl_framework.settings import MssqlSettings, Settings

__all__ = ["Counts", "Database", "LedgerEntry", "Reader", "TooManyRowsError", "Writer", "open_database"]


def open_database(settings: Settings, name: str) -> Database:
    """The database a job names, as the settings configure it. Opening connects to nothing."""
    found = settings.database(name)
    return MssqlDatabase(name, found) if isinstance(found, MssqlSettings) else OracleDatabase(name, found)
