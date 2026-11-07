"""`CREATE TABLE` statements for the tables a part writes, from their columns, and for the ledger, which every part
that publishes writes beside them. A run never creates or alters a table: these are printed for whoever has the rights
to run them, by `basis ddl`: a job's tables, optim's orders table, and the ledger. A string column without a
maximum length is unbounded, and says so in a comment beside it. Pure: these only build text."""

from basis.core.destination import Dialect
from basis.core.tables import Column, Kind, Target

ORACLE_VARCHAR_LIMIT = 4000
"""The most characters an Oracle VARCHAR2 holds without extended data types."""


def create_table(target: Target, dialect: Dialect) -> str:
    items = [(f"{column.name} {_type(column, dialect)}{'' if column.nullable else ' NOT NULL'}", _note(column)) for column in target.columns]
    if target.key is not None:
        bare = target.table.rsplit(".", 1)[-1].lower()
        items.append((f"CONSTRAINT pk_{bare} PRIMARY KEY ({', '.join(target.key)})", ""))
    lines = [f"    {definition}{',' if index < len(items) - 1 else ''}{note}" for index, (definition, note) in enumerate(items)]
    return f"CREATE TABLE {target.table} (\n" + "\n".join(lines) + "\n);\n"


_LEDGER: dict[Dialect, list[str]] = {
    "mssql": [
        "run_id CHAR(36) NOT NULL", "job NVARCHAR(200) NOT NULL", "output NVARCHAR(128) NOT NULL", "table_name NVARCHAR(261) NOT NULL",
        "interval_start DATETIME2 NULL", "interval_end DATETIME2 NULL", "rerun_safe BIT NOT NULL", "deleted BIGINT NOT NULL",
        "upserted BIGINT NOT NULL", "inserted_new BIGINT NOT NULL", "inserted BIGINT NOT NULL", "published_at DATETIME2 NOT NULL",
        "etl_version NVARCHAR(50) NOT NULL",
    ],
    "oracle": [
        "run_id CHAR(36) NOT NULL", "job VARCHAR2(200 CHAR) NOT NULL", "output VARCHAR2(128 CHAR) NOT NULL", "table_name VARCHAR2(261 CHAR) NOT NULL",
        "interval_start TIMESTAMP NULL", "interval_end TIMESTAMP NULL", "rerun_safe NUMBER(1) NOT NULL", "deleted NUMBER(19) NOT NULL",
        "upserted NUMBER(19) NOT NULL", "inserted_new NUMBER(19) NOT NULL", "inserted NUMBER(19) NOT NULL", "published_at TIMESTAMP NOT NULL",
        "etl_version VARCHAR2(50 CHAR) NOT NULL",
    ],
}  # fmt: skip
"""The ledger's columns, in the order `basis.core.databases.base.LEDGER_COLUMNS` writes them."""


def ledger(table: str, dialect: Dialect) -> str:
    """The ledger, one table per database a part publishes to: a row per table each run published, in the transaction
    that wrote it, keyed by run and output, and indexed for the rerun guard's lookup by job, output and interval."""
    bare = table.rsplit(".", 1)[-1].lower()
    columns = ",\n".join(f"    {column}" for column in _LEDGER[dialect])
    return f"CREATE TABLE {table} (\n{columns},\n    CONSTRAINT pk_{bare} PRIMARY KEY (run_id, output)\n);\nCREATE INDEX ix_{bare}_interval ON {table} (job, output, interval_start, interval_end);\n"


_FIXED: dict[tuple[Kind, Dialect], str] = {
    ("int", "mssql"): "BIGINT",
    ("int", "oracle"): "NUMBER(19)",
    ("float", "mssql"): "FLOAT",
    ("float", "oracle"): "BINARY_DOUBLE",
    ("bool", "mssql"): "BIT",
    ("bool", "oracle"): "NUMBER(1)",
    ("date", "mssql"): "DATE",
    ("date", "oracle"): "DATE",
    ("datetime", "mssql"): "DATETIME2(6)",
    ("datetime", "oracle"): "TIMESTAMP(6)",
}


def _type(column: Column, dialect: Dialect) -> str:
    if column.kind == "decimal":
        return f"{'DECIMAL' if dialect == 'mssql' else 'NUMBER'}({column.precision},{column.scale})"
    if column.kind == "str" and dialect == "mssql":
        return f"NVARCHAR({column.max_length if column.max_length is not None else 'MAX'})"
    if column.kind == "str":
        return f"VARCHAR2({column.max_length} CHAR)" if column.max_length is not None and column.max_length <= ORACLE_VARCHAR_LIMIT else "CLOB"
    return _FIXED[column.kind, dialect]


def _note(column: Column) -> str:
    notes: list[str] = []
    if column.kind == "str" and column.max_length is None:
        notes.append("no MaxLength in the row: unbounded")
    if column.kind == "datetime":
        notes.append(f"naive, in {column.zone}")
    return f"  -- {'; '.join(notes)}" if notes else ""
