# Reference

The job contract, the commands, the settings and the files of basis-job.

## Job module

A job is a module, `jobs/JOB/job.py` in a job repo, listed as an entry point in the `basis.jobs` group of the
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
| `database` | `str` | The database's name, as `BASIS_DATABASES__NAME__*` configures it. |
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

A run writes nothing when any of these holds, and lists each in its manifest's `reasons`:

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
one JSON file of params. A field's docstring is its description in `basis describe`.

## Interval

`START..END`, half-open. Each end is a date, meaning its midnight UTC, or an ISO datetime with an offset or `Z`, as
`2026-11-05..2026-11-06` or `2026-11-05T06:00Z..2026-11-05T07:00Z`. A job that has no notion of an interval runs
without one.

## Commands

A job repo has one command, `basis`. It offers the verbs of the parts the repo declares in its `pyproject.toml`, as
`[tool.basis] parts = ["job"]`, and `basis --help` lists them; a verb of a part the repo does not declare is refused,
with how to declare it. `basis-job`, which `uvx` runs before a repo exists, makes one. `[tool.basis] apt-packages`
names the Debian packages the repo's image installs beside basis's own, such as `["libpq5"]` or `["libpq5=15.10-0"]`,
which `basis sync` writes into the Dockerfile: it is the one way to change the image, since the Dockerfile is
basis's.

| Command | Does |
|---|---|
| `basis check [--staged]` | Checks that the framework and agent files are what the installed basis writes, byte for byte, with nothing left over that it no longer writes; that the clone runs the hooks in `.githooks/`, pointing it at them if it names no hooks folder; and that `uv.lock` matches `pyproject.toml`; then runs `ruff check`, `ruff format --check`, `ty check` and `pytest`, each through `uv run --locked`. With `--staged`, it first checks each file staged for a commit, as git will commit it: none holds a private key, none is over 500 KB, and each YAML and TOML file parses. Every step runs, and each that fails prints its findings and then its `ERROR` line. Needs no network or database. The commit hook runs `basis check --staged` after ruff fixes and formats the staged Python files. |
| `basis check --full [--settings-from DIR] [--no-qa]` | `basis check`, then `regress` (the last 10 published runs of every job, from `BASIS_SNAPSHOT_ROOT` in the environment, where it is set and reachable), then the QA tier (see [QA tier](#qa-tier)), where `pyproject.toml` declares one and this machine has its settings file. A piece that does not run prints one `SKIPPED:` line saying why; neither runs when `basis check` fails. An output `regress` sees change prints and fails nothing; a failure to regress, a QA tier that refuses, or a QA run that fails exits 1. With nothing uncommitted, it records what it found for `basis review`. The push hook runs it on each commit pushed, in a temporary worktree when it is not a clean `HEAD`, with `--settings-from` naming the clone's folder, where the files git does not track are; `azure-pipelines.yml`'s check stage runs it too. `--no-qa` leaves the QA tier to the push, as `basis upgrade` does. A failure in one piece, whatever it raises, is its `ERROR` line, and the pieces after it still run and are recorded. |
| `basis review [--commit REF]` | Prints, as Markdown for a pull request's description, what the latest `basis check --full` of `REF` (default `HEAD`) found: the outputs `regress` saw change, the QA tier's runs, the basis versions, and the commit. Exits 1 when no check of the commit is recorded in this clone, or when it failed. |
| `basis doctor` | Checks this desktop, on Windows and in WSL, in a repo or outside one: uv and its version, git, that the clone runs the repo's hooks, the feed (the default index of the user's `uv.toml`, which must be the repo's index where `pyproject.toml` names one), and the settings: the environment's, and the QA tier's file. Prints `ok:` for each check passed and one `ERROR` line, ending with its fix, for each problem. |
| `basis upgrade` | With nothing uncommitted, relocks basis's packages within what `pyproject.toml` allows, runs `basis sync` and `basis check --full --no-qa` with the newly locked version, and prints each package's version before and after and each file it changed. The QA tier runs when the upgrade is committed and pushed, so QA publishes it once, with a record for `basis review`. |
| `basis run JOB --params FILE [--interval START..END] [--data DIR] [--label KEY=VALUE]... [--publish] [--again]` | Runs a job, saves its snapshot, and with `--publish` writes its changes. `--again` publishes an `Append()` table for an interval the ledger already has. |
| `basis replay [JOB] RUN_ID [--params FILE]` | Runs a saved run's transform again on its inputs; prints the replay's status, reasons and differences as JSON. Without `JOB`, the run is found by its id alone. |
| `basis resume [JOB] RUN_ID [--again]` | Writes a partly published run's changes to each database whose ledger lacks the run, told `--again` if the run was, or if given now. Without `JOB`, the run is found by its id alone. |
| `basis regress [JOB] [--last N] [--label KEY=VALUE]... [--snapshots ROOT]` | Replays the last `N` (default 10) published runs of `JOB`, or of every job the repo lists, whose labels include every `--label`; prints each run's job, reasons and differences as JSON. |
| `basis describe [JOB]` | Prints the job's params schema, inputs and outputs as JSON; without `JOB`, every job the repo lists, with its module. |
| `basis ddl JOB --dialect mssql\|oracle` | Prints `CREATE TABLE` for each of the job's tables. |
| `basis ddl --ledger TABLE --dialect mssql\|oracle` | Prints `CREATE TABLE` and `CREATE INDEX` for the ledger. |
| `basis new job NAME` | Adds a job to the repo, copied from the example, and lists it in `pyproject.toml`. |
| `basis sync` | Rewrites the framework files from the installed version (the `Dockerfile`, `azure-pipelines.yml`, `.githooks/pre-commit` and `pre-push`, `.gitattributes`), `AGENTS.md`'s basis and basis-job blocks, `CLAUDE.md`, `.agents/skills/job-*` and their copies in `.claude/skills/`, and adds the rules that refuse `--no-verify` to `.claude/settings.json`, keeping its other keys. It removes what basis wrote that no installed part writes now: the block of a part the repo no longer has, a `job-*` skill the version dropped, a file whose first lines say basis manages it, and what basis-job wrote before 1.5 (its `etl-*` skills and `tests/test_agent_files.py`). A file of the repo's own at one of these paths (one without the marker, a `CLAUDE.md` that holds anything but `@AGENTS.md`, or settings with none of basis's rules) is left alone, with an `ERROR` line; `basis sync --adopt` saves it as `NAME.before-basis`, for you to compare and delete, and writes basis's. It points a clone that names no hooks folder at `.githooks/`, as `basis check` and `basis new` do. |
| `basis docs [PAGE]` | Lists the docs, as `job/PAGE`, or prints one; `PAGE` alone does where no other part has a page of that name. |
| `basis-job new repo NAME [JOB] [--dir DIR] [--index URL \| --framework-path PATH]` | Makes a job repo, `NAME`, whose first job, `JOB` (`positions` if not given), is a copy of the example, runs `git init`, points git at its hooks, and runs `uv sync`. `PATH` is a checkout of basis, the folder that holds `packages/`, installed editable. `URL` replaces PyPI for the whole repo, so it must proxy PyPI. Without either option, basis-job comes from the commit `basis-job` was installed from, when `uvx --from 'git+https://github.com/jacobragsdale/basis@REF#subdirectory=packages/job'` installed it, and otherwise from basis's GitHub tag for this version, which needs a release. |

`JOB` is a name the repo lists as an entry point, or a module path. `replay`, `resume` and `regress` find runs under
`BASIS_SNAPSHOT_ROOT`. Every command ends its stderr with a log record, `finished VERB`, with its exit code and how
long it took, and every failure then prints one line: `ERROR:`, the reason, and the fix, the last line. A result a
caller reads, such as `describe`'s, is JSON on stdout.

| Exit code | Meaning |
|---|---|
| `0` | The command did what it says: a run without `--publish` was `checked`, a run with it `published`. |
| `1` | A run failed, was refused, or published partly; a regression found differences; or the command failed. |
| `2` | The command line did not parse. |

## QA tier

`basis check --full` runs each job against the QA databases, published, where the repo declares the tier in
`pyproject.toml` and this machine has its settings file. Everything is declared; nothing is inferred:

```toml
[tool.basis.qa]
settings = ".env.qa"   # the settings file the tier reads, alone: never the process's environment
marker = "qa"          # what each QA server or database name, and each QA store, contains
stores = ["abfs://runs@qaaccount.dfs.core.windows.net/pricing-etl"]   # BASIS_SNAPSHOT_ROOT must be one of these
writes = { warehouse = ["daily_positions", "account_activity", "etl.publishes"] }   # by database: ledgers too

[tool.basis.qa.job]
positions = { params = "jobs/positions/params/default.json", interval = "2026-11-05..2026-11-06" }
```

| Key | Meaning |
|---|---|
| `settings` | The settings file, relative to the repo, which the tier reads and nothing else. |
| `marker` | Two or more letters, digits, `_`, `.` or `-`, which every QA name contains, ignoring case, and no production name does. |
| `stores` | The run stores the tier may save its runs in, as `BASIS_SNAPSHOT_ROOT` writes them; each must contain the marker. |
| `writes` | By database, as the settings name it, the tables the tier may write, as the job names them, ignoring case. Each ledger the settings name must be here. |
| `[tool.basis.qa.job]` | Each job the repo lists, with `params` (relative to the repo), `interval` (`START..END`; leave it out for a job that runs without one) and `again` (`true` to publish an `Append()` table for an interval the ledger has, as a second QA run of one does). |

A database's settings carry the marker when a SQL Server connection string, and its `WRITE` block's, names one server
(`Server`, `Data Source`, `Address`, `Addr` or `Network Address`) or one database (`Database` or `Initial Catalog`)
that contains it, or when an Oracle DSN contains it. The tier refuses to run, and runs nothing, while any database the
file configures does not carry it, the store is not in `stores`, a ledger is not in `writes`, a job the repo lists is
not declared, a job reads a database the file does not configure, or a job writes a table `writes` does not list.
The databases a QA run writes refuse any other table inside the transaction, before writing it. Each QA run is
labelled `purpose=qa` and records the commit checked. A login can still reach another database by name inside a
query, which no setting shows: give QA's logins access to QA alone.

## Python API

`etl.run(job, *, params, interval=None, env=".env", data=None, labels=None, snapshot_root=None)` runs a job module
without publishing and returns its result: `result.record` is its `RunRecord`, `result.inputs` its frames, and
`result.changes(NAME)` a table's `Changes`. `etl.replay(job, run_id, *, params=None, env=None, snapshot_root=None)`
returns the replay's result and its differences.

Paths are relative to the job's folder. `env` names a settings file, looked for in the job's folder and each folder
above it up to the repo's root; its variables are set for the call alone, and the process's environment overrides them. Snapshots are saved in the repo's
`snapshots/`. `replay` looks for the run there, then under the settings file's `BASIS_SNAPSHOT_ROOT`. Given `data` and
no `interval`, `run` uses the fixture's interval from `fixture.json`.

## Settings

Settings come only from environment variables, which a settings file can hold.

| Variable | Description |
|---|---|
| `BASIS_SNAPSHOT_ROOT` | Where `basis run` saves snapshots and the other commands find runs: a path, or a URL `pyarrow.fs` reads and writes. Required by the commands. |
| `BASIS_DATABASES__NAME__KIND` | `mssql` or `oracle`. |
| `BASIS_DATABASES__NAME__TIMEOUT_S` | Seconds a statement may take; a login waits at most 30. |
| `BASIS_DATABASES__NAME__MAX_ROWS` | The most rows a query may return before the run fails. |
| `BASIS_DATABASES__NAME__CONNECTION_STRING` | SQL Server: the reading login's connection string. |
| `BASIS_DATABASES__NAME__ISOLATION` | SQL Server: `snapshot` or `read_committed`. Oracle: `read_only` or `read_committed`. |
| `BASIS_DATABASES__NAME__WRITE__CONNECTION_STRING` | SQL Server: the writing login's connection string. |
| `BASIS_DATABASES__NAME__DSN`, `__USER`, `__PASSWORD` | Oracle: the reading login. |
| `BASIS_DATABASES__NAME__MODE` | Oracle: `thin` or `thick`. |
| `BASIS_DATABASES__NAME__CLIENT_LIB_DIR` | Oracle, thick mode: where Oracle Instant Client is, if not on the library path. |
| `BASIS_DATABASES__NAME__WRITE__USER`, `__WRITE__PASSWORD` | Oracle: the writing login. |
| `BASIS_DATABASES__NAME__WRITE__LEDGER` | The ledger table in that database. |
| `BASIS_LOG_LEVEL` | `DEBUG`, `INFO` (default), `WARNING` or `ERROR`. |
| `BASIS_LOG_FORMAT` | `json` (the commands' default) or `text` (the Python API's default). |
| `BASIS_GIT_SHA` | The job repo's commit, which the run's manifest records; the generated `Dockerfile` sets it. |

`NAME` is the database's name in upper case. A database without a `WRITE` block can be read and never written. A
field a database's block does not have is an error; a variable the framework does not read is ignored.

With `snapshot` or `read_only` isolation, every query a run makes on that database sees one moment. SQL Server's
database must allow snapshot isolation. Oracle's `read_only` fails with ORA-01466 on a table whose definition changed in
the seconds before the run began, which a `TRUNCATE` does.

## Snapshot files

Snapshots are kept in a run store, at `BASIS_SNAPSHOT_ROOT`, or in the repo's `snapshots/` for a run from Python:

```text
objects/HASH.parquet         each dataset, once, named by the SHA-256 of the file
runs/job/JOB/RUN_ID.json     each run's manifest
```

A dataset that several runs save, such as a day's input loaded by each of that day's runs, or the inputs a replay
reruns, is stored once. A run's objects are written before its manifest, and the manifest under a hidden name renamed
into place, all before anything is published; the manifest is rewritten after publishing. Run ids are UUIDv7s, so
they sort by start time.

The manifest holds the run's `run_id`, `job`, `module`, `version`, `git_sha`, `started_at`, `finished_at`,
`interval`, `params_path` (the params file, or `run RUN_ID` for a replay with that run's params), `params_sha256`,
`labels`, `data`, `replay_of`, `status`, `inputs` (each dataset's source, rows and read time), `outputs` (each
table's database, table, write mode, delete and rows by operation), `reasons`, `error`, `publishes` (each database's
status, time, counts and error), `params` (the params file's text, as the run read it), and `datasets`: the hash of
each input, as `inputs/NAME`, and of each table's frames, as `outputs/NAME/upsert`, `insert_new`, `insert` and
`delete_matching`. The params and datasets never change after the run saves them.

| `status` | Meaning |
|---|---|
| `failed` | An error, or at least one reason not to write. |
| `checked` | Every check passed; the run did not publish. |
| `published` | Every database the job writes committed. |
| `partial` | Some databases committed and one failed; `basis resume` finishes it. |

## Ledger

One table per database a job writes, created from `basis ddl --ledger`. Each publish adds one row per table, in the
transaction that wrote the table: `run_id`, `job`, `output`, `table_name`, `interval_start`, `interval_end` (naive
UTC), `rerun_safe`, `deleted`, `upserted`, `inserted_new`, `inserted` (as the database counted), `published_at`
(naive UTC), and `etl_version`. Its key is `(run_id, output)`.

## Testing

`basis.job.testing` holds:

- `ContractTests`: a test class to subclass with `job = job`. It checks that `params/default.json` validates, that
  the job runs on `tests/fixture` with every check passing, and that `job.py` cannot reach I/O through its imports.
  Set `params` or `fixture` on the subclass for other paths, relative to the job's folder.
- `frame(row, *rows)`: a frame of `row` from partial rows, conformed as a load conforms it; a column a row leaves out
  is null.
- `inputs(Inputs, *, interval=None, **frames)`: an `Inputs` instance; a dataset not given has no rows.
- `check_params`, `check_fixture`, `check_imports`: the contract tests' checks, as functions. Whether the agent files match the installed version is `basis check`'s to say.

A fixture is a folder with one `NAME.csv` or `NAME.parquet` per dataset and a `fixture.json` that gives its interval,
as `{"interval": "2026-11-05..2026-11-06"}`, or `{"interval": null}`.
