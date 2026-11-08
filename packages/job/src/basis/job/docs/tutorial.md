# Run, replay and check your first job

In this tutorial, we will make a job repo, run its job on the fixture it ships with, read the snapshot the run saves,
replay that run with other params, and watch a check stop a run. Nothing touches a database.

Before you start, you need [uv](https://docs.astral.sh/uv/) 0.13 or later, git, and access to github.com.

## Make a job repo

basis has no release yet, so a job repo takes basis-job from a checkout of basis. Clone it, and make a repo named
`pricing-etl` whose first job is `prices`, beside the clone:

```sh
git clone https://github.com/jacobragsdale/basis
uv run --project basis basis-job new repo pricing-etl prices --framework-path basis
cd pricing-etl
```

The last log line should be:

```text
{"time":"...","level":"info","logger":"basis.job.cli","message":"made pricing-etl: run `uv run basis check` in it"}
```

From here on, every command is `basis`, run with `uv run` in the repo. `uv run basis --help` lists its verbs: those
every basis repo has, such as `check`, and those of the job part, such as `run`, which the repo declares in its
`pyproject.toml` as `[tool.basis] parts = ["job"]`.

The repo's job is `jobs/prices/`: `job.py` declares what it reads and writes and holds the transform, `rows.py` holds
the tables' columns, `sql/` holds the queries it runs on its source database, and `tests/fixture/` holds a few rows
of each input. In this tutorial the fixture replaces the queries, so no database is needed.

## Check the repo

```sh
uv run basis check
```

It runs the lint, format and type checks, which print nothing when they pass, and then the tests. The output should
end with this, with your own time:

```text
9 passed in 0.27s
```

Notice that three of those tests are the job's contract tests, in `jobs/prices/tests/test_contract.py`: its default
params validate, it runs on its fixture with every check passing, and its code cannot reach a database through its
imports. You wrote none of them.

## Run the job on its fixture

Write your settings for dev, which say where snapshots go, in your own `dev.env`, which every repo you work in shares
(on Windows, `%APPDATA%\basis\dev.env`). Then run the job on the fixture for one day, in dev. An interval includes
its start and stops before its end, so `2026-11-05..2026-11-06` is the 5th alone:

```sh
mkdir -p ~/.config/basis && echo BASIS_SNAPSHOT_ROOT=snapshots >> ~/.config/basis/dev.env
uv run basis run prices --env dev --params jobs/prices/params/default.json --data jobs/prices/tests/fixture --interval 2026-11-05..2026-11-06
```

The log is JSON, one record per line. The last record is the run's summary:

```text
{"time":"...","level":"info","logger":"basis.job.runs","message":"run summary","run_id":"01a1b3f7-9b1f-724c-96ad-0d10e4f6ef1d","job":"prices","status":"checked","interval":"2026-11-05..2026-11-06","labels":{},"inputs":{"trades":5,"accounts":3},"outputs":{"positions":{"insert":3},"activity":{"upsert":2}},"reasons":0,"error":null,"publishes":{},"run_ms":63.7}
```

Notice `"status":"checked"`: every check passed, and since we did not pass `--publish`, nothing was written anywhere
but the snapshot. Your run id differs.

## Read the snapshot

The run saved everything it read and everything it would write in the run store, `snapshots/`: each dataset as an
object named by the hash of its content, and the run's manifest, which names them:

```sh
ls snapshots/objects snapshots/runs/job/prices
```

```text
snapshots/objects:
3aca7f74903992ba4609db9e6877fbc5fd3d1b40a9d138562160695e964aa2ac.parquet
bd659bc4bed2437efd39b26a58e3291c8b17c8fa2d519e2a6f091088dd4df4bf.parquet
c45da99ad8dd5389042039d0f8fd67c77e081af2144402a84f60f7c3a37260c7.parquet
cb9e8905675ad07f48948ebfb4fd594de12cce71161c3582f99dd93edfde085c.parquet

snapshots/runs/job/prices:
01a1b3f7-9b1f-724c-96ad-0d10e4f6ef1d.json
```

Your hashes may differ. Open the manifest. Its `datasets` name the four objects: the two inputs, `inputs/trades` and
`inputs/accounts`, and the frames of the two tables, `outputs/positions/insert` and `outputs/activity/upsert`. Its
`outputs.positions` entry says the run would delete the interval's trade dates from the
`daily_positions` table of the database the job calls `warehouse`, and insert three rows, because the job writes
positions with `ReplaceWhere(interval="trade_date")`:

```json
"positions": {
  "database": "warehouse",
  "table": "daily_positions",
  "write": "ReplaceWhere",
  "delete": {"column": "trade_date", "start": "2026-11-05T00:00:00Z", "end": "2026-11-06T00:00:00Z", "matching": false},
  "rows": {"insert": 3}
}
```

## Replay the run with other params

The job leaves out closed accounts unless its params say otherwise. Make a params file that includes them; a param the
file leaves out keeps its default:

```sh
echo '{"include_closed_accounts": true}' > jobs/prices/params/closed.json
```

Replay the saved run with it. RUN_ID is the manifest's name under `snapshots/runs/job/prices/`, without `.json`:

```sh
uv run basis replay prices RUN_ID --env dev --params jobs/prices/params/closed.json 2>/dev/null
```

The replay prints what changed:

```json
{
  "run_id": "01a1b3f7-b5f1-73ea-9c34-f0ac64e2df39",
  "replay_of": "01a1b3f7-9b1f-724c-96ad-0d10e4f6ef1d",
  "status": "checked",
  "reasons": [],
  "differences": [
    {"output": "activity", "operation": "upsert", "added": 1, "removed": 0, "changed": 0, "columns": {}},
    {"output": "positions", "operation": "insert", "added": 1, "removed": 0, "changed": 0, "columns": {}}
  ]
}
```

Notice that the replay read nothing: it ran the transform again on the inputs the first run saved. The closed
account's trade adds one position and one activity row. The replay saved a manifest of its own beside the run's,
under its own run id, and stored only the two frames that changed: its inputs are the run's own objects, so
`snapshots/objects/` holds six datasets, not eight.

## Watch a check stop a run

Break one trade in the fixture. In `jobs/prices/tests/fixture/trades.csv`, change the last line from
`T5,A3,XS0002,2026-11-05,BUY,...` to `T5,A3,XS0002,2026-11-05,BYU,...`. Then run the job again:

```sh
uv run basis run prices --env dev --params jobs/prices/params/default.json --data jobs/prices/tests/fixture --interval 2026-11-05..2026-11-06
echo $?
```

You should see a warning naming the reason, a summary with `"status":"failed"` and `"reasons":1`, one `ERROR` line
that gives the reason and the fix, and an exit code of 1:

```text
{"time":"...","level":"warning","logger":"basis.job.runs","message":"not writing: check: trades have sides other than BUY and SELL: ['BYU']","run_id":"...","job":"prices"}
{"time":"...","level":"info","logger":"basis.job.runs","message":"run summary","run_id":"...","job":"prices","status":"failed","interval":"2026-11-05..2026-11-06","labels":{},"inputs":{"trades":5,"accounts":3},"outputs":{"positions":{"insert":3},"activity":{"upsert":2}},"reasons":1,"error":null,"publishes":{},"run_ms":...}
ERROR: run ... of prices wrote nothing, because check: trades have sides other than BUY and SELL: ['BYU']: fix it, and run the job again; `basis replay prices ...` reruns it on what it read
1
```

The reason comes from the job's own `check` in `jobs/prices/job.py`. A run that has a reason writes nothing to any
database, even with `--publish`, and its snapshot keeps its inputs, so `basis replay` can run it again after a fix.
`uv run basis check` fails the same way now. Change `BYU` back to `BUY` before you go on.

## Next steps

You have made a job repo, run its job, read its snapshot, replayed it with other params, and seen a check stop it.
With `--publish` and its databases configured, a run whose checks pass writes each database and reports
`"status":"published"`. To set that up, see [How to publish a job to a database for the first
time](how-to.md#publish-a-job-to-a-database-for-the-first-time) (`uv run basis docs job/how-to`). To write your own
job, run `uv run basis new job NAME` and read the [reference](reference.md) (`uv run basis docs job/reference`). For
why a job is shaped this way, read [How basis-job is put together](explanation.md) (`uv run basis docs
job/explanation`).
