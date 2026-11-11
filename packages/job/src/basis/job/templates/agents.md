## basis-job

This repo's jobs run on basis-job {version}. A job is a folder in `jobs/`, whose `job.py` exports `Params`,
`Inputs`, `Outputs` and `transform(inputs, params) -> Outputs`, and may export `check(inputs, outputs, params) ->
list[str]`. The framework loads the inputs, runs the transform, checks every output, saves a snapshot, and only then,
with `basis run --publish`, writes each database in one transaction.

Rules to keep:

- Job code does no I/O: `job.py`, `rows.py` and what they import never reach a database, a file or the network. The
  contract tests fail on any import chain that could. Data arrives in `Inputs`; results leave in `Outputs`.
- A query binds only the run's interval (`%(interval_start)s`/`%(interval_end)s` in SQL Server,
  `:interval_start`/`:interval_end` in Oracle). Params
  never change what is read, so a replay with other params reads the same snapshot.
- Every output names its write mode: `Append`, `Replace`, `ReplaceWhere`, `Upsert`, `InsertNew`, or `Manual` with
  `Changes`. Nothing about a table is assumed.
- A column is a `float`, `str`, `bool`, `date` or `datetime`. Every number is a `float`, an amount or a count
  alike; an identifier, such as an account number, is a `str`, which the query selects as text. Every `datetime`
  column names its `Zone`; in a frame, datetimes are UTC.
- Frames are pandas: a number is numpy's float64, NaN where missing, and every other column Arrow-backed, of its
  row's type. Job code names a dtype through its row, as `Row.dtypes()`, never through pyarrow.
- Every input names `empty="fail"` or `empty="allow"`.
- Every dataset has a file in the job's `tests/fixture/`, and `fixture.json` gives the fixture's interval.

Commands, all through `basis`, whose `--help` lists every verb this repo has. A failure prints one line that starts
with `ERROR`, gives the reason and ends with the fix:

```sh
uv run basis check                                  # lint, format, types, the jobs' tests on fixtures, the agent files
uv run basis new job NAME                           # a new job, copied from the example and listed in pyproject.toml
uv run basis describe JOB                           # params, inputs and outputs as JSON
uv run basis run JOB --params jobs/JOB/params/default.json --data jobs/JOB/tests/fixture   # the run's summary as JSON
uv run basis replay JOB RUN_ID                      # a saved run again, with the code of the day; --rows 5 shows each change
uv run basis regress JOB --last 10                  # recent runs replayed; exit 1 if anything changed
uv run basis runs JOB --env prod --last 10          # the store's recent runs: status, interval, error, trace id
uv run basis ddl JOB --dialect mssql                # CREATE TABLE for the job's tables
uv run basis docs job/reference                     # the job contract, write modes, settings
```

A run whose every dataset is a file in `--data`, as a fixture's are, reaches nothing of the firm's, so it needs no
`--env`: it takes `fixture.json`'s interval and time and saves to the repo's `snapshots/`, and `replay` and `regress`
of it read that store, as they do whenever no environment is named. Every other run, replay or regress names one.

The README's table maps each kind of change to the file it goes in. The skills in `.agents/skills/job-*` cover making
a job, porting an existing one, and finding out why a run failed.
