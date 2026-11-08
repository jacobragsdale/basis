# The etl-framework design

etl-framework is a package that ETL job repos depend on, modelled on
[portfolio-optim](https://github.com/jacobragsdale/portfolio-optim)'s idea: the core owns the run loop, so job code is
pure and every run replays. It was designed on 2026-11-06 and built as 0.1.0 the same day. The docs for job authors,
in `src/basis/job/docs/`, describe the result; this page records the decisions and their reasons.

Since basis's step 1.3, etl-framework is basis-job, which imports as `basis.job`, and its plumbing is basis-core's:
the databases, settings, logs, storage, snapshots, publishing with the ledger, the shared halves of `replay`, `resume`
and `regress`, and the machinery of `new`, `sync` and the contract tests. [basis's design](../../../docs/design.md)
supersedes the decisions below about sharing a core with portfolio-optim, and about polars, whose frames became pandas
in basis's step 5.0b, as the table now says; the rest stand.

## The problem

Software developers and quants write a few hundred ETL jobs. Each reinvents the same plumbing, jobs fail on bad code
and bad checks, they are hard to test, and they are hard to observe. Their data usually fits in memory, they usually
read and write SQL Server or Oracle, and they do not feed each other.

## Decisions

| Decision | Reason |
|---|---|
| A package separate from portfolio-optim, sharing its patterns but not its code | The two are similar, but separate on purpose; the generic modules were adapted from optim rather than shared. |
| Generic: every table shape, every number of databases, nothing assumed | A framework for hundreds of jobs cannot lean on the common case. Every output names its write mode, every `Decimal` its precision, every `datetime` its zone, every input what empty means. |
| All or nothing | A run writes nothing unless every check passes for every output; each database commits in one transaction. |
| Runs in a container; no scheduler is assumed | The CLI is the entry point, `tini` the first process, the exit code the contract. Anything a scheduler wants recorded arrives as `--label`. |
| Four operations under every write mode: delete, upsert, insert of new keys, insert | Every write mode, and `Manual()`, reduces to them, so one writer per dialect covers every table. |
| No custom SQL write hook | A SQL side effect is the one write that a replay and `regress` could not see. A job that needs the table's current rows reads the table as an input. |
| A ledger table per destination database, written in the same transaction | It is never wrong about whether a publish happened. It guards plain appends against a second publish of an overlapping interval, and tells `resume` which databases have a run. It is the only DDL the framework asks for. |
| Databases commit one at a time, in name order; `resume` finishes a partial run from its snapshot | Distributed transactions are not dependable from these drivers; resuming the same rows gives the same end state. |
| No DDL at run time | Whether job logins have DDL rights is unknown, so runs never need them: SQL Server stages in `#temp` tables, Oracle binds frames to batched statements, and `basis ddl` prints the tables. |
| pandas, every column Arrow-backed (since basis's 5.0b; polars before it) | ETL moves values that must arrive exactly: integers with nulls, decimals at their scale, zone-aware datetimes. An Arrow-backed pandas column keeps each, where numpy's would make a nullable integer a float and a decimal an object, and data arrives and leaves as Arrow. Why pandas rather than polars is [basis's design](../../../docs/design.md#jobs-basisjob)'s. |
| No worker processes or partitions | Data fits in memory. optim's process pool exists for CPU-bound solves. |
| Destination checks against what the database says the table is | A drifted table fails a run in seconds, before it loads; a value too long or too precise fails it before the database would truncate it or fail halfway. |
| Read isolation is a required setting per database | SQL Server's snapshot isolation must be enabled, and Oracle's read-only transactions fail on a table truncated seconds before; the trade-off is each database's own. |

## Found while building

- Oracle reserves `START`, so `:start` is not a valid bind name (ORA-01745). The interval binds as `interval_start` and
  `interval_end` in both dialects.
- With autocommit off, mssql-python's implicit transaction begins at `SET TRANSACTION ISOLATION LEVEL SNAPSHOT` itself,
  under the default level, and SQL Server then refuses snapshot reads of user tables. Reads connect with autocommit on
  and begin the transaction explicitly. portfolio-optim's `sources/mssql.py` reads the same way it first did here, and
  likely has the same defect.
- mssql-python's bulk copy opens a connection of its own, outside the run's transaction, so writes use `executemany`.
  Its own typing, in 1.17.0, types each column from the column's longest value: one whose longest is ASCII binds as
  `varchar`, losing what the database's code page cannot hold without an error, and a batch with a string past 4,000
  UTF-16 units counts one row. So writes give each column's type, every string as `nvarchar`, and send such a row by
  itself (docs/spikes/sqlserver-json.md, section 3).
- Oracle's read-only transactions raise ORA-01466 for a table whose definition changed in the last few seconds, which
  is why Oracle's isolation is a setting.
- polars rounded a decimal cast to a smaller scale and truncated a decimal cast to an integer, even with `strict=True`,
  so `conform` cast back and compared. Since basis's 5.0b, pyarrow's safe casts refuse both, and a value that would
  overflow or lose a fraction of a microsecond, but cast a datetime to a date by dropping its time of day, and make
  any integer but 0 a true boolean, so `basis.core.frames.convert` checks those two itself.
- pandas makes `[1, None]` a float column, since numpy's integers have no null, and `conform` refuses a float for an
  `int` column: a transform gives such a column its dtype, from `Row.dtypes()`.
- A decimal's `pd.ArrowDtype` cannot be named by a string, so code that declares one imports pyarrow, which the
  contract tests forbid in job code: `basis.core.frames` is the one pure module that does, and the import check
  discounts that one import.
- `pa.Table.from_pandas` records pandas' version in the schema's metadata, which would make an object's hash depend
  on it; `frames.to_arrow` drops the metadata, so the hash depends on pyarrow's version and the writer's settings
  alone, as the run store's design says.
- A settings file read by pydantic-settings rejects any line the settings do not declare, such as `BASIS_LOG_LEVEL`. The
  Python API sets the file's variables for the call alone instead, as the command line's `uv run --env-file` does.
- The generated image's dependencies install in a build stage with git, since `uv sync` fetches an etl-framework taken
  from GitHub with git, which `python:3.14-slim` lacks.
- Oracle has no range locks, so the append guard locks the ledger table for its check, as SQL Server's `HOLDLOCK` locks
  the range.

## Decided against

| Option | Why not |
|---|---|
| A shared core with portfolio-optim | Kept separate by choice. |
| Defaults for write modes, decimal precision, time zones or empty inputs | Each would be wrong for some jobs, silently. |
| A `Custom(sql)` write mode | Invisible to replay and regression. |
| Plain appends without a guard | A rerun would duplicate them; the ledger refuses a second publish unless `--again`. |
| Lineage and asset-triggered scheduling | Jobs do not feed each other. |
| SQL Server `MERGE` | Its history of defects; upserts are an UPDATE then an INSERT of missing keys. |
| Creating or altering tables at run time | Rights are unknown, and a run that changes a schema cannot be replayed. |

## Verified, and not yet

Verified on 2026-11-06: the unit tests, the example's tests, ruff, ty with every rule an error, the import contract,
the integration tier against SQL Server 2022 and Oracle 23 Free (every write mode rerun, the append guard, a failure
mid-transaction, a drifted table, a value that does not fit, a partial publish resumed, typed reads, the row cap, time
zones), the example job published to SQL Server twice, a generated repo with two jobs passing its own checks, the
tutorial run from the GitHub tag, and a generated repo's image built, run on its fixture, and published to SQL Server
from inside the container.

Not yet verified: a run on Windows, Oracle thick mode, publishing to Oracle from the image, Azure Data Lake or another
remote snapshot root, real production databases and their logins' rights, and a job ported from an existing one.
