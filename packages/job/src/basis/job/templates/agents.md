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
- Every `Decimal` column names its `Precision`, every `datetime` column its `Zone`; in a frame, datetimes are UTC.
- Every input names `empty="fail"` or `empty="allow"`.
- Every dataset has a file in the job's `tests/fixture/`, and `fixture.json` gives the fixture's interval.

Commands, all through `basis`, whose `--help` lists every verb this repo has. A failure prints one line that starts
with `ERROR`, gives the reason and ends with the fix:

```sh
uv run basis check                                  # lint, format, types, the jobs' tests on fixtures, the agent files
uv run basis new job NAME                           # a new job, copied from the example and listed in pyproject.toml
uv run basis describe JOB                           # params, inputs and outputs as JSON
uv run basis run JOB --params jobs/JOB/params/default.json --data jobs/JOB/tests/fixture
uv run basis replay JOB RUN_ID [--params FILE]      # a saved run again, with the code of the day
uv run basis regress [JOB] --last 10                # recent published runs replayed; exit 1 if anything changed
uv run basis ddl JOB --dialect mssql                # CREATE TABLE for the job's tables
uv run basis docs job/reference                     # the job contract, write modes, settings
```

The README's table maps each kind of change to the file it goes in. The skills in `.agents/skills/job-*` cover making
a job, porting an existing one, and finding out why a run failed.
