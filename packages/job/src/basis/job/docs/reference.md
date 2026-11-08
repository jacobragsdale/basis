# Reference

The job contract, the commands, the settings and the files of basis-job.

## Job module

A job is a module, `jobs/JOB/job.py` in a job repo, listed as an entry point in the `basis.jobs` group of the
repo's `pyproject.toml`. The entry point's name is the job's name in snapshots, logs and the ledger; a module that is
not listed is named by its module path. The module exports:

| Name | Required | What it is |
|---|---|---|
| `Params` | yes | A subclass of `etl.Params`: the job's parameters. |
| `Inputs` | yes | A subclass of `etl.Inputs`: one `Query`, `Fetch` or `File` attribute per dataset, and a `Clock` or `Ids` attribute for each the transform uses. |
| `Outputs` | yes | A subclass of `etl.Outputs`: one `Table` attribute per table written. |
| `transform(inputs, params)` | yes | Returns an instance of `Outputs`, with every declared output by name. |
| `check(inputs, outputs, params)` | no | Returns a list of reasons not to write; an empty list writes. |
| `CALENDAR` | no | The job's business days, as `CALENDAR = etl.calendar("sifma-us")`, which a relative `--interval` resolves in (see [Interval](#interval)). |

The module, and everything it imports, must not reach a database driver, an HTTP client, storage, pyarrow, or the run
loop's modules; the contract tests list the import chain if it does. `main.py` and tests are not job code.

Every frame a job gets and returns is a pandas `DataFrame`, its columns of the types the next section gives. The
transform and the check are each given frames of their own of the inputs, made afresh from the Arrow tables the run
loaded and saves, so a frame either changes in place changes neither what the other sees nor the snapshot.

## Rows

A row is a subclass of `etl.Row` with one annotated attribute per column, in column order. It is never instantiated.

| Annotation | Frame dtype | Requires |
|---|---|---|
| `int` | `pd.ArrowDtype(pa.int64())` | |
| `float` | `pd.ArrowDtype(pa.float64())` | |
| `Annotated[Decimal, etl.Precision(P, S)]` | `pd.ArrowDtype(pa.decimal128(P, S))` | `1 <= P <= 38`, `0 <= S <= P` |
| `str`, or `Annotated[str, etl.MaxLength(N)]` | `pd.ArrowDtype(pa.string())` | `N >= 1` |
| `bool` | `pd.ArrowDtype(pa.bool_())` | |
| `date` | `pd.ArrowDtype(pa.date32())` | |
| `Annotated[datetime, etl.Zone(NAME)]` | `pd.ArrowDtype(pa.timestamp("us", "UTC"))` | an IANA zone name |

Every column is backed by Arrow, so a null is `pd.NA` in every type, an `int` with nulls stays an integer, a
`Decimal` stays exact through arithmetic and sums, and no column is `object`. Job code, which may not import pyarrow,
names a dtype through its row: `Row.dtypes()` gives each column's by name, for `frame.astype(Row.dtypes())`, and
`Row.empty()` a frame with no rows. `T | None`, or
`Annotated[T, ...] | None`, makes a column nullable. A column name is lower-case letters, digits and underscores,
starting with a letter or an underscore. `Zone` names the zone a datetime's naive values are in, in sources and
destinations; in a frame, every datetime is UTC.

Every frame that enters or leaves a job is conformed to its row:

- Its columns must be exactly the row's, matched without regard to case; a column left in the frame's index, as a
  `groupby` without `as_index=False` leaves its keys, is named as such. Its index is never data: a conformed frame's
  only counts its rows.
- A column of any dtype converts, Arrow's, numpy's, a category or `object`'s Python values, but only without loss: an
  integer type to `int`, a decimal or integer to a `Decimal` that keeps every digit, a datetime at midnight to `date`,
  `0` and `1` to `bool`, text to any type. A float never converts to an `int` or a `Decimal`, and numpy's `NaN` is a
  null. Since numpy's integers have no null, pandas makes `[1, None]` floats: give such a column its dtype, as
  `pd.array(values, dtype=Row.dtypes()["count"])`.
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

### `etl.Fetch(row, *, http, path, deadline_s, empty)`

Rows another service answers a GET with: the one way a job reaches another service.

| Parameter | Type | Description |
|---|---|---|
| `row` | `type[etl.Row]` | The rows' columns. |
| `http` | `str` | The service's name, as `BASIS_HTTP__NAME__*` configures it. |
| `path` | `str` | The service's own path, after its base URL. `{start}` and `{end}` give the interval's ends in ISO 8601, as `2026-11-05T00:00:00+00:00`, or in the format the path names, as `prices/{start:%Y-%m-%d}`, each percent-encoded, so a `+` reaches the service as a `+`. |
| `deadline_s` | `float` | Seconds the GET may take, from the first byte sent to the last byte of the reply, the attempts it makes again included. |
| `empty` | `"fail"` or `"allow"` | Whether a run may continue when the service answers no rows. |

The service answers a JSON list of objects, one per row, each of the row's columns by name; a field the object leaves
out is null, and a field the row has no column for, a value that is an object or a list, or `NaN` or `Infinity`, which
are no JSON numbers, fails the run. Each value is read as its text, as a CSV's is, so a number keeps the digits it was
sent with. A GET that fails fails the run, its `error` naming the kind of service error it is: `Timeout` past
`deadline_s` or for a 504; `Unavailable` for a service that cannot be reached or answers 502, 503 or 429, which the GET
makes again, after a backoff, while `deadline_s` allows; `BadReply` for a reply that is not the rows; and `NotFound`,
`Conflict`, `Rejected`, `Denied` or `Failed` for a 404, a 409, a 400 or 422, a 401 or 403, or any other status. The rows
are saved in the snapshot like any input's, so a replay never calls the service.

### `etl.File(row, *, path, empty)`

A `.csv` or `.parquet` file, by a path relative to the declaring file, or a URL that `pyarrow.fs` reads. `{start}` and
`{end}` in `path` format the interval's ends, as `rates/{start:%Y-%m-%d}.csv`. A CSV's values are read as text and
converted by the row; an empty field is null, and so is a blank line in a CSV of one column. A header that names a
column twice is refused.

### `etl.Excel(row, *, folder, path, sheet, empty)`

A sheet of a workbook in a shared folder, such as where portfolio managers keep theirs. `folder` names the folder, as
`BASIS_FOLDERS__FOLDER__PATH` gives it on each machine: `\\server\share\pm` on Windows, its mount path in WSL and in a
pod. `path` is the workbook's inside it, `.xlsx`, `.xlsm`, `.xlsb` or `.xls`, which may format the interval's ends, as
`positions/{start:%Y-%m-%d}.xlsx`, and never leaves the folder. The sheet's first row names its columns, and each cell
is read as a CSV's text is, for the row to type, so nothing is inferred: a whole number as its digits, a date as
`YYYY-MM-DD`, an empty cell null. A naive time is read in its column's `Zone`. A repo that reads or writes workbooks
takes `basis-core[excel]` (python-calamine and xlsxwriter).

### A run on files

A run given a folder of files (`--data DIR`, or `data=` from Python) reads each dataset that has `NAME.csv` or
`NAME.parquet` there instead of its source. A folder with both for one dataset is an error.

### `etl.Clock()` and `etl.Ids()`

The time, and new ids, for a transform that needs them, named by their attributes on `Inputs`, each a resource's name:
lower-case letters, digits and underscores, starting with a letter. One instance names one attribute. `inputs.NAME.now()` is
the time, in UTC; `inputs.NAME.new()` is a new UUIDv7, and `inputs.NAME.new(count)` a list of `count`, for a column's.
A run keeps every value they give in its manifest, and a replay of the run is given the same values again, in the same
order, so it changes nothing a transform makes from them. A replay that asks for more than its run kept is given, past
them, the last time the run kept, or the run's start, and new ids, and logs that it did.

### `Inputs` instances

`inputs.NAME` is the dataset's frame, or a clock's or ids', `inputs.frames` maps every dataset's name to its frame, and
`inputs.interval` is the run's `etl.Interval`, or `None`.

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

### `etl.Slack(channel, *, ledger, deadline_s)` and `etl.Email(to, *, ledger, deadline_s)`

A message the job sends: to a Slack channel, through its webhook, as `BASIS_SLACK__CHANNEL__WEBHOOK` configures it, or
an email to the people `BASIS_EMAIL__TO__TO` lists, through the relay the rest of that block names. The transform
gives a `Slack` output its text, which Slack reads as `mrkdwn`, an `Email` output an `etl.Mail(subject=..., text=...)`,
and either `None` for no message this run; write numbers in it with `basis.core.display.text`, so they read as the UI
shows them.

| Parameter | Type | Description |
|---|---|---|
| `channel`, `to` | `str` | The channel's or the email's name, as its settings name it. |
| `ledger` | `str` | The database whose ledger records each message sent, which the run's settings must let it publish to. |
| `deadline_s` | `float` | Seconds the post or the send may take. |

Only `basis run --publish` sends a message, once every database the run writes has committed; a run without it, a
replay, `regress` and the QA tier send nothing, and each message the run planned is in its manifest, `messages`.
Each send is recorded in its ledger first, with its text's SHA-256 in `detail`, in a transaction that commits only
once the message is sent, so a send that fails records nothing. A run for an interval another run sent the message
for skips it, and logs why, unless it is given `--again`; `basis resume` sends what a run could not, and never what
the ledger has. A send is never made again on failure: the run ends `partial`, its `sent` records the error, and
`basis resume` sends it once it is fixed. In a test, `fakes.Slack()` and `fakes.Email()`, given by the channel's or
the email's name, keep what is sent, in `posted` and `sent`, and fail on demand, as `channel.fails(Unavailable)`. A
job's failure is no message of its own: Airflow and Datadog alert on that.

### `etl.ExcelReport(row, *, folder, path, sheet)`

A workbook the job writes for people to a shared folder, as `etl.Excel` names one: its value is a frame of `row`,
written as the sheet `sheet`, 1 to 31 characters, of the `.xlsx` at `path`, which may format the interval's ends. Only
`basis run --publish` writes it, once every database has committed and before any message is sent, to a temporary file
beside it that is then renamed into place, so a reader of the folder never sees half of it, and a rerun writes it
again, whole. A datetime is written as its wall clock in its column's `Zone`, since Excel has none. A write that fails
leaves the run `partial`, its `written` naming the error, and `basis resume` writes it from the run's snapshot; a run
writes no message while one of its workbooks is unwritten.

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
- A value does not fit its column: a string longer than the column holds, in its own units (UTF-16 units for an
  `nvarchar`, bytes for a SQL Server `varchar`, and characters and bytes for an Oracle `VARCHAR2`); a string holding
  a character its column's code page or character set cannot store (a SQL Server `char`, `varchar` or `text` without
  a UTF-8 collation, or an Oracle column in a set such as WE8MSWIN1252, which would store another in its place); a
  number its precision cannot hold; an integer beyond its type's range; a null in a `NOT NULL` column; or a fraction
  of a second that `DATE`, `DATETIME` or `SMALLDATETIME` drops.
- The job's `check` returns a reason, or raises.

The destination checks run only where the database is configured.

## Params

A subclass of `etl.Params` is a strict pydantic model: an unknown key is refused, and no value is coerced. A run reads
one JSON file of params. A field's docstring is its description in `basis describe`.

## Interval

`START..END`, half-open. Each end is a date, meaning its midnight UTC, or an ISO datetime with an offset or `Z`, as
`2026-11-05..2026-11-06` or `2026-11-05T06:00Z..2026-11-05T07:00Z`. A job that has no notion of an interval runs
without one.

`basis run --interval` also takes an interval relative to the moment the run starts, so a scheduler's command never
changes. It resolves in the zone and business days of the job's `CALENDAR`, which it needs, to dates, and the run's
manifest records those dates, so `replay` and `regress` rerun exactly the interval it ran for. On Tuesday 2026-11-10 in
New York, with `sifma-us`:

| Given | Resolves to |
|---|---|
| `today` | `2026-11-10..2026-11-11` |
| `yesterday` | `2026-11-09..2026-11-10` |
| `previous-business-day` | `2026-11-09..2026-11-10`; on a Monday, the Friday before, or the last business day before it |
| `previous-month` | `2026-10-01..2026-11-01` |
| `today-7d..today` | `2026-11-03..2026-11-10`: each end is `today`, `today-Nd` (N days before), `today-Nbd` (N business days before) or a date |

A business day before today is counted from today, which need not be one: on a Saturday, `today-1bd` is the Friday.
`etl.run` and the QA tier take dates only.

## Calendars

`etl.calendar(NAME)` is a business-day calendar, which a transform or a check calls as it is, since it is data in basis
and reads nothing. A job names its calendar; there is no default.

| Name | Is | Covers | Early closes |
|---|---|---|---|
| `sifma-us` | SIFMA's recommended closes for the US bond market | 1996-01-01 to 2027-12-31 | 2 p.m. New York time, noon on a Good Friday with the employment report |
| `target2` | TARGET2's (now T2's) closing days, for euro settlement | 2000-01-01 to 2099-12-31 | none |

| Call | Gives |
|---|---|
| `cal.is_business_day(day)` | Whether the market is open on `day`, all day or until an early close. |
| `cal.add(day, n)` | The business day `n` business days after `day`, or before it for a negative `n`; `day` need not be one, and `add(day, 0)` is `day`, which must be. |
| `cal.previous(day)` | The last business day before `day`. |
| `cal.early_close(day)` | When a business day closes early, as a `datetime` in `cal.zone`, or `None` for its regular session; a day closed all day is an error. |
| `cal.reason(day)` | Why a day is closed or closes early, as `Thanksgiving Day` or `Weekend`, or `None`. |

`day` is a `date`, never a `datetime`: give a moment's as `moment.astimezone(cal.zone).date()`. A day before a
calendar's `first` or after its `last`, or one a call walks to, fails the run with the fix, and is never a business day
by default: SIFMA publishes each year's recommendations about a year ahead, and a basis release that adds them moves
`sifma-us`'s last day. Saturdays and Sundays are closed in both. The rules and their dated exceptions are basis-ui's,
checked against the same schedules SIFMA published, so a day reads alike in a job, an API and the UI.

## Display text

`basis.core.display.text(value, display=None)` writes a value as people read it, in a message's text, as the UI's grid
writes it: a Python port of parallax's formatter, which follows Excel's display rules, so 2.675 shows as `2.68` and
0.1 + 0.2 as `0.30`. `display` is a display format, the contract's object, as a dict or a `Display`:
`{"kind": "money", "currency": "USD"}` shows -1234.5 as `-$1,234.50`, `{"kind": "pct", "decimals": 2}` shows 0.0525
as `5.25%`, `{"kind": "bps"}` as `525.00 bp`, and `{"kind": "price32", "step": "half"}` shows 99.515625 as `99-16+`;
the kinds are `number`, `money`, `pct`, `bps`, `price` and `price32`. A `display.Format` gives the formatter's other
choices, such as `Format(abbreviate="M")` for `1.3M`. Without one, a float shows 2 decimals, an int none, a date
`YYYY-MM-DD`, an aware datetime its wall clock in New York, and None or NaN nothing. It returns text and never
changes a value, so keep the float for arithmetic.

## Commands

A job repo has one command, `basis`. It offers the verbs of the parts the repo declares in its `pyproject.toml`, as
`[tool.basis] parts = ["job"]`, and `basis --help` lists them; a verb of a part the repo does not declare is refused,
with how to declare it. `basis-job`, which `uvx` runs before a repo exists, makes one. `[tool.basis] apt-packages`
names the Debian packages the repo's image installs beside basis's own, such as `["libpq5"]` or `["libpq5=15.10-0"]`,
which `basis sync` writes into the Dockerfile: it is the one way to change the image, since the Dockerfile is
basis's.

| Command | Does |
|---|---|
| `basis check [--staged]` | Checks that the framework and agent files are what the installed basis writes, byte for byte, with nothing left over that it no longer writes; that the clone runs the hooks in `.githooks/`, pointing it at them if it names no hooks folder; and that `uv.lock` matches `pyproject.toml`; then runs `ruff check`, `ruff format --check`, `ty check` and `pytest`, each through `uv run --locked`. It holds the tests to three gates: the unit tests cover at least `[tool.basis] coverage` percent of the code's lines and branches, as pytest-cov measures them with basis's settings, a repo without the floor failing; no test uses `unittest.mock` or pytest-mock, but in a file with a line `# basis: allow-mocks -- WHY`; and no unit test opens a network socket, which basis's pytest plugin fails, a test marked `integration` excepted. With `--staged`, it first checks each file staged for a commit, as git will commit it: none holds a private key, none is over 500 KB, and each YAML and TOML file parses. Every step runs, and each that fails prints its findings and then its `ERROR` line. Needs no network or database. The commit hook runs `basis check --staged` after ruff fixes and formats the staged Python files. |
| `basis check --full [--settings-from DIR] [--no-qa]` | `basis check`, then `regress` (the last 10 published runs of every job, from `BASIS_SNAPSHOT_ROOT` in the environment, where it is set and reachable), then the QA tier (see [QA tier](#qa-tier)), where `pyproject.toml` declares one and the user has a `qa.env`, then, in WSL and on Linux, mutmut on each function of a module named `domain` that the change touches, which fails nothing. A piece that does not run prints one `SKIPPED:` line saying why; none runs when `basis check` fails. An output `regress` sees change prints and fails nothing; a failure to regress, a QA tier that refuses, or a QA run that fails exits 1. With nothing uncommitted, it records what it found for `basis review`. The push hook runs it on each commit pushed, in a temporary worktree when it is not a clean `HEAD`, with `--settings-from` naming the clone's folder, where a relative `BASIS_SNAPSHOT_ROOT` is; `azure-pipelines.yml`'s check stage runs it too. `--no-qa` leaves the QA tier to the push, as `basis upgrade` does. A failure in one piece, whatever it raises, is its `ERROR` line, and the pieces after it still run and are recorded. |
| `basis review [--commit REF]` | Prints, as Markdown for a pull request's description, what the latest `basis check --full` of `REF` (default `HEAD`) found: the outputs `regress` saw change, the QA tier's runs, the mutants that survived, the basis versions, and the commit. Exits 1 when no check of the commit is recorded in this clone, or when it failed. |
| `basis doctor [--env ENV]` | Checks this desktop, on Windows and in WSL, in a repo or outside one: uv and its version, git, that the clone runs the repo's hooks, the feed (the default index of the user's `uv.toml`, which must be the repo's index where `pyproject.toml` names one), and the settings: the process's, and the user's `qa.env` where the repo declares a QA tier. With `--env`, also where that environment's settings come from, whether each database's read login, and its write login where it has one, answers a query that reads no table, whether its run store is reachable, and, in prod, whether this machine may write. Prints `ok:` for each check passed and one `ERROR` line, ending with its fix, for each problem. |
| `basis upgrade` | With nothing uncommitted, relocks basis's packages within what `pyproject.toml` allows, runs `basis sync` and `basis check --full --no-qa` with the newly locked version, and prints each package's version before and after and each file it changed. The QA tier runs when the upgrade is committed and pushed, so QA publishes it once, with a record for `basis review`. |
| `basis run JOB --env ENV --params FILE [--interval INTERVAL] [--data DIR] [--label KEY=VALUE]... [--publish] [--again]` | Runs a job in an environment (see [Environments](#environments)), saves its snapshot, and with `--publish` writes its changes, which prod refuses unless the deployment allows it. `--again` publishes an `Append()` table for an interval the ledger already has. |
| `basis replay [JOB] RUN_ID --env ENV [--params FILE]` | Runs a saved run's transform again on its inputs; prints the replay's status, reasons and differences as JSON. Without `JOB`, the run is found by its id alone. |
| `basis resume [JOB] RUN_ID --env ENV [--again]` | Writes a partly published run's changes to each database whose ledger lacks the run, told `--again` if the run was, or if given now. Without `JOB`, the run is found by its id alone. |
| `basis regress [JOB] (--env ENV \| --snapshots ROOT) [--last N] [--label KEY=VALUE]...` | Replays the last `N` (default 10) published runs of `JOB`, or of every job the repo lists, whose labels include every `--label`, from the environment's run store or `ROOT`; prints each run's job, reasons and differences as JSON: rows added, removed and changed, by column, and each column added, removed or retyped, the rows comparing by the columns both runs hold alike. |
| `basis describe [JOB]` | Prints the job's params schema, inputs and outputs as JSON; without `JOB`, every job the repo lists, with its module. |
| `basis ddl JOB --dialect mssql\|oracle` | Prints `CREATE TABLE` for each of the job's tables. |
| `basis ddl --ledger TABLE --dialect mssql\|oracle` | Prints `CREATE TABLE` and `CREATE INDEX` for the ledger. |
| `basis new job NAME` | Adds a job to the repo, copied from the example, and lists it in `pyproject.toml`. |
| `basis sync` | Rewrites the framework files from the installed version (the `Dockerfile`, `azure-pipelines.yml`, `.githooks/pre-commit` and `pre-push`, `.gitattributes`), `AGENTS.md`'s basis and basis-job blocks, `CLAUDE.md`, `.agents/skills/job-*` and their copies in `.claude/skills/`, and adds the rules that refuse `--no-verify` to `.claude/settings.json`, keeping its other keys. It removes what basis wrote that no installed part writes now: the block of a part the repo no longer has, a `job-*` skill the version dropped, a file whose first lines say basis manages it, and what basis-job wrote before 1.5 (its `etl-*` skills and `tests/test_agent_files.py`). A file of the repo's own at one of these paths (one without the marker, a `CLAUDE.md` that holds anything but `@AGENTS.md`, or settings with none of basis's rules) is left alone, with an `ERROR` line; `basis sync --adopt` saves it as `NAME.before-basis`, for you to compare and delete, and writes basis's. It points a clone that names no hooks folder at `.githooks/`, as `basis check` and `basis new` do. |
| `basis docs [PAGE]` | Lists the docs, as `job/PAGE`, or prints one; `PAGE` alone does where no other part has a page of that name. |
| `basis-job new repo NAME [JOB] [--dir DIR] [--index URL \| --framework-path PATH]` | Makes a job repo, `NAME`, whose first job, `JOB` (`positions` if not given), is a copy of the example, runs `git init`, points git at its hooks, and runs `uv sync`. `PATH` is a checkout of basis, the folder that holds `packages/`, installed editable. `URL` replaces PyPI for the whole repo, so it must proxy PyPI. Without either option, basis-job comes from git when `uvx --from 'git+https://github.com/jacobragsdale/basis@REF#subdirectory=packages/job'` installed it: at this version's tag when `REF` is that tag, such as `v0.7.0`, and otherwise at the commit `REF` named. A basis-job installed from an index or a `--find-links` folder refuses, since uv does not record which: rerun with `--index`, or install basis-job from git. A repo made from a tag is refused after `uv sync` when its lock takes another commit than the one `basis-job` came from, as a tag moved since would. One installed from a folder or a wheel takes basis's GitHub tag for this version, which needs a release. |

`JOB` is a name the repo lists as an entry point, or a module path. `replay`, `resume` and `regress` find runs under
the environment's `BASIS_SNAPSHOT_ROOT`. `--env` may be left out where `BASIS_ENV` names the environment, as a pod's
deployment does. Every command ends its stderr with a log record, `finished VERB`, with its exit code and how
long it took, and every failure then prints one line: `ERROR:`, the reason, and the fix, the last line. A result a
caller reads, such as `describe`'s, is JSON on stdout.

| Exit code | Meaning |
|---|---|
| `0` | The command did what it says: a run without `--publish` was `checked`, a run with it `published`. |
| `1` | A run failed, was refused, or published partly; a regression found differences; or the command failed. |
| `2` | The command line did not parse. |

## QA tier

`basis check --full` runs each job against the QA databases, published, where the repo declares the tier in
`pyproject.toml` and the user has a `qa.env` (see [Environments](#environments)), which the tier reads alone, never
the process's environment. Everything else is declared; nothing is inferred:

```toml
[tool.basis.qa]
marker = "qa"          # what each QA server or database name, and each QA store, contains
stores = ["abfs://runs@qaaccount.dfs.core.windows.net/pricing-etl"]   # BASIS_SNAPSHOT_ROOT must be one of these
writes = { warehouse = ["daily_positions", "account_activity", "etl.publishes"] }   # by database: ledgers too

[tool.basis.qa.job]
positions = { params = "jobs/positions/params/default.json", interval = "2026-11-05..2026-11-06" }
```

| Key | Meaning |
|---|---|
| `marker` | Two or more letters, digits, `_`, `.` or `-`, which every QA name contains, ignoring case, and no production name does. |
| `stores` | The run stores the tier may save its runs in, as `BASIS_SNAPSHOT_ROOT` writes them; each must contain the marker. |
| `writes` | By database, as the settings name it, the tables the tier may write, as the job names them, ignoring case. Each ledger the settings name must be here. |
| `[tool.basis.qa.job]` | Each job the repo lists, with `params` (relative to the repo), `interval` (`START..END`; leave it out for a job that runs without one) and `again` (`true` to publish an `Append()` table for an interval the ledger has, as a second QA run of one does). |

A database's settings carry the marker when a SQL Server connection string, and its `WRITE` block's, names one server
(`Server`, `Data Source`, `Address`, `Addr` or `Network Address`) or one database (`Database` or `Initial Catalog`)
that contains it, or when an Oracle DSN contains it. The tier refuses to run, and runs nothing, while any database
`qa.env` configures does not carry it, the store is not in `stores`, a ledger is not in `writes`, a job the repo lists is
not declared, a job reads a database `qa.env` does not configure, or a job writes a table `writes` does not list.
The databases a QA run writes refuse any other table inside the transaction, before writing it. Each QA run is
labelled `purpose=qa` and records the commit checked. A login can still reach another database by name inside a
query, which no setting shows: give QA's logins access to QA alone.

## Python API

`etl.run(job, *, params, interval=None, env=None, data=None, labels=None, snapshot_root=None)` runs a job module
without publishing and returns its result: `result.record` is its `RunRecord`, `result.inputs` frames of its inputs,
made afresh on each access from `result.tables`, as the run loaded them, and `result.changes(NAME)` a table's
`Changes`. `etl.replay(job, run_id, *, params=None, env=None, snapshot_root=None)` returns the replay's result and its
differences.

Paths are relative to the job's folder. `env` names the environment whose settings the call reads, `"dev"`, `"qa"`
or `"prod"`, as `--env` does; without it, the call reads no settings at all, so every dataset must come from `data`.
Snapshots are saved in the repo's `snapshots/`. `replay` looks for the run there, then under the environment's
`BASIS_SNAPSHOT_ROOT`. Given `data` and
no `interval`, `run` uses the fixture's interval from `fixture.json`. Given `data`, whatever the interval, its clocks
read the fixture's `now`, which a job with a clock needs, and its ids count up from 1.

## Environments

Every command that reaches the firm's systems, `run`, `replay`, `resume` and `regress`, acts in one environment, `dev`,
`qa` or `prod`, which `--env` names, or else `BASIS_ENV`; there is no default, and a command that names none is
refused, with the fix. `--env` and a `BASIS_ENV` that differ are refused too.

- On a desktop, the settings are the user's file for the environment, read alone: `%APPDATA%\basis\ENV.env` on
  Windows, and `$XDG_CONFIG_HOME/basis/ENV.env`, or `~/.config/basis/ENV.env`, in WSL and on Linux. Every repo the user
  works in shares it; the repo's `.env.example` lists the names it needs.
- In a pod, which has no such file, the settings are the process's environment, which its deployment sets, `BASIS_ENV`
  among it.

An environment with no file, whose process sets no `BASIS_` setting, is refused, naming the file to write. In prod, a
run that publishes, `basis run --publish` or `basis resume`, is refused unless the process's environment sets
`BASIS_PROD_WRITES=1`, which a deployment that writes sets, and a user's file cannot. That stops accidents, not
malice: the real control is a read-only prod login for desktops. Desktops write in dev and QA.

## Settings

Settings are environment variables, which an environment's file holds, as `NAME=VALUE` lines.

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
| `BASIS_HTTP__NAME__URL` | The base URL of the service a fetch names `NAME`. |
| `BASIS_HTTP__NAME__CA_BUNDLE` | A PEM file of the certificate authorities the service's certificate chains to, in place of the system's. |
| `BASIS_HTTP__NAME__PROXY` | The proxy the service's calls go through; without it, they go direct. `HTTP_PROXY` and `SSL_CERT_FILE` are never read. |
| `BASIS_FOLDERS__NAME__PATH` | The absolute path on this machine of the shared folder an `etl.Excel` or `etl.ExcelReport` names `NAME`: `\\server\share\pm` on Windows, `/mnt/pm` in WSL and in a pod. |
| `BASIS_SLACK__NAME__WEBHOOK` | The incoming-webhook URL of the Slack channel a message names `NAME`, a secret. `__CA_BUNDLE` and `__PROXY` as for a service. |
| `BASIS_EMAIL__NAME__HOST`, `__PORT`, `__SECURITY`, `__SENDER`, `__TO` | The relay an email named `NAME` goes through, `none`, `starttls` or `tls`, its sender's address, and, as JSON, the addresses it goes to, `["desk@example.com"]`: each environment's own. `__USER` and `__PASSWORD` where the relay asks for a login. |
| `BASIS_TRACES__EXPORTER` | `otlp`, to post each run's spans to `BASIS_TRACES__ENDPOINT`, or `console`, to write them to stderr. Without it, spans go nowhere. |
| `BASIS_TRACES__ENDPOINT` | For `otlp`: the full URL spans are posted to, path and all, such as `http://$(HOST_IP):4318/v1/traces`, the Datadog Agent on the pod's node. |
| `BASIS_TRACES__TIMEOUT_S` | Seconds each export may take, its retries included (default 5). An export that fails is logged and fails nothing. |
| `BASIS_ENV` | The environment a command acts in where `--env` does not name one: `dev`, `qa` or `prod`. Never read from a file. |
| `BASIS_PROD_WRITES` | `1` lets a prod run publish. Never read from a file. |
| `BASIS_LOG_LEVEL` | `DEBUG`, `INFO` (default), `WARNING` or `ERROR`. |
| `BASIS_LOG_FORMAT` | `json` (the commands' default) or `text` (the Python API's default). |
| `BASIS_GIT_SHA` | The job repo's commit, which the run's manifest records, and its spans carry as their version; the generated `Dockerfile` sets it. |

No `OTEL_*` variable changes where spans go. Every run, replay and resume is one trace, with a span for each SQL
statement and GET, and every JSON record it logs carries `trace_id`, `span_id` and the job's name as `service`. Where
the deployment sets `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES`, as berth does, the spans and the `service` field
carry the deployment's names in place of the job's, so Datadog shows its logs, traces and metrics as one service; an
environment there that is not the run's fails the run.

`NAME` is the database's or the service's name in upper case. A database without a `WRITE` block can be read and never written. A
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
status, time, counts and error), `clocks` and `ids` (each value the run's clocks and ids gave, by name, in order),
`params` (the params file's text, as the run read it), and `datasets`: the hash of
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
(naive UTC), `etl_version`, and `detail`, a JSON object or null. A message sent adds a row too, its `table_name` the
destination, as `slack:desk` or `email:risk`, `inserted` 1, and `detail` its text's `sha256`. Its key is
`(run_id, output)`. A ledger made before `detail` existed takes it with `ALTER TABLE etl.publishes ADD detail
NVARCHAR(MAX) NULL` on SQL Server, or `ADD (detail CLOB)` on Oracle.

## Testing

`basis.job.testing` holds:

- `ContractTests`: a test class to subclass with `job = job`. It checks that `params/default.json` validates, that
  the job runs on `tests/fixture` with every check passing, and that `job.py` cannot reach I/O through its imports.
  Set `params` or `fixture` on the subclass for other paths, relative to the job's folder.
- `frame(row, *rows)`: a frame of `row` from partial rows of Python values, conformed as a load conforms it; a column a
  row leaves out is null, and a `Decimal` column's `int` or `str` is exact.
- `inputs(Inputs, *, interval=None, now=None, **frames)`: an `Inputs` instance; a dataset not given has no rows. Each
  clock reads `now`, which an `Inputs` with a clock needs, and each ids counts up from
  `00000000-0000-7000-8000-000000000001`.
- `check_params`, `check_fixture`, `check_imports`: the contract tests' checks, as functions. Whether the agent files match the installed version is `basis check`'s to say.

A fixture is a folder with one `NAME.csv` or `NAME.parquet` per dataset, a fetch's among them, and a `fixture.json` that
gives its interval, as `{"interval": "2026-11-05..2026-11-06"}`, or `{"interval": null}`, and, for a job with a clock,
the time it reads, as `"now": "2026-11-06T06:00:00+00:00"`; its ids count up from 1.
