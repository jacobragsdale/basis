# Spike 9.5: reading a query's columns with sqlglot

Can sqlglot parse each job's SQL file in its database's dialect, and say which columns it selects, so `basis check`
could compare them with the query's row? Partly: it parses every form tried below and names each selected column by
its alias, but not SQL Server's binds as basis writes them, and not a `SELECT *`. Run on 2026-11-11 against the
example job's two queries and thirteen made-up statements of each dialect's own syntax. Nothing of the firm's was
parsed, so, as the plan says, it reports only until it has read the firm's real queries, and basis ships nothing of it
yet: no dependency, no check.

## Versions

sqlglot 30.31.0 (the version `uvx --with sqlglot` resolved that day), Python 3.14, the `tsql` and `oracle` dialects.

## Answers

| Question | Answer | Fallback for a no |
|---|---|---|
| Does it parse a SQL Server query with basis's binds, `%(interval_start)s`? | No: `ParseError: Required keyword: 'this' missing for Mod`, since `%` is T-SQL's modulo | Rewrite each `%(name)s` as `@name` first, a T-SQL variable, which parses, and leaves a `'%x%'` literal alone; both example queries then parse |
| Does it parse an Oracle query with `:interval_start`? | Yes | |
| Does it name each selected column, by its alias where it has one? | Yes: `t.account_id AS acct` is `acct`, `price * quantity AS notional` is `notional` | |
| Does it parse T-SQL's own syntax: `TOP`, `WITH (NOLOCK)`, `[brackets]`, `CROSS APPLY`, a CTE, `PIVOT`, a `SELECT INTO #t` batch? | Yes, each; of a batch it names the last statement's columns | |
| Does it parse a `DECLARE` before the query? | Yes, with a warning that it fell back to a `Command` for that statement | |
| Does it parse Oracle's `CONNECT BY` and `LEVEL`, `(+)` joins, `NVL`, `DECODE`, `q'[...]'` literals? | Yes, each | |
| Does it say which columns `SELECT *` gives? | No: it gives `*`, since that needs the table's schema | Report the query as unchecked, and leave it to the fixture run and the destination's checks, as today |
| Has it parsed the firm's queries? | No: that needs the firm's repos | The **(work)** step below |

A parser that refused valid vendor SQL would teach agents to ignore it, which is why the check stays a report: none of
the made-up statements was refused, but they are a sample, not the firm's dialect as written.

## The check it would make

For each `etl.Query`, parse its file in its database's dialect (`tsql` for `mssql`, `oracle`), with SQL Server's
binds rewritten as `@name`; take the last statement's select list; and report each column the row declares that it
does not select, and each it selects that the row does not declare, by name, ignoring case. A query that does not
parse, or selects `*`, is reported as unchecked, never as a failure.

## (work) Run it on the firm's queries

In each of a few of the firm's job repos, with the script below saved as `sql_columns.py` beside it:

```sh
uv run --with sqlglot==30.31.0 python sql_columns.py > sql-columns.txt
```

Send back `sql-columns.txt`, with the database kind of each database it names. Each line is one query: `ok`,
`differs` with the columns, `unchecked` with why, or `refused` with sqlglot's error, which is the line that matters:
each `refused` of a query that runs is a reason to keep this a report.

```python
"""Each job's queries, parsed by sqlglot, with the columns each selects against its row's: a report, never a check."""

import re
import sys

import sqlglot
from sqlglot import exp

from basis.job import job as jobs
from basis.job.inputs import Query

DIALECTS = {"oracle": "oracle"}  # a database named here is Oracle; every other is SQL Server's tsql

for name in sorted(jobs.listed()):
    job = jobs.load(name)
    for dataset_name, dataset in job.inputs.declared().items():
        if not isinstance(dataset, Query):
            continue
        where = f"{job.name}.{dataset_name} ({dataset.sql})"
        dialect = DIALECTS.get(dataset.database, "tsql")
        text = re.sub(r"%\((\w+)\)s", r"@\1", dataset.text()) if dialect == "tsql" else dataset.text()
        try:
            statements = [tree for tree in sqlglot.parse(text, read=dialect) if tree is not None]
        except sqlglot.errors.ParseError as error:
            print(f"refused   {where}: {str(error).splitlines()[0]}")
            continue
        select = statements[-1].find(exp.Select) if statements else None
        if select is None:
            print(f"unchecked {where}: no SELECT")
            continue
        selected = {column.alias_or_name.lower() for column in select.expressions}
        if "*" in selected:
            print(f"unchecked {where}: SELECT *")
            continue
        declared = {column.lower() for column in dataset.row.names()}
        if selected == declared:
            print(f"ok        {where}")
        else:
            print(f"differs   {where}: not selected {sorted(declared - selected)}, not declared {sorted(selected - declared)}")
sys.exit(0)
```
