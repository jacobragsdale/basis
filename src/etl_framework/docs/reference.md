# Reference

The job contract, the commands, the settings and the files of etl-framework 0.1.

## Job module

A job is a module, `jobs/JOB/job.py` in a job repo, listed as an entry point in the `etl_framework.jobs` group of the
repo's `pyproject.toml`. The entry point's name is the job's name in snapshots, logs and the ledger; a module that is
not listed is named by its module path. The module exports:

| Name | Required | What it is |
|---|---|---|
| `Params` | yes | A subclass of `etl.Params`: the job's parameters. |
| `Inputs` | yes | A subclass of `etl.Inputs`: one `Query` or `File` attribute per dataset. |
| `Outputs` | yes | A subclass of `etl.Outputs`: one `Table` attribute per table written. |
| `transform(inputs, params)` | yes | Returns an instance of `Outputs`, with every declared output by name. |
| `check(inputs, outputs, params)` | no | Returns a list of reasons not to write; an empty list writes. |

The module, and everything it imports, must not reach a database driver, an HTTP client, storage, or the run loop's
modules; the contract tests list the import chain if it does. `main.py` and tests are not job code.

## Rows

A row is a subclass of `etl.Row` with one annotated attribute per column, in column order. It is never instantiated.

| Annotation | Frame type | Requires |
|---|---|---|
| `int` | `Int64` | |
| `float` | `Float64` | |
| `Annotated[Decimal, etl.Precision(P, S)]` | `Decimal(P, S)` | `1 <= P <= 38`, `0 <= S <= P` |
| `str`, or `Annotated[str, etl.MaxLength(N)]` | `String` | `N >= 1` |
| `bool` | `Boolean` | |
| `date` | `Date` | |
| `Annotated[datetime, etl.Zone(NAME)]` | `Datetime("us", "UTC")` | an IANA zone name |

`T | None`, or `Annotated[T, ...] | None`, makes a column nullable. A column name is lower-case letters, digits and
underscores, starting with a letter or an underscore. `Zone` names the zone a datetime's naive values are in, in
sources and destinations; in a frame, every datetime is UTC.

Every frame that enters or leaves a job is conformed to its row:

- Its columns must be exactly the row's, matched without regard to case.
- A value converts only without loss: an integer type to `int`, a decimal or integer to a `Decimal` whose cast back is
  unchanged, a datetime at midnight to `date`, `0` and `1` to `bool`, text to any type. A float never converts to an
  `int` or a `Decimal`.
- A naive datetime from a source is read in its column's zone. A naive datetime in a transform's output is refused.
- Non-null columns have no nulls, floats are finite, and strings are at most their `MaxLength`.

## Inputs

### `etl.Query(row, *, database, sql, empty)`

Rows a SQL file selects from a database.

| Parameter | Type | Description |
|---|---|---|
| `row` | `type[etl.Row]` | The rows' columns. |
| `database` | `str` | The database's name, as `ETL_DATABASES__NAME__*` configures it. |
| `sql` | `str` | The SQL file, relative to the file that declares the query. |
| `empty` | `"fail"` or `"allow"` | Whether a run may continue when the query returns no rows. |

A query binds only the run's interval, as naive UTC datetimes: `%(interval_start)s` and `%(interval_end)s` in SQL
Server, `:interval_start` and `:interval_end` in Oracle. A query that names them in a run without an interval fails.
An Oracle `NUMBER` without a precision arrives as a float; cast it in the query for a `Decimal` column.

### `etl.File(row, *, path, empty)`

A `.csv` or `.parquet` file, by a path relative to the declaring file, or a URL that `pyarrow.fs` reads. `{start}` and
`{end}` in `path` format the interval's ends, as `rates/{start:%Y-%m-%d}.csv`. A CSV's values are read as text and
converted by the row; an empty field is null.

### A run on files

A run given a folder of files (`--data DIR`, or `data=` from Python) reads each dataset that has `NAME.csv` or
`NAME.parquet` there instead of its source. A folder with both for one dataset is an error.

### `Inputs` instances

`inputs.NAME` is the dataset's frame, `inputs.frames` maps every name to its frame, and `inputs.interval` is the run's
`etl.Interval`, or `None`.

## Outputs

### `etl.Table(row, *, database, table, write, key=None)`

| Parameter | Type | Description |
|---|---|---|
| `row` | `type[etl.Row]` | The table's columns that the job writes. |
| `database` | `str` | The database's name. |
| `table` | `str` | `TABLE` or `SCHEMA.TABLE`: letters, digits, underscores and `$`, never quoted. |
| `write` | a write mode | How the table is written. |
| `key` | `tuple[str, ...]` or `None` | The columns that identify a row; none of them may be nullable. |

### Write modes

The core writes a table through four operations, applied in one transaction in this order: delete, upsert, insert of
new keys, insert. Each write mode makes them from the frame the transform returns:

| Mode | Operations | Needs `key` | Rerun leaves the table unchanged |
|---|---|---|---|
| `etl.Append()` | insert | no | no: the ledger refuses a second publish of an overlapping interval |
| `etl.Replace()` | delete every row, insert | no | yes |
| `etl.ReplaceWhere(interval=COLUMN, matching=(COLUMN, ...))` | delete rows whose `interval` column is in the run's interval and whose `matching` columns equal a row of the frame's, insert | no | yes |
| `etl.Upsert()` | update rows whose key exists, insert the rest | yes | yes |
| `etl.InsertNew()` | insert rows whose key does not exist | yes | yes |
| `etl.Manual()` | the `Changes` the transform returns | for an upsert or an insert of new keys | when every insert is inside the delete |

`ReplaceWhere` names `interval`, `matching`, or both. Its `interval` column is a `date` or a `datetime`; a date compares
as its midnight UTC. Its `matching` columns may not be nullable.

### `etl.Changes(delete=None, upsert=None, insert_new=None, insert=None)`

What a `Manual()` table's transform returns. `delete` is an `etl.Delete(within=None, matching=None)`: rows in
`within`, an `etl.Range(column, start, end)` with zone-aware ends, and matching a row of `matching`, a frame of some of
the row's columns. `etl.Delete()` deletes every row. The other three are frames of the table's row.

### Checks on every output

A run writes nothing when any of these holds, and lists each in `run.json`'s `reasons`:

- A frame does not conform to its row.
- A key appears more than once across the upsert, the insert of new keys, and the insert.
- An inserted row is outside the delete, so a rerun would insert it twice.
- A delete matches on a null.
- `ReplaceWhere(interval=...)` runs without an interval.
- The destination table lacks a row's column, fills it itself (identity, computed or virtual), cannot hold its type
  exactly, or requires a column the row leaves out.
- A value does not fit its column: a string longer than its length, a number its precision cannot hold, an integer
  beyond its type's range, a null in a `NOT NULL` column, or a fraction of a second that `DATE`, `DATETIME` or
  `SMALLDATETIME` drops.
- The job's `check` returns a reason, or raises.

The destination checks run only where the database is configured.

## Params

A subclass of `etl.Params` is a strict pydantic model: an unknown key is refused, and no value is coerced. A run reads
one JSON file of params. A field's docstring is its description in `etl describe`.

## Interval

`START..END`, half-open. Each end is a date, meaning its midnight UTC, or an ISO datetime with an offset or `Z`, as
`2026-11-05..2026-11-06` or `2026-11-05T06:00Z..2026-11-05T07:00Z`. A job that has no notion of an interval runs
without one.

## Commands

| Command | Does |
|---|---|
| `etl run JOB --params FILE [--interval START..END] [--data DIR] [--label KEY=VALUE]... [--publish] [--again]` | Runs a job, saves its snapshot, and with `--publish` writes its changes. `--again` publishes an `Append()` table for an interval the ledger already has. |
| `etl replay JOB RUN_ID [--params FILE]` | Runs a saved run's transform again on its inputs; prints the replay's status, reasons and differences as JSON. |
| `etl resume JOB RUN_ID` | Writes a partly published run's changes to each database whose ledger lacks the run. |
| `etl regress JOB [--last N] [--label KEY=VALUE]... [--snapshots ROOT]` | Replays the job's last `N` (default 10) published runs whose labels include every `--label`; prints each run's differences as JSON. |
| `etl describe JOB` | Prints the job's params schema, inputs and outputs as JSON. |
| `etl list` | Prints the jobs the installed packages list, by name. |
| `etl ddl JOB --dialect mssql\|oracle` | Prints `CREATE TABLE` for each of the job's tables. |
| `etl ddl --ledger TABLE --dialect mssql\|oracle` | Prints `CREATE TABLE` and `CREATE INDEX` for the ledger. |
| `etl new repo REPO JOB [--dir DIR] [--index URL \| --framework-path PATH]` | Makes a job repo with one job, runs `git init` and `uv sync`. Without either option, etl-framework comes from its GitHub tag. |
| `etl new job NAME` | Adds a job to the repo in the working directory. |
| `etl sync` | Rewrites `AGENTS.md`'s etl-framework block and `.agents/skills/etl-*` from the installed version. |
| `etl docs [PAGE]` | Lists the docs, or prints one. |

`JOB` is an entry point name or a module path. `replay`, `resume` and `regress` find runs under
`ETL_SNAPSHOT_ROOT`.

| Exit code | Meaning |
|---|---|
| `0` | The command did what it says: a run without `--publish` was `checked`, a run with it `published`. |
| `1` | A run failed, was refused, or published partly; a regression found differences; or the command raised. |
| `2` | The command line did not parse. |

## Python API

`etl.run(job, *, params, interval=None, env=".env", data=None, labels=None, snapshot_root=None)` runs a job module
without publishing and returns its result: `result.record` is its `RunRecord`, `result.inputs` its frames, and
`result.changes(NAME)` a table's `Changes`. `etl.replay(job, run_id, *, params=None, env=None, snapshot_root=None)`
returns the replay's result and its differences.

Paths are relative to the job's folder. `env` names a settings file, looked for in the job's folder and each folder
above it up to the repo's root; its variables are set for the call alone, and the process's environment overrides them. Snapshots are saved in the repo's
`snapshots/`. `replay` looks for the run there, then under the settings file's `ETL_SNAPSHOT_ROOT`. Given `data` and
no `interval`, `run` uses the fixture's interval from `fixture.json`.

## Settings

Settings come only from environment variables, which a settings file can hold.

| Variable | Description |
|---|---|
| `ETL_SNAPSHOT_ROOT` | Where `etl run` saves snapshots and the other commands find runs: a path, or a URL `pyarrow.fs` reads and writes. Required by the commands. |
| `ETL_DATABASES__NAME__KIND` | `mssql` or `oracle`. |
| `ETL_DATABASES__NAME__TIMEOUT_S` | Seconds a statement may take; a login waits at most 30. |
| `ETL_DATABASES__NAME__MAX_ROWS` | The most rows a query may return before the run fails. |
| `ETL_DATABASES__NAME__CONNECTION_STRING` | SQL Server: the reading login's connection string. |
| `ETL_DATABASES__NAME__ISOLATION` | SQL Server: `snapshot` or `read_committed`. Oracle: `read_only` or `read_committed`. |
| `ETL_DATABASES__NAME__WRITE__CONNECTION_STRING` | SQL Server: the writing login's connection string. |
| `ETL_DATABASES__NAME__DSN`, `__USER`, `__PASSWORD` | Oracle: the reading login. |
| `ETL_DATABASES__NAME__MODE` | Oracle: `thin` or `thick`. |
| `ETL_DATABASES__NAME__CLIENT_LIB_DIR` | Oracle, thick mode: where Oracle Instant Client is, if not on the library path. |
| `ETL_DATABASES__NAME__WRITE__USER`, `__WRITE__PASSWORD` | Oracle: the writing login. |
| `ETL_DATABASES__NAME__WRITE__LEDGER` | The ledger table in that database. |
| `ETL_LOG_LEVEL` | `DEBUG`, `INFO` (default), `WARNING` or `ERROR`. |
| `ETL_LOG_FORMAT` | `json` (the commands' default) or `text` (the Python API's default). |
| `ETL_GIT_SHA` | The job repo's commit, which `run.json` records; the generated `Dockerfile` sets it. |

`NAME` is the database's name in upper case. A database without a `WRITE` block can be read and never written. A
field a database's block does not have is an error; a variable the framework does not read is ignored.

With `snapshot` or `read_only` isolation, every query a run makes on that database sees one moment. SQL Server's
database must allow snapshot isolation. Oracle's `read_only` fails with ORA-01466 on a table whose definition changed in
the seconds before the run began, which a `TRUNCATE` does.

## Snapshot files

```text
ETL_SNAPSHOT_ROOT/JOB/RUN_ID/
  run.json
  params.json
  inputs/NAME.parquet
  outputs/NAME/upsert.parquet, insert_new.parquet, insert.parquet, delete_matching.parquet
```

A snapshot is written whole, under a hidden name renamed into place, before anything is published; `run.json` is
rewritten after publishing. Run ids are UUIDv7s, so they sort by start time.

`run.json` holds the run's `run_id`, `job`, `module`, `version`, `git_sha`, `started_at`, `finished_at`, `interval`,
`params_path`, `params_sha256`, `labels`, `data`, `replay_of`, `status`, `inputs` (each dataset's source, rows and
read time), `outputs` (each table's database, table, write mode, delete and rows by operation), `reasons`, `error`,
and `publishes` (each database's status, time, counts and error).

| `status` | Meaning |
|---|---|
| `failed` | An error, or at least one reason not to write. |
| `checked` | Every check passed; the run did not publish. |
| `published` | Every database the job writes committed. |
| `partial` | Some databases committed and one failed; `etl resume` finishes it. |

## Ledger

One table per database a job writes, created from `etl ddl --ledger`. Each publish adds one row per table, in the
transaction that wrote the table: `run_id`, `job`, `output`, `table_name`, `interval_start`, `interval_end` (naive
UTC), `rerun_safe`, `deleted`, `upserted`, `inserted_new`, `inserted` (as the database counted), `published_at`
(naive UTC), and `etl_version`. Its key is `(run_id, output)`.

## Testing

`etl_framework.testing` holds:

- `ContractTests`: a test class to subclass with `job = job`. It checks that `params/default.json` validates, that
  the job runs on `tests/fixture` with every check passing, and that `job.py` cannot reach I/O through its imports.
  Set `params` or `fixture` on the subclass for other paths, relative to the job's folder.
- `frame(row, *rows)`: a frame of `row` from partial rows, conformed as a load conforms it; a column a row leaves out
  is null.
- `inputs(Inputs, *, interval=None, **frames)`: an `Inputs` instance; a dataset not given has no rows.
- `check_params`, `check_fixture`, `check_imports`, `check_agent_files`: the contract tests' checks, as functions.

A fixture is a folder with one `NAME.csv` or `NAME.parquet` per dataset and a `fixture.json` that gives its interval,
as `{"interval": "2026-11-05..2026-11-06"}`, or `{"interval": null}`.
