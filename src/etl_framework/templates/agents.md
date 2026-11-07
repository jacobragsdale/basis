## etl-framework

This repo's jobs run on etl-framework {version}. A job is a folder in `jobs/`, whose `job.py` exports `Params`,
`Inputs`, `Outputs` and `transform(inputs, params) -> Outputs`, and may export `check(inputs, outputs, params) ->
list[str]`. The framework loads the inputs, runs the transform, checks every output, saves a snapshot, and only then,
with `etl run --publish`, writes each database in one transaction.

Rules to keep:

- Job code does no I/O: `job.py`, `rows.py` and what they import never reach a database, a file or the network. The
  contract tests fail on any import chain that could. Data arrives in `Inputs`; results leave in `Outputs`.
- A query binds only the run's interval (`%(interval_start)s`/`%(interval_end)s` in SQL Server,
  `:interval_start`/`:interval_end` in Oracle). Params
  never change what is read, so a replay with other params reads the same snapshot.
- Every output names its write mode: `Append`, `Replace`, `ReplaceWhere`, `Upsert`, `InsertNew`, or `Manual` with
  `Changes`. Nothing about a table is assumed.
- Every `Decimal` column names its `Precision`, every `datetime` column its `Zone`; in a frame, datetimes are UTC.
- Every input names `empty="fail"` or `empty="allow"`.
- Every dataset has a file in the job's `tests/fixture/`, and `fixture.json` gives the fixture's interval.

Commands:

```sh
uv run pytest                                     # contract tests and the jobs' own, on fixtures
uv run etl describe JOB                           # params, inputs and outputs as JSON
uv run etl run JOB --params jobs/JOB/params/default.json --data jobs/JOB/tests/fixture
uv run etl replay JOB RUN_ID [--params FILE]      # a saved run again, with the code of the day
uv run etl regress JOB --last 10                  # recent published runs replayed; exit 1 if anything changed
uv run etl ddl JOB --dialect mssql                # CREATE TABLE for the job's tables
uv run etl docs reference                         # the job contract, write modes, settings
```

The README's table maps each kind of change to the file it goes in. The skills in `.agents/skills/etl-*` cover making
a job, porting an existing one, and finding out why a run failed.
