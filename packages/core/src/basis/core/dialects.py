"""Each SQL database dialect basis reads and writes, as one object: what its types are named and which take each
kind of a row's column, the words it reserves, the DDL basis prints for a row and for the ledger, how a statement
binds a value, and the query that checks a login. A dialect's reads, writes and ledger are its module in
`basis.core.databases`, which shares what both do through `basis.core.databases.base`; a third dialect is an object
here, a module there, its settings in `basis.core.settings`, and its container in the integration tier. Pure: these
only describe."""

import re
from dataclasses import dataclass
from typing import Literal

from basis.core.tables import Kind

type Dialect = Literal["mssql", "oracle"]
"""A dialect's name, as a database's settings give it in `BASIS_DATABASES__NAME__KIND`."""

ORACLE_VARCHAR_LIMIT = 4000
"""The most characters an Oracle VARCHAR2 holds without extended data types."""

_WHOLE = frozenset({"TINYINT", "SMALLINT", "INT", "BIGINT"})


@dataclass(frozen=True)
class SqlDialect:
    """One dialect."""

    name: Dialect
    title: str
    """Its name as people know it, as `SQL Server`."""
    bind_format: str
    """A named bind's text, with `{}` for its name: `%({})s` or `:{}`."""
    bind_pattern: str
    """A regular expression with a `{name}` field, that finds a named bind in a query."""
    types: dict[Kind, frozenset[str]]
    """The types that take each kind of column's values (`basis.core.destination`)."""
    reserved: frozenset[str]
    """The words it reserves, which an unquoted column may not be named."""
    fixed: dict[Kind, str]
    """The type `basis ddl` declares a column of each kind but text as."""
    json_type: str
    ledger_columns: tuple[str, ...]
    """The ledger's columns, in the order `basis.core.databases.base.LEDGER_COLUMNS` writes them."""
    lock_table: bool
    """Whether a publish's lock is a row of a table beside the ledger (`basis.core.ddl.locks`), as on Oracle, whose own
    locks need a grant a login seldom has, rather than an application lock that needs no table."""
    empty_is_null: bool
    """Whether it stores an empty string as NULL, as Oracle does."""
    unicode_column: str
    """The fix for text a column's code page cannot store."""
    charsets_at: str
    """Where basis learns which characters a character set stores."""
    ping: str
    """A query that reads no table, which checks a login."""
    schema_indexes: bool
    """Whether an index is named in its table's schema, as Oracle's are, rather than in its table."""

    def bind(self, name: str) -> str:
        return self.bind_format.format(name)

    def binds(self, sql: str, name: str) -> bool:
        """Whether `sql` binds `name`."""
        return re.search(self.bind_pattern.format(name=name), sql) is not None

    def text_type(self, max_length: int | None) -> str:
        """The type `basis ddl` declares a text column as, holding `max_length` characters, or any number."""
        if self.name == "mssql":
            return f"NVARCHAR({max_length if max_length is not None else 'MAX'})"
        return f"VARCHAR2({max_length} CHAR)" if max_length is not None and max_length <= ORACLE_VARCHAR_LIMIT else "CLOB"

    def first(self, columns: str, table: str, where: str, limit: str) -> str:
        """A query of the first `limit` rows `where` finds, `limit` a bind's text."""
        if self.name == "mssql":
            return f"SELECT TOP ({limit}) {columns} FROM {table} {where}"
        return f"SELECT {columns} FROM {table} {where} FETCH FIRST {limit} ROWS ONLY"


_MSSQL_LEDGER = (
        "run_id CHAR(36) NOT NULL", "job NVARCHAR(200) NOT NULL", "output NVARCHAR(128) NOT NULL", "table_name NVARCHAR(261) NOT NULL",
        "interval_start DATETIME2 NULL", "interval_end DATETIME2 NULL", "rerun_safe BIT NOT NULL", "deleted BIGINT NOT NULL",
        "upserted BIGINT NOT NULL", "inserted_new BIGINT NOT NULL", "inserted BIGINT NOT NULL", "published_at DATETIME2 NOT NULL",
        "etl_version NVARCHAR(50) NOT NULL", "detail NVARCHAR(MAX) NULL",
)  # fmt: skip

MSSQL = SqlDialect(
    name="mssql",
    title="SQL Server",
    bind_format="%({})s",
    bind_pattern=r"%\({name}\)s",
    types={
        "float": frozenset({"FLOAT", "REAL", "DECIMAL", "NUMERIC"}) | _WHOLE,
        "str": frozenset({"CHAR", "VARCHAR", "NCHAR", "NVARCHAR", "TEXT", "NTEXT"}) | _WHOLE,
        "bool": frozenset({"BIT"}),
        "date": frozenset({"DATE", "DATETIME2", "DATETIME", "SMALLDATETIME"}),
        "datetime": frozenset({"DATETIME2", "DATETIME", "SMALLDATETIME"}),
    },
    reserved=frozenset(
        """ADD ALL ALTER AND ANY AS ASC AUTHORIZATION BACKUP BEGIN BETWEEN BREAK BROWSE BULK BY CASCADE CASE CHECK
        CHECKPOINT CLOSE CLUSTERED COALESCE COLLATE COLUMN COMMIT COMPUTE CONSTRAINT CONTAINS CONTAINSTABLE CONTINUE
        CONVERT CREATE CROSS CURRENT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP CURRENT_USER CURSOR DATABASE DBCC
        DEALLOCATE DECLARE DEFAULT DELETE DENY DESC DISK DISTINCT DISTRIBUTED DOUBLE DROP DUMP ELSE END ERRLVL ESCAPE
        EXCEPT EXEC EXECUTE EXISTS EXIT EXTERNAL FETCH FILE FILLFACTOR FOR FOREIGN FREETEXT FREETEXTTABLE FROM FULL
        FUNCTION GOTO GRANT GROUP HAVING HOLDLOCK IDENTITY IDENTITY_INSERT IDENTITYCOL IF IN INDEX INNER INSERT
        INTERSECT INTO IS JOIN KEY KILL LEFT LIKE LINENO LOAD MERGE NATIONAL NOCHECK NONCLUSTERED NOT NULL NULLIF OF
        OFF OFFSETS ON OPEN OPENDATASOURCE OPENQUERY OPENROWSET OPENXML OPTION OR ORDER OUTER OVER PERCENT PIVOT PLAN
        PRECISION PRIMARY PRINT PROC PROCEDURE PUBLIC RAISERROR READ READTEXT RECONFIGURE REFERENCES REPLICATION
        RESTORE RESTRICT RETURN REVERT REVOKE RIGHT ROLLBACK ROWCOUNT ROWGUIDCOL RULE SAVE SCHEMA SECURITYAUDIT SELECT
        SEMANTICKEYPHRASETABLE SEMANTICSIMILARITYDETAILSTABLE SEMANTICSIMILARITYTABLE SESSION_USER SET SETUSER
        SHUTDOWN SOME STATISTICS SYSTEM_USER TABLE TABLESAMPLE TEXTSIZE THEN TO TOP TRAN TRANSACTION TRIGGER TRUNCATE
        TRY_CONVERT TSEQUAL UNION UNIQUE UNPIVOT UPDATE UPDATETEXT USE USER VALUES VARYING VIEW WAITFOR WHEN WHERE
        WHILE WITH WITHIN WRITETEXT""".split()  # noqa: SIM905 -- a list of words, which a string holds legibly
    ),
    fixed={"float": "FLOAT", "bool": "BIT", "date": "DATE", "datetime": "DATETIME2(6)"},
    json_type="JSON",
    ledger_columns=_MSSQL_LEDGER,
    lock_table=False,
    empty_is_null=False,
    unicode_column="store them in a Unicode column, NVARCHAR or a VARCHAR with a UTF-8 collation",
    charsets_at="Python's codecs",
    ping="SELECT 1 AS answered",
    schema_indexes=False,
)
"""SQL Server's: its reserved keywords."""

_ORACLE_LEDGER = (
        "run_id CHAR(36) NOT NULL", "job VARCHAR2(200 CHAR) NOT NULL", "output VARCHAR2(128 CHAR) NOT NULL", "table_name VARCHAR2(261 CHAR) NOT NULL",
        "interval_start TIMESTAMP NULL", "interval_end TIMESTAMP NULL", "rerun_safe NUMBER(1) NOT NULL", "deleted NUMBER(19) NOT NULL",
        "upserted NUMBER(19) NOT NULL", "inserted_new NUMBER(19) NOT NULL", "inserted NUMBER(19) NOT NULL", "published_at TIMESTAMP NOT NULL",
        "etl_version VARCHAR2(50 CHAR) NOT NULL", "detail CLOB NULL",
)  # fmt: skip

ORACLE = SqlDialect(
    name="oracle",
    title="Oracle",
    bind_format=":{}",
    bind_pattern=r"(?<![:\w]):{name}\b",
    types={
        "float": frozenset({"BINARY_DOUBLE", "BINARY_FLOAT", "FLOAT", "NUMBER"}),
        "str": frozenset({"CHAR", "NCHAR", "VARCHAR2", "NVARCHAR2", "CLOB", "NCLOB", "NUMBER"}),
        "bool": frozenset({"BOOLEAN", "NUMBER"}),
        "date": frozenset({"DATE", "TIMESTAMP"}),
        "datetime": frozenset({"TIMESTAMP", "DATE"}),
    },
    reserved=frozenset(
        """ACCESS ADD ALL ALTER AND ANY AS ASC AUDIT BETWEEN BY CHAR CHECK CLUSTER COLUMN COMMENT COMPRESS CONNECT CREATE
        CURRENT DATE DECIMAL DEFAULT DELETE DESC DISTINCT DROP ELSE EXCEPT EXCLUSIVE EXISTS FILE FLOAT FOR FROM GRANT
        GROUP HAVING IDENTIFIED IMMEDIATE IN INCREMENT INDEX INITIAL INSERT INTEGER INTERSECT INTO IS LEVEL LIKE LOCK
        LONG MAXEXTENTS MINUS MLSLABEL MODE MODIFY NOAUDIT NOCOMPRESS NOT NOWAIT NULL NUMBER OF OFFLINE ON ONLINE
        OPTION OR ORDER PCTFREE PRIOR PUBLIC RAW RENAME RESOURCE REVOKE ROW ROWID ROWNUM ROWS SELECT SESSION SET SHARE
        SIZE SMALLINT START SUCCESSFUL SYNONYM SYSDATE TABLE THEN TO TRIGGER UID UNION UNIQUE UPDATE USER VALIDATE
        VALUES VARCHAR VARCHAR2 VIEW WHENEVER WHERE WITH""".split()  # noqa: SIM905 -- as above
    ),
    fixed={"float": "BINARY_DOUBLE", "bool": "NUMBER(1)", "date": "DATE", "datetime": "TIMESTAMP(6)"},
    json_type="CLOB",
    ledger_columns=_ORACLE_LEDGER,
    lock_table=True,
    empty_is_null=True,
    unicode_column="store them in an NVARCHAR2 or NCLOB column, whose national character set is Unicode",
    charsets_at="basis.core.databases.oracle.CHARSETS",
    ping="SELECT 1 AS answered FROM DUAL",
    schema_indexes=True,
)
"""Oracle's: its reserved words, 23ai's `V$RESERVED_WORDS` with those 19c reserves too."""

DIALECTS: dict[Dialect, SqlDialect] = {"mssql": MSSQL, "oracle": ORACLE}


def of(name: Dialect) -> SqlDialect:
    return DIALECTS[name]


_UNBOUND = re.compile(r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'|\"[^\"]*\"", re.DOTALL)
"""What holds no bind: a comment, a string literal, and a quoted identifier."""
_BIND = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)|%\(([A-Za-z_]\w*)\)s")


def named_binds(sql: str) -> dict[Dialect, set[str]]:
    """Each name `sql` binds, by the dialect whose form it is in, Oracle's `:name` or SQL Server's `%(name)s`, outside its
    comments and literals; a cast's `::` is none. A dialect it binds nothing in is left out."""
    found: dict[Dialect, set[str]] = {}
    for oracle, mssql in _BIND.findall(_UNBOUND.sub(" ", sql)):
        found.setdefault("oracle" if oracle else "mssql", set()).add(oracle or mssql)
    return found
