"""`CREATE TABLE` statements for the tables a part writes, from their columns, and for the ledger, which every part
that publishes writes beside them. A run never creates or alters a table: these are printed for whoever has the rights
to run them, by `basis ddl`: a part's tables, such as a job's, and the ledger. A string column without a
maximum length is unbounded, and says so in a comment beside it. Pure: these only build text."""

from basis.core.dialects import ORACLE_VARCHAR_LIMIT, Dialect, of
from basis.core.tables import Column, Target

__all__ = ["ORACLE_VARCHAR_LIMIT", "create_table", "ledger", "ledger_tables", "locks"]


def create_table(target: Target, dialect: Dialect) -> str:
    items = [(f"{column.name} {_type(column, dialect)}{'' if column.nullable else ' NOT NULL'}", _note(column)) for column in target.columns]
    if target.key is not None:
        bare = target.table.rsplit(".", 1)[-1].lower()
        items.append((f"CONSTRAINT pk_{bare} PRIMARY KEY ({', '.join(target.key)})", ""))
    lines = [f"    {definition}{',' if index < len(items) - 1 else ''}{note}" for index, (definition, note) in enumerate(items)]
    return f"CREATE TABLE {target.table} (\n" + "\n".join(lines) + "\n);\n"


def ledger(table: str, dialect: Dialect) -> str:
    """The ledger, one table per database a part publishes to: a row per table each run published, in the transaction
    that wrote it, keyed by run and output, and indexed for the rerun guard's lookup by job, output and interval. On
    Oracle, beside it, a row per output for the publish's lock (`locks`): SQL Server takes an application lock, which
    needs no table, and Oracle's own, `DBMS_LOCK`, needs a grant a login seldom has."""
    bare = table.rsplit(".", 1)[-1].lower()
    columns = ",\n".join(f"    {column}" for column in of(dialect).ledger_columns)
    text = f"CREATE TABLE {table} (\n{columns},\n    CONSTRAINT pk_{bare} PRIMARY KEY (run_id, output)\n);\nCREATE INDEX ix_{bare}_interval ON {table} (job, output, interval_start, interval_end);\n"
    if of(dialect).lock_table:
        text += f"CREATE TABLE {locks(table)} (\n    job VARCHAR2(200 CHAR) NOT NULL,\n    output VARCHAR2(128 CHAR) NOT NULL,\n    CONSTRAINT pk_{bare}_locks PRIMARY KEY (job, output)\n);\n"
    return text


def locks(ledger: str) -> str:
    """The table of Oracle's publish locks beside the ledger `ledger`."""
    return f"{ledger}_locks"


def ledger_tables(ledger: str, dialect: Dialect) -> tuple[str, ...]:
    """The tables `ledger` prints for the ledger `ledger`, which a publish needs."""
    return (ledger, locks(ledger)) if of(dialect).lock_table else (ledger,)


def _type(column: Column, dialect: Dialect) -> str:
    if column.json:
        return of(dialect).json_type
    if column.kind == "str":
        return of(dialect).text_type(column.max_length)
    return of(dialect).fixed[column.kind]


def _note(column: Column) -> str:
    notes: list[str] = []
    if column.json:
        notes.append("JSON text: SQL Server 2025 or Azure SQL" if column.max_length is None else "JSON text")
    elif column.kind == "str" and column.max_length is None:
        notes.append("no MaxLength in the row: unbounded")
    if column.kind == "datetime":
        notes.append(f"naive, in {column.zone}")
    return f"  -- {'; '.join(notes)}" if notes else ""
