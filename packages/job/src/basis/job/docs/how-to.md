# How-to guides

Each section is one goal. The [reference](reference.md) has every option they use.

## Publish a job to a database for the first time

A job publishes only from `basis run --publish`, on a machine whose settings give each database the job writes a
`WRITE` block, and only once its tables and the database's ledger exist.

Prerequisites:

- A login that may read, and one that may insert, update and delete in the job's tables and the ledger. Creating the
  tables needs a login with DDL rights, once; runs never need one.
- The job passes `uv run pytest`.

To publish a job:

1. Print the job's tables in the destination's dialect, and the ledger's:

   ```sh
   uv run basis ddl prices --dialect mssql
   uv run basis ddl --ledger etl.publishes --dialect mssql
   ```

   Have someone with DDL rights run both. One ledger serves every job that writes to that database.

2. In the settings prod's deployment gives the job's container, configure each database the job names, with a
   `WRITE` block for each one it writes, as `.env.example` shows, and `BASIS_ENV=prod` and `BASIS_PROD_WRITES=1`.
   `BASIS_DATABASES__WAREHOUSE__WRITE__LEDGER` names the ledger from step 1. Your own `prod.env` (see the reference's
   [Environments](reference.md#environments)) holds the same databases with your read-only logins.
3. Run the job once from your desktop without publishing, against prod's databases:

   ```sh
   uv run basis run prices --env prod --params jobs/prices/params/default.json --interval 2026-11-05..2026-11-06
   ```

   The run checks each destination table against its row before it loads anything. If the summary's `status` is
   `failed`, its `reasons` name each column that does not match, or each value that does not fit.
4. Let its deployment run it with `--publish`, which a desktop's prod run is refused. The exit code is 0 only when
   every database committed. In dev and QA, a desktop publishes with `--env dev --publish` or `--env qa --publish`.

To confirm the publish, query the ledger: it has one row per table the run wrote, with the run's id and what the
database counted.

## Finish a run that published partly

A run that writes to two databases commits each in its own transaction, so a failure after the first commit leaves
`status` as `partial` in its manifest. Resume it, after fixing what failed, where the run ran, since in prod only a
deployment that sets `BASIS_PROD_WRITES=1` may write:

```sh
basis resume prices RUN_ID --env prod
```

`resume` writes the run's saved changes, the same rows, to each database whose ledger does not have the run, and
exits 0 once every database has committed. A run told `--again` resumes as it ran, inserting an append-only table's
rows for an interval the ledger already has; `basis resume prices RUN_ID --again` does so for a run that was not. A
run that failed before publishing cannot be resumed: run it again.

## Publish an append-only table again for the same interval

A table written with `Append()` refuses a second publish of an interval that overlaps one the ledger already has,
since its rows would be inserted twice. If you mean to insert them twice, add `--again`:

```sh
basis run prices --env prod --params jobs/prices/params/default.json --interval 2026-11-05..2026-11-06 --publish --again
```

To rerun an interval without duplicating it, write the table with `ReplaceWhere(interval=COLUMN)` instead.

## Find out why a run failed

Every log record carries the run's `run_id`. The run's manifest is `BASIS_SNAPSHOT_ROOT/runs/job/JOB/RUN_ID.json`:

- `status` says how the run ended, `error` holds an error that stopped it, and `reasons` lists every reason it did
  not write: a row that does not match, a table that has drifted, a value that does not fit, or the job's own check.
- A transform that raised appears in `reasons` with the line of job code that raised it; the log holds the traceback.
- `publishes` has each database's outcome and error.

To reproduce the run on a desktop with the code you have now, replay it from prod's run store, which your `prod.env`
names:

```sh
uv run basis replay prices RUN_ID --env prod
```

A replay reads only the snapshot and writes nothing to a database.

## Check a change against recent production runs

`basis regress` replays a job's latest published runs with the code you have now, and lists every table whose changes
differ. It exits 1 if anything changed, so a pipeline can gate a merge on it:

```sh
uv run basis regress prices --env prod --last 20 --label purpose=production
```

Each entry names the run, and each difference the output, the operation, and the rows added, removed and changed, by
column.

## Run every job against QA before it is pushed

Declare the QA tier in `pyproject.toml`, as the reference's [QA tier](reference.md#qa-tier) shows: the marker QA's
names contain, QA's run stores, the tables each job may write there, and each job's params and interval. Then write
your own `qa.env` (`~/.config/basis/qa.env`, or `%APPDATA%\basis\qa.env` on Windows), which every repo shares, with
QA's databases and `BASIS_SNAPSHOT_ROOT`, as `.env.example` lists them:

```sh
uv run basis doctor        # says whether qa.env points only at QA, with the fix for anything that does not
uv run basis check --full  # what the push hook runs: check, regress, and each job published to QA
```

A setting that points outside QA, or a table the declaration does not list, is one `ERROR` line, and nothing runs.
On a machine without a `qa.env`, the push hook prints a `SKIPPED` line instead.

## Open a pull request with what the checks found

The push hook runs `basis check --full` on each commit pushed, and records what it found. `basis review` prints it as
Markdown, for the pull request's description:

```sh
git push
gh pr create --title "TITLE" --body "$(uv run basis review)"   # or paste its output into the pull request
```

It lists the outputs `regress` saw change, which the description should say are meant, what the QA tier ran, the
basis versions, and the commit checked. `regress` reads `BASIS_SNAPSHOT_ROOT` from the environment git runs the hook
in: set it there to the store whose published runs to replay.

## Write a table whose changes depend on what it holds

When no write mode fits, such as a history table or a dedupe against rows already written, write the table with
`Manual()`, read it as an input, and return the `Changes` yourself:

```python
class Inputs(etl.Inputs):
    prices = etl.Query(Price, database="markets", sql="sql/prices.sql", empty="fail")
    current = etl.Query(Price, database="warehouse", sql="sql/current_prices.sql", empty="allow")


class Outputs(etl.Outputs):
    prices = etl.Table(Price, database="warehouse", table="prices", key=("security_id",), write=etl.Manual())


def transform(inputs: Inputs, params: Params) -> Outputs:
    new = inputs.prices[~inputs.prices["security_id"].isin(inputs.current["security_id"])]
    return Outputs(prices=etl.Changes(insert_new=new))
```

The destination's rows are then part of the run's snapshot, so a replay sees what the run saw. The checks still apply:
an insert outside its delete, or a key repeated across operations, stops the run.
