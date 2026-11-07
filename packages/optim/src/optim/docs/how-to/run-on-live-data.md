# How to run a model on live data

Decide portfolios with what the firm's databases and APIs hold now, from a model's `main.py`, on your machine's
settings, QA's or production's. Each dataset says what it reads and from which source; a settings file says where
each source is.

Prerequisites:

- A model repo with `uv sync` run, and your editor's Python interpreter set to the repo's `.venv`.
- Read access to every source the model reads. `uv run optim describe MODEL` lists each dataset with its `source`;
  the income example reads `holdings`, `portfolios`, `controls` and `mandates` from SQL Server (`mssql`), `securities`
  from Oracle (`oracle`) and `signals` from the signals API (`rest`).
- The values of each source's variables, such as its connection string, from the environment's owner. The repo's
  `.env.example` lists every variable, and the [reference](../reference.md) describes each.

To run a model on live data:

1. Copy `.env.example`, at the repo's root, to `.env`, and in the copy uncomment the whole block of every source the
   model reads and set its values: where it is, such as `OPTIM_MSSQL__CONNECTION_STRING`, and how hard a run may press
   it, `MAX_CONCURRENT`, `TIMEOUT_S` and `MAX_ROWS`, which every source requires, as Oracle requires `MODE`. A block
   set in part stops the run, naming the line it lacks, such as `mssql.max_concurrent Field required`. For QA's data
   and production's, make `.env.qa` and `.env.prod` the same way, with each environment's read-only credentials. Git
   ignores all three files.
2. In `models/MODEL/main.py`, name the settings file with `env`, and the portfolios to decide:

   ```python
       results = run(model, params="params/default.json", portfolios=["P1", "P2"], env=".env.qa")
   ```

   - `portfolios=None` decides every portfolio the model lists in its `portfolios` dataset, as the income example
     does. A model without that dataset must be given its portfolios' ids. A portfolio given that the model does not
     list is rejected with `not one of the portfolios the model lists on 2026-10-16`.
   - `as_of`, a date or `"2026-10-16"`, is the business date every query takes. Without it, the run decides as of
     today in New York.
3. Run the file: press Run in your editor, or `uv run python models/MODEL/main.py` from a terminal. The run prints a
   row per portfolio, as `optim run` does, and `results` holds its decisions, trades, reasons and metrics as tables.

The settings file is found in the model's folder or a folder above it, up to the repo's root, wherever your editor
runs the file from. Its variables are set for the call alone, and the environment is as it was after it, so two calls
with different settings files in one session do not mix. A run that reads a source stops before it loads anything
when its settings file does not exist:

```text
FileNotFoundError: there is no .env.qa in /home/you/sma-optimizer/models/income or a folder above it: copy .env.example to .env.qa and set the sources income reads, or run on its fixture with data="tests/fixture"
```

A source the settings file leaves out cannot be read, and the first dataset that reads it stops the run, naming what
to set:

```text
LookupError: portfolios reads from mssql, which is not configured: set OPTIM_MSSQL__* (see .env.example)
```

A run never falls back from a source to the fixture: it reads the fixture only with `data="tests/fixture"`. A folder
given as `data` that holds only some datasets, as `NAME.csv` or `NAME.parquet`, replaces those alone, and the run reads
the rest from their sources, which suits a what-if on one dataset.

## Where the run goes

- A run from `main.py` saves its snapshot in the repo's `snapshots/`, whichever settings file it reads; git ignores
  the folder, and you may delete it whenever you like. `snapshots="PATH"` saves elsewhere, a path or a URL.
- It never writes orders, even when the settings file configures an orders table.
- It decides in `workers` processes: `OPTIM_WORKERS`, or 1 when neither says. With one, the run decides in this
  process, where a breakpoint in a rule or a hook stops. A run of hundreds of portfolios is faster with more, such as
  `workers=8`.
- A dataset with a cache age is read from the cache where `OPTIM_CACHE_ROOT` is set and the entry is fresh;
  `cache=False` reads every dataset live.

To confirm it read live data, open the run's `snapshots/RUN_ID/run.json`: `datasets` records where each dataset came
from, such as `"source": "mssql"` and `"read": "live"`, and `portfolios` the portfolios it decided.

## Run it in production

Airflow runs the repo's image, which runs `optim`, under `tini`, with the arguments it is given and the production
environment's variables:

```sh
optim run income --params models/income/params/default.json --as-of 2026-10-16
```

`optim run` decides as a run from `main.py` does, through the same code, with these differences: it reads the process's
environment, which `uv run --env-file .env optim run ...` sets on a machine; its paths are the working directory's; it
saves its snapshot under `OPTIM_SNAPSHOT_ROOT`, which it requires; it writes the accepted portfolios' orders where
`OPTIM_ORDERS__CONNECTION_STRING` and `OPTIM_ORDERS__TABLE` configure a table; and it exits 0 only when every portfolio
was accepted. `--portfolios P1 P2`, or `--portfolios @FILE` with one id per line, names the portfolios; without it, it
decides every portfolio the model lists.

> [!WARNING]
> `optim run` writes orders wherever its environment configures an orders table. Keep `OPTIM_ORDERS__*` out of the
> settings files on your machine, `.env.prod` included, which hold read-only credentials alone.

For QA, see [how to run the QA suite](run-the-qa-suite.md). To find out why a live run rejected a portfolio, see
[how to find out why a portfolio was rejected](find-out-why-a-portfolio-was-rejected.md).
