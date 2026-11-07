# How-to guides

Each section is one goal. The [reference](reference.md) has every option they use.

## Publish a job to a database for the first time

A job publishes only from `etl run --publish`, on a machine whose settings give each database the job writes a
`WRITE` block, and only once its tables and the database's ledger exist.

Prerequisites:

- A login that may read, and one that may insert, update and delete in the job's tables and the ledger. Creating the
  tables needs a login with DDL rights, once; runs never need one.
- The job passes `uv run pytest`.

To publish a job:

1. Print the job's tables in the destination's dialect, and the ledger's:

   ```sh
   uv run etl ddl prices --dialect mssql
   uv run etl ddl --ledger etl.publishes --dialect mssql
   ```

   Have someone with DDL rights run both. One ledger serves every job that writes to that database.

2. In the settings file the scheduler's container reads, configure each database the job names, with a `WRITE` block
   for each one it writes, as `.env.example` shows. `ETL_DATABASES__WAREHOUSE__WRITE__LEDGER` names the ledger from
   step 1.
3. Run the job once without publishing, against the real databases:

   ```sh
   uv run --env-file .env.prod etl run prices --params jobs/prices/params/default.json --interval 2026-11-05..2026-11-06
   ```

   The run checks each destination table against its row before it loads anything. If the summary's `status` is
   `failed`, its `reasons` name each column that does not match, or each value that does not fit.
4. Run it again with `--publish`. The exit code is 0 only when every database committed.

To confirm the publish, query the ledger: it has one row per table the run wrote, with the run's id and what the
database counted.

## Finish a run that published partly

A run that writes to two databases commits each in its own transaction, so a failure after the first commit leaves
`status` as `partial` in its `run.json`. Resume it, after fixing what failed:

```sh
uv run --env-file .env.prod etl resume prices RUN_ID
```

`resume` writes the run's saved changes, the same rows, to each database whose ledger does not have the run, and
exits 0 once every database has committed. A run that failed before publishing cannot be resumed: run it again.

## Publish an append-only table again for the same interval

A table written with `Append()` refuses a second publish of an interval that overlaps one the ledger already has,
since its rows would be inserted twice. If you mean to insert them twice, add `--again`:

```sh
uv run --env-file .env.prod etl run prices --params jobs/prices/params/default.json --interval 2026-11-05..2026-11-06 --publish --again
```

To rerun an interval without duplicating it, write the table with `ReplaceWhere(interval=COLUMN)` instead.

## Find out why a run failed

Every log record carries the run's `run_id`. The run's `run.json` is under `ETL_SNAPSHOT_ROOT/JOB/RUN_ID/`:

- `status` says how the run ended, `error` holds an error that stopped it, and `reasons` lists every reason it did
  not write: a row that does not match, a table that has drifted, a value that does not fit, or the job's own check.
- A transform that raised appears in `reasons` with the line of job code that raised it; the log holds the traceback.
- `publishes` has each database's outcome and error.

To reproduce the run on a laptop with the code you have now, replay it from the production snapshot root:

```sh
uv run --env-file .env.prod etl replay prices RUN_ID
```

A replay reads only the snapshot and writes nothing to a database.

## Check a change against recent production runs

`etl regress` replays a job's latest published runs with the code you have now, and lists every table whose changes
differ. It exits 1 if anything changed, so a pipeline can gate a merge on it:

```sh
uv run --env-file .env.prod etl regress prices --last 20 --label purpose=production
```

Each entry names the run, and each difference the output, the operation, and the rows added, removed and changed, by
column.

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
    new = inputs.prices.join(inputs.current, on="security_id", how="anti")
    return Outputs(prices=etl.Changes(insert_new=new))
```

The destination's rows are then part of the run's snapshot, so a replay sees what the run saw. The checks still apply:
an insert outside its delete, or a key repeated across operations, stops the run.
