# Reference

The repo layout, the model contract, the Python API, the command line, the settings, the snapshot files,
`optim.testing` and the pytest plugin of `optim`, as it runs on basis.

## The repo

`optim new repo REPO MODEL` makes this layout. Every repo's package is `models`, at the repo's root, installed in
editable mode, so `from models.MODEL import model` works whichever folder a file runs from.

| Path | Holds |
|---|---|
| `README.md` | The start page: a table of models, how to start, and which file each kind of change goes in. |
| `AGENTS.md`, `CLAUDE.md` | basis's block and the optim contract, each between the markers of the block `basis sync` writes; `CLAUDE.md` holds only `@AGENTS.md`. |
| `.agents/skills/optim-*/`, `.claude/skills/optim-*/` | The skills `basis sync` copies. |
| `pyproject.toml` | The `optim` version range and its source, `[tool.basis] parts = ["optim"]`, an entry point per model, and the pytest, ruff and ty settings. |
| `.env.example` | The settings, commented; copied to `.env`, `.env.qa` and `.env.prod`, which git ignores. |
| `Dockerfile`, `.dockerignore`, `azure-pipelines.yml` | The image Airflow runs, `basis run MODEL ... --publish` under tini, and a stub of the pipeline that checks and builds it. `basis sync` writes the Dockerfile and the pipeline. |
| `.githooks/`, `.gitattributes` | The commit hook, which fixes and formats with ruff and runs `basis check --staged`, and the push hook, which runs `basis check`; LF line endings. |
| `snapshots/` | Every run from Python, which git ignores. Safe to delete at any time. |
| `tests/e2e/test_smoke.py` | The QA suite, marked `e2e`. |
| `models/MODEL/` | One model. |
| `models/shared/` | What two or more models use, laid out like a model's folder; `basis new model` adds it with the second model. |

Each model is listed as `NAME = "models.MODEL.model"` under `[project.entry-points."optim.models"]`. A command names a
model by that `NAME`, or by its module path, as does a module path that is not an entry point, such as
`optim.example.model`. A name's hyphens become underscores in its folder.

## The model folder

| Path | Holds |
|---|---|
| `__init__.py` | Empty. |
| `README.md` | What the model does, in business terms: its objective, constraints, data and known limits. |
| `main.py` | One `optim.run(...)` call under `if __name__ == "__main__":`, which spawned workers need, since each imports the file that started the run. Its docstring shows `replay` and `sweep`. |
| `model.py` | The module the entry point names: `Params`, `Data`, `Metrics`, `Market`, `Portfolio`, and the hooks. |
| `prep.py` | Joins the datasets into the market, and each portfolio's starting universe and limits. |
| `datasets/` | One module per dataset, its row model and `Dataset`; its SQL in `datasets/sql/`. |
| `rules/` | One module per group of rules, each `(portfolio, market, params) -> portfolio`. |
| `optimizer/` | `solve.py`, `solve(portfolio, params) -> Decision`; `problem.py`, `build(portfolio, params)`, the variables, the objective's `terms` and the list of constraints; `constraints.py`, each constraint in cvxpy: trade sizes, cash, turnover and caps on weights. |
| `params/default.json` | The default params; other files beside it are variants. |
| `tests/` | `test_contract.py`, `builders.py`, the behaviour tests, and `fixture/`: a CSV or Parquet file per dataset and `fixture.json`, `{"as_of_date": "YYYY-MM-DD"}`. |

Workers import `model.py` and what it imports, never `main.py` or the tests.

## The model contract

`model.py` exports:

| Export | Required | Signature | Runs |
|---|---|---|---|
| `Params` | yes | a subclass of `optim.Params` | Validated from the params file once per run, before anything loads. |
| `Data` | yes | a subclass of `optim.Datasets` | Loaded once per run, in the parent process. |
| `decide` | yes | `decide(portfolio, market, params) -> optim.Decision` | Once per portfolio, in a worker; one at a time, in `order`, when `reserve` is defined. |
| `prepare_market` | no | `prepare_market(data, params) -> market` | Once per run, in the parent. Without it, `market` is the run's `Data`. |
| `prepare_portfolio` | no | `prepare_portfolio(data, market, params) -> portfolio` | Once per portfolio, always in parallel. `data` is the portfolio's slice of `Data`. Without it, `portfolio` is that slice. |
| `reserve` | no | `reserve(market, decision) -> market` | After each accepted decision, with the rounded decision. Returns the market the next portfolio decides with. |
| `order` | no | `order(portfolios, market, params) -> Iterable[str]` | Once, when `reserve` is defined. `portfolios` maps each prepared portfolio's id to what `prepare_portfolio` returned. Must return each of those ids once. Without it, portfolios decide in the run's order. |
| `check` | no | `check(portfolio, decision) -> Iterable[str]` | Once per decision, with the rounded decision. Returns reasons to reject it, beyond the core's; none accepts it. |
| `Metrics` | no | a subclass of `optim.Metrics` | Declares the metrics decisions report. A model that reports metrics must declare them all. |

`Market` and `Portfolio` are the model's own classes, usually frozen dataclasses; `optim` reads nothing in them. They
must pickle: the market travels to worker processes, and a portfolio comes back from one when the model reserves.

An exception in `prepare_portfolio`, `decide` or `check` rejects that portfolio alone. Its reason is the error and the
last line of the model's code it passed through, `TYPE: MESSAGE, at PATH:LINE in FUNCTION`, such as
`KeyError: 'sector', at models/income/rules/screens.py:14 in exclude_sectors`, where the model's code is everything
beside the model's folder, `models/shared/` included; the log holds the traceback. An exception in `prepare_market`,
`order` or `reserve` fails the run.

`optim` defines no constraint. The example's constraints are cvxpy, in `optimizer/constraints.py`, and read their
limits from `Params`, such as `max_issuer_weight`, and from the portfolio, such as its `sector_caps`; see
[how to add a constraint](how-to/add-a-constraint.md).

### The portfolios dataset

A model may declare a dataset as `portfolios` in its `Data` (`optim.contract.LISTING`): the portfolios it manages, one
row each, with a `portfolio_id` and any other columns. Its query takes only the as-of date, and it is read whole, before
the other datasets.

| When | What happens |
|---|---|
| A run is given no portfolios | It decides every portfolio the dataset lists, in its order. |
| A run is given portfolio ids | It decides those; one the dataset does not list is rejected, `not one of the portfolios the model lists on DATE`. |
| A portfolio's hooks run | Its slice holds its own row; `prepare_market` sees every row. |
| The model declares none | A run must be given its portfolios, unless its holdings are read from a file, when it decides every portfolio in the file. |

## `optim.Params`

The base of a model's `Params`, and of any model nested in them. It refuses unknown keys, does not coerce types (a JSON
integer is a valid float, a string is not a number), and is frozen. Field docstrings become the field descriptions
`basis describe` prints. A params file is one JSON object; a field it leaves out takes its default.

## `optim.Datasets`

The base of a model's `Data`. Each class attribute that is an `optim.Dataset` is a dataset the model reads; on an
instance, the same attribute is that dataset's `DataFrame`. It declares `holdings = HOLDINGS` and
`securities = SECURITIES`, which a model may replace with a dataset of the same row model.

| Attribute | Type | Description |
|---|---|---|
| `frames` | `dict[str, DataFrame]` | Each dataset's frame, by the dataset's `name`. |
| `as_of_date` | `date` | The run's business date. |
| `portfolio_id` | `str \| None` | The portfolio a slice holds; `None` for the run's whole data. |

A portfolio's slice holds that portfolio's rows of each dataset whose row model has a `portfolio_id` field, the
`portfolios` dataset included, and every other dataset whole. Each frame in a slice is a shallow copy. Two datasets of
one `Data` must not share a `name`.

## `optim.Dataset`

```python
Dataset(name, row, source, query, key, empty, cache=None, check=None)
```

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Names its frame, its fixture file and its dataset in a run's snapshot. |
| `row` | `type[optim.Row]` | The row model. Field names are column names; each field's annotation sets its dtype and holds its rules. A row model with a `@field_validator` or `@model_validator` is refused. |
| `source` | `str` | Where rows come from: a database, by the name its settings give, such as `"mssql"` for `BASIS_DATABASES__MSSQL__*`; or `"rest"` or `"file"`, which no database may be named. A database's name is lower-case letters, digits and underscores. |
| `query` | `str \| Path` | A SQL file in the database's own dialect for a database, a URL path for `rest`, a Parquet or CSV file's path or URL for `file`. `{as_of_date}` in a file's path becomes the run's date. |
| `key` | `tuple[str, ...]` | Columns whose values no two rows share; a duplicate fails the load. |
| `empty` | `"fail"` or `"allow"` | What a load that returns no rows for the whole run means: `"fail"`, a feed that has not landed, stops the run before it decides; `"allow"`, a day with nothing in it, such as no controls on any portfolio. |
| `cache` | `timedelta \| None` | How long a cache entry stays fresh; `None` reads the dataset live on every load. |
| `check` | `Callable[[DataFrame], None] \| None` | Rules across rows, which raise `ValueError` on rows that disagree. |

`per_portfolio` is true when `row` has a `portfolio_id` field. `file(path)` returns the same dataset read from a file,
uncached.

Every query binds the business date as `as_of_date` (`:as_of_date` for an Oracle database, `%(as_of_date)s` for a SQL
Server one, a query parameter for REST), and reads in a transaction that only reads, which sees the database at one
moment. A per-portfolio dataset other than `portfolios` also binds `portfolios`, a JSON array of up to 1,000 ids per
query, which SQL Server unpacks with `OPENJSON(%(portfolios)s) WITH (id VARCHAR(64) '$')` and Oracle with `JSON_TABLE`.
Oracle refuses a bind its SQL does not use. A file source filters a per-portfolio file to the batch's `portfolio_id`s; a
CSV's values are read as text and validated into the row model's types, and an empty field is null. Field types for rows
are in `optim.schema`: `Id`, `Cusip`, `Isin`, `Currency`, `Price`, `Quantity`, `Weight` and `Rating`, an ordered
category best first. A `Literal` field becomes an ordered category in its declared order.

## Shared datasets

`optim.datasets` holds the datasets every model reads. A dataset only one repo reads belongs in that repo.

`HOLDINGS`: `Holding` rows from SQL Server, read per portfolio, key `portfolio_id, security_id`, never cached. Working
holdings: the start of the day plus what has traded since.

| Column | Type | Description |
|---|---|---|
| `portfolio_id` | id | |
| `security_id` | id | |
| `quantity` | float | Face amount for a bond, currency units for cash. Cash may be negative. |

`SECURITIES`: `Security` rows from Oracle, read whole, key `security_id`, cached 15 minutes.

| Column | Type | Description |
|---|---|---|
| `security_id` | id | The canonical id every dataset joins on. |
| `cusip`, `isin` | str or null | Identifiers the security may also have. |
| `asset_class` | `bond`, `cash` or `other` | A holding of a `cash` security is the portfolio's cash. |
| `currency` | ISO 4217 code | |
| `issuer`, `sector` | str or null | |
| `rating` | rating or null | An ordered category, best first. |
| `maturity` | date or null | |
| `price` | float > 0 | Clean price per `price_per` of quantity. |
| `accrued` | float | Accrued interest per `price_per` of quantity. |
| `price_per` | int > 0 | 100 for a bond quoted in percent of par, 1 for cash. |
| `increment` | float > 0 | Every trade is a whole number of increments. |
| `min_piece` | float > 0 or null | The smallest trade, where the market sets one. |
| `min_remaining` | float >= 0 or null | The smallest holding a partial sale may leave. |

`unit_value(securities)` returns what one unit of each security's quantity is worth: `(price + accrued) / price_per`.

## `optim.Decision`

| Field | Type | Default | Description |
|---|---|---|---|
| `buy` | `dict[str, float]` | `{}` | Quantity to buy, by security id. A `pd.Series` indexed by security id is accepted. |
| `sell` | `dict[str, float]` | `{}` | Quantity to sell, by security id. |
| `solved` | `bool` | `True` | Whether the problem was solved. A decision that was not is rejected. |
| `objective` | `float \| None` | `None` | The objective's value. |
| `solver` | `SolverStats \| None` | `None` | How the solver stopped. |
| `metrics` | `dict[str, float]` | `{}` | Numbers for sweeps. A `Metrics` instance is accepted. |

A quantity that is not a finite number is refused when the decision is made. `SolverStats`:

| Field | Description |
|---|---|
| `solver` | The solver's name. |
| `status` | The solver's own status, for diagnosis. |
| `stop` | `optimal` (proved optimal within the gap asked), `limit` (stopped by a limit, usually the time), `imprecise`, `violated` (the answer breaks a constraint at the values returned), `infeasible`, `unbounded` or `failed`. |
| `gap` | For an integer problem with a solution: the relative gap the solver reports. |
| `nodes` | For an integer problem: branch-and-bound nodes explored. |
| `iterations`, `seconds` | Iterations, and time in the solver itself. |

## `optim.cvxpy`

`SolveSettings`, a strict params model to nest in a model's `Params`:

| Field | Type | Default | Description |
|---|---|---|---|
| `solver` | `"HIGHS"` or `"COPT"` | `"HIGHS"` | COPT needs the `copt` extra; without a license it runs size-limited. |
| `time_limit_s` | float > 0 | `30.0` | A solve that reaches it is not solved. |
| `mip_gap` | float >= 0 | `0.001` | The relative gap at which an integer answer counts as optimal. |
| `options` | `dict` | `{}` | Passed to the solver by its own option names; they override COPT's one thread, and an option that sets the time limit or the gap, such as `mip_rel_gap` or `RelGap`, is refused: the fields above set those. |

`solve(problem, settings, *, securities, buy=None, sell=None, metrics=None) -> Decision` solves `problem` with
`warm_start=False`, then reads it. `read(problem, *, securities, buy=None, sell=None, metrics=None) -> Decision` reads a
problem the model solved itself. `buy` and `sell` are cvxpy expressions in the holdings'
units, one entry per id in `securities`, which names each security once. `metrics` maps names to expressions or numbers, evaluated at the solution. A
trade smaller than 1e-6 is dropped. The decision is solved only when the solver stopped `optimal` and the answer meets
every constraint at the values returned, within 1e-6 of the constraint's size, or of 1 below that, else it stops
`violated`, keeping its trades for the snapshot; without a solution it has no trades. A problem with no variables is solved with no trades, without calling a solver.

## Rounding and acceptance

After `decide`, the core rounds each quantity to the nearest whole number of its security's `increment`, sells the whole
holding where a sale is within half an increment of it, and drops a quantity that rounds to zero. It then rejects the
rounded decision, with these reasons, in this order, when:

| Reason | When |
|---|---|
| `it has no holdings: is its id right?` | The portfolio has no rows in `holdings`. |
| `not solved: STOP` | `solved` is false. |
| `trades securities that are not loaded: [...]` | A trade's security is not in `securities`. |
| `trades cash as a security: [...]: ...` | A trade's security is cash, whose `asset_class` is `cash`: cash moves with the other trades. |
| `buys quantities that are not positive: [...]`, `sells ...` | A quantity is zero or negative. |
| `sells Q of S, more than its holding of H` | A sale exceeds the holding, by any amount. |
| `rounding moved its SIDE of S from X to Y: decide whole increments` | Rounding changed a trade's value by more than the tolerance. |
| `leaves cash of C: it spends more than it has` | Cash holdings plus sales less buys, at unit values, is below minus the tolerance. |

The tolerance is 1e-6 of the portfolio's value, its holdings at unit values, $1 per $1mm, and never more than $1, in
the portfolio's currency, however large the portfolio. The rounding check is why no
constraint needs checking again after rounding: a model that decides whole increments passes it, and the solver proved
the unrounded decision meets its constraints. The model's `check` adds its reasons after the core's. A portfolio can
also be rejected before it is decided: `not one of the portfolios the model lists on DATE`, a hook's error, or
`error: its worker process died`.

## The Python API

`optim.run`, `optim.replay` and `optim.sweep` run a model from Python, as its `main.py` does. Each takes the model's
`model.py` module first:

```python
from optim import run

from models.income import model

if __name__ == "__main__":
    results = run(model, params="params/default.json", portfolios=None, env=".env")
```

They run as `basis run`, `basis replay` and `basis sweep` do, through the same code, with these differences:

| | From Python | From the command line |
|---|---|---|
| Relative paths | The model's folder's, wherever the process runs | The working directory's |
| Settings | The settings file `env` names, set in the process for the call alone and restored after it, over the process's environment | The process's environment |
| Snapshots saved | In the repo's `snapshots/`: the folder beside the nearest `pyproject.toml` above the model's, or `snapshots/` in the working directory without one | Under `BASIS_SNAPSHOT_ROOT`, which it requires |
| Orders | Never written | Written by `basis run --publish`, to the table `BASIS_OPTIM__ORDERS__*` configures |
| Logs | Text, unless `BASIS_LOG_FORMAT` says otherwise | JSON lines, unless it says otherwise |
| Errors | Raise, with their traceback | Logged once, and exit 1 |

`env` is found in the model's folder or the nearest folder above it that has it, up to the repo's root. A run whose
datasets need a source, and whose settings file is not found, stops: `there is no .env in FOLDER or a folder above it:
copy .env.example to .env ...`. With `env=None`, a call reads the process's environment alone.

### `optim.run`

```python
run(model, *, params="params/default.json", portfolios=None, as_of=None, env=".env", data=None, workers=None, labels={}, snapshots=None, cache=True) -> Run
```

Decides portfolios with the model, saves the run, prints what `basis run` prints, and returns the `Run`.

| Argument | Default | Description |
|---|---|---|
| `params` | `"params/default.json"` | The params file. |
| `portfolios` | every portfolio the model lists | Portfolio ids. See [the portfolios dataset](#the-portfolios-dataset); a model that lists none decides every portfolio in a holdings file read from `data`. |
| `as_of` | the fixture's date with `data`, or today in New York | A `date`, or `YYYY-MM-DD`. The fixture's date is `fixture.json`'s, where `data` has one. |
| `env` | `".env"` | The settings file, which says where each dataset's source is; `None` reads the process's environment alone. |
| `data` | none | A folder whose files, `NAME.csv` or `NAME.parquet`, replace those datasets' sources, such as `"tests/fixture"`; every other dataset is read from its source. |
| `workers` | `BASIS_OPTIM__WORKERS`, or 1 | Processes that decide; 1 decides in this process, where a breakpoint stops. |
| `labels` | none | Recorded in the run's manifest, as `--label` records them. |
| `snapshots` | the repo's `snapshots/` | The run store to save the run in: a path, relative to the model's folder, or a URL. |
| `cache` | on | Reads cached datasets from `BASIS_OPTIM__CACHE_ROOT`, where it is set; off reads every dataset live. |

### `optim.replay`

```python
replay(model, run, *, params=None, portfolios=None, env=".env", workers=None, labels={}, snapshots=None) -> Run
```

Decides a saved run of the model again, with the code you have now and other params if given, saves it as a new run,
prints what `basis replay` prints, and returns it. `run` is a run's id, found in the repo's `snapshots/`, then under the
settings file's `BASIS_SNAPSHOT_ROOT`, such as production's in `.env.prod`; or the path of its manifest, relative to the
model's folder, or its URL. A run of another model is refused. `params` defaults to the params the run used.
`portfolios` decides those alone; for a model that reserves, the portfolios before them in its order decide again too.
It reads no source and writes no orders.

### `optim.sweep`

```python
sweep(model, run, *, params, env=".env", workers=None, snapshots=None) -> DataFrame
```

Decides a saved run of the model once per params file in `params`, each relative to the model's folder, every file
validated before any decides, saves nothing, and returns the [sweep table](#basis-sweep). `run` is found as `replay`
finds it. `workers` defaults to `BASIS_OPTIM__WORKERS`, or 1.

### `Run`

| Attribute | Description |
|---|---|
| `run_id` | The run's id, which names its manifest. |
| `ok` | Every portfolio was accepted. |
| `decisions` | A row per portfolio, as the snapshot's `results/decisions`. |
| `trades` | A row per trade, as `results/trades`. |
| `reasons` | A row per reason a portfolio was rejected, as `results/reasons`. |
| `metrics` | A row per portfolio and metric, as `results/metrics`. |
| `outcomes` | Each portfolio's `Outcome`: `portfolio_id`, `decision` (rounded), `decided` (as the model decided it), `reasons`, `numbers` (`value`, `cash`, `turnover`), `seconds` and `accepted`. |
| `summary()` | What `basis run` prints: `run RUN_ID`, a row per portfolio, and each reason. |

## The command line

A model repo's command is `basis`, and `uv run basis --help` lists every verb it has; `optim` is the same command, under
its earlier name. `run`, `describe`, `resume`, `replay`, `regress` and `ddl` are verbs jobs have too: in a repo with
both jobs and models, the model's name comes first, and `basis VERB NAME --help` gives the model's arguments. Every verb
reads its settings from the process's environment, as `uv run --env-file .env basis ...` sets them, logs JSON lines on
stderr, ends its stderr with a log record, `finished VERB`, with its exit code and how long it took, and then prints
each failure as one line, `ERROR: REASON: FIX`, the last. Paths are the working directory's. `--portfolios` takes ids,
or `@FILE` with one per line.

### `basis run`

```text
basis run MODEL --params FILE [--as-of DATE] [--portfolios ID ...] [--data DIR] [--label KEY=VALUE] [--workers N] [--no-cache] [--publish [--again]]
```

Validates the params, loads the model's data, decides each portfolio, and saves the snapshot under
`BASIS_SNAPSHOT_ROOT`, which it requires. With `--publish`, it then writes the accepted portfolios' orders to the
orders table `BASIS_OPTIM__ORDERS__*` configures; it and `basis resume` are the only things that write orders. Prints
`run RUN_ID`, a row per portfolio, each reason, and, with `--publish`, a line saying what the publish wrote or why it
failed. Exits 0 only when every portfolio was accepted and the publish, if asked for, committed.

A publish writes every order in one transaction with the run's row in the ledger, `BASIS_DATABASES__NAME__WRITE__LEDGER`
of the orders database, as basis-core publishes a job's tables: either every order is written, with its ledger row, or
none is. The ledger keeps a run's orders from being written twice:

- A run the ledger holds is not written again, however often its publish is retried or resumed.
- A run's interval in the ledger is its as-of date, under its model's module. A run of the same model for a date that
  another run published orders for stops before it loads, and its publish is refused again as it writes, unless it is
  given `--again`. To publish the portfolios a published run rejected, decide those alone, with
  `--portfolios ... --again`.
- `--again` still guards each portfolio: it needs `--portfolios`, and a run given it, or a resume told it, is refused
  while any portfolio it publishes has orders in the table from another run of the model for the date.
- A run with no orders, as when every portfolio was rejected, publishes nothing, not even a ledger row, so running it
  again for the same date is not refused.

`--publish` stops the run before it loads, naming the fix, when no orders table is configured, when the table does not
exist or cannot hold the orders' columns (`basis ddl` prints one that can), or when the date's orders are published
already. `--again` without `--publish` is refused, and so is `--publish` with `--data`, whose files, such as a
fixture's, are no portfolio's. A run without `--publish` where an orders table is configured writes
none, and its last line says so, `orders: none written to TABLE, since the run was not given --publish`, so a
scheduled run that leaves the option out is seen. The orders are written after the snapshot is saved, so a publish
that fails keeps the run, and `basis resume` writes its orders.

| Option | Default | Description |
|---|---|---|
| `--params` | required | The params JSON file. |
| `--as-of` | the fixture's date with `--data`, or today in New York | The business date, `YYYY-MM-DD`. The fixture's date is `fixture.json`'s, where `--data`'s folder has one, as for `optim.run`. |
| `--portfolios` | every portfolio the model lists, or with `--data`, every one in its holdings | Portfolio ids, or `@FILE` with one per line. |
| `--data` | none | Reads each dataset that has a file in the directory, `NAME.parquet` or `NAME.csv`, instead of its source; not with `--publish`. |
| `--label` | none | `KEY=VALUE`, recorded in the run's manifest; repeat for more. |
| `--workers` | `BASIS_OPTIM__WORKERS`, or 1 | Worker processes. |
| `--no-cache` | off | Reads every dataset live. |
| `--publish` | off | Writes the accepted portfolios' orders to the orders table, through its database's write login. |
| `--again` | off | With `--publish` and `--portfolios`: publishes even though another run of the model published orders for the as-of date, for portfolios that run did not order. |

### `basis resume`

```text
basis resume [MODEL] RUN_ID [--again]
```

Publishes the orders of a run of `basis run --publish` whose publish did not commit, from its snapshot: the same rows,
to the same table, which the settings must still name. `RUN_ID` is found as `basis replay` finds it, and `MODEL`, where
given, must be the run's. A run whose publish committed is left as it is, and so is one whose publish committed before
its manifest recorded it, as when the process was stopped between the two: the ledger holds the run, and the resume
saves and prints what its row says that earlier attempt wrote, and when. `--again` publishes even though another run of
the model published orders for its as-of date, for portfolios that run did not order; a run given `--again` resumes
with it. Saves the outcome in the run's
manifest, prints what it wrote or why it failed, and exits 0 when the publish committed or the run had no orders.

### `basis ddl`

```text
basis ddl MODEL --dialect mssql|oracle
basis ddl --ledger TABLE --dialect mssql|oracle
```

Prints the `CREATE TABLE` statement for the orders table `BASIS_OPTIM__ORDERS__TABLE` names, which every model
publishes to, or with `--ledger`, for the ledger `TABLE`, which the orders database's
`BASIS_DATABASES__NAME__WRITE__LEDGER` names. `MODEL` is a model's name in the `optim.models` entry points, or its
module path there; its code is not imported. A run never creates or alters a table: these are for whoever may create
them. The orders table holds `run_id`, `portfolio_id`, `security_id`, `side` and `quantity`, with the primary key
`(run_id, portfolio_id, security_id, side)`; `quantity` is a float column, `FLOAT` or `BINARY_DOUBLE`, as every value
in optim is. A table a publish writes may differ, as long as its columns can hold the orders' values exactly.

### `basis replay`

```text
basis replay [MODEL] RUN [--params FILE] [--portfolios ID ...] [--label KEY=VALUE] [--workers N]
```

Decides a saved run again with the code installed now, and other params if given, and saves it as a new run under
`BASIS_SNAPSHOT_ROOT`. `RUN` is a run id under `BASIS_SNAPSHOT_ROOT`, or the path or URL of its manifest; `MODEL`,
where given, must be the model whose run it is, and is needed first in a repo that has jobs too. Reads no
source and writes no orders. A replay of a subset decides that subset; for a model that reserves, the portfolios before
them in its order decide again too. A replay of a replay of a subset replays that subset. Exit status as `run`.

### `basis sweep`

```text
basis sweep RUN VARIANTS [--out FILE] [--workers N]
```

Decides the run once per `*.json` params file in the directory `VARIANTS`, every file validated first, and writes one
table, as CSV on stdout or to `--out` as `.csv` or `.parquet`. Reads only the snapshot and saves none. `--workers`
defaults to 1. Columns: `variant` (the file's stem), `portfolio_id`, `accepted`, `objective`, `turnover`, `trades`,
`cash`, `solve_seconds`, `seconds`, one column per metric, and `reasons`, joined with `; `. Exits 0.

### `basis regress`

```text
basis regress [MODEL] [--snapshots ROOT] [--last N] [--label KEY=VALUE] [--out FILE] [--workers N]
```

Replays the model's, or without `MODEL` every model's, `--last` (default 20) most recent runs under `--snapshots`
(default `BASIS_SNAPSHOT_ROOT`), replays aside and only those with every `--label` given, with each run's own params,
and writes a row per change: `run_id`, `portfolio_id`, `status_before`, `status_after`, `limit` (either decision
stopped at a time limit), and for a changed trade `security_id`, `side`, `before` and `after`, the rounded quantities.
With `MODEL`, the rows are a table, CSV on stdout or the `--out` file; without it, each row also names its `model`,
and they are JSON on stdout, a list of objects, or the `--out` file. In a repo with jobs too, `basis regress` without a
name takes only `--snapshots`, `--last` and `--label`, which a job's takes as well, and prints one JSON object, each
part's result by part, `job` and `optim`. Exits 1 if anything changed.

### `basis describe`

```text
basis describe [MODEL]
```

Prints JSON: `model`, `module`, `optim_version`, `hooks` (the optional hooks it defines), `params` (the JSON schema),
`datasets` (by attribute: `name`, `source`, `per_portfolio`, which is false for `portfolios`, `key`, `cache_seconds`,
`columns`) and `metrics` (the JSON schema, or null). Without `MODEL`, it prints each model the repo lists, by name,
with its module path.

### `optim new repo` and `basis new model`

```text
optim new repo NAME MODEL [--index URL | --framework-path PATH] [--dir DIR]
basis new model NAME
```

`optim new repo` makes `DIR/NAME` (default here), which must not exist: basis's files, the hooks and the agent files
among them, optim's templates, and one model, `models/MODEL/`, copied from optim's example; then it runs `git init`,
points the clone at its hooks, runs `uv sync`, and leaves the first commit to you. It runs outside any repo, as
`uvx optim new repo` runs it. The repo declares `[tool.basis] parts = ["optim"]`, and depends on optim at the minor
version that made it, from the source its `pyproject.toml` names: `--index`, the internal index, which every package
then comes from; `--framework-path`, a checkout of basis, whose `packages/optim` it installs editable, to try a change
to optim; or, with neither, the commit optim was installed from, as `uvx --from 'git+URL@REF#subdirectory=packages/optim'
optim new repo` installs it, or else the release's tag. It never takes optim from PyPI, which has an unrelated package
of that name.

`basis new model` adds `models/NAME/`, copied from the example, to the repo, with its entry point and a row in the
`README.md`'s table of models, and adds `models/shared/`, with a `README.md`, when it is the repo's second model; then
it runs `uv sync`. A name that is taken, or cannot name a Python module, is refused.

### `basis sync`, `basis docs` and `basis warm`

`basis sync` writes the files basis owns, as the installed versions of basis and optim write them: the Dockerfile,
the pipeline stub and the hooks; basis's block of `AGENTS.md` and optim's, the contract, keeping what is written
around them; `CLAUDE.md`; and the `optim-*` skills, in `.agents/skills/` and `.claude/skills/`, deleting copies of
skills optim no longer ships. `basis check` fails when one of them differs. `basis docs` lists these docs, as
`optim/PAGE`, and `basis docs optim/PAGE` prints one, such as `optim/how-to/add-a-dataset`. `basis warm MODEL ...
[--as-of DATE]` reads every cached dataset the models read whole, the `portfolios` dataset included, live, and
overwrites its cache entry; it needs `BASIS_OPTIM__CACHE_ROOT` and exits 1 if any load failed.

## Settings

Settings come only from `BASIS_*` environment variables: on the command line, the process's; from Python, the settings
file `env` names, over the process's. Nested fields take a double underscore, as in `BASIS_DATABASES__ORACLE__DSN`.
A database a dataset names is read once its block, `BASIS_DATABASES__NAME__*`, and its limit,
`BASIS_OPTIM__DATABASES__NAME__MAX_CONCURRENT`, are set; a source that is not set cannot be read, and a dataset that
reads from it fails its load, naming the variables to set. A variable optim 0.2.0 read, under `OPTIM_`, is refused,
naming the variable that replaced it.

| Variable | Default | Description |
|---|---|---|
| `BASIS_SNAPSHOT_ROOT` | none | The run store where `basis run` saves its runs, and where a replay finds a run by its id: a path, or a URL `pyarrow.fs` opens, such as `abfs://...`. Required by `basis run`, `basis replay`, and `basis resume`, `basis sweep` and `basis regress` given a run id; a run from Python saves in the repo's `snapshots/` instead. |
| `BASIS_DATABASES__NAME__KIND` | required with the database | `mssql` or `oracle`. `NAME` is the name a dataset's `source` gives, upper-cased: optim's shared holdings read `mssql`, and its securities `oracle`. |
| `BASIS_DATABASES__NAME__CONNECTION_STRING` | required with SQL Server | A login that may read. The primary, not a readable secondary: holdings must include the trades just booked. |
| `BASIS_DATABASES__NAME__DSN`, `__USER`, `__PASSWORD`, `__MODE` | required with Oracle | `MODE` is `thin` (no client libraries) or `thick`, which loads Oracle Instant Client. |
| `BASIS_DATABASES__NAME__CLIENT_LIB_DIR` | none | Oracle in thick mode: where Oracle Instant Client is, if not on the library path. |
| `BASIS_DATABASES__NAME__ISOLATION` | required with the database | How a run's reads see it. optim reads each batch of portfolios at one moment, so it requires `snapshot` of a SQL Server database it reads, which the database must allow, and `read_only` of an Oracle one; it refuses `read_committed`, which reads each query at its own, there. A database that only receives the orders may take either. |
| `BASIS_DATABASES__NAME__TIMEOUT_S`, `__MAX_ROWS` | required with the database | Seconds each statement may take, the login included, which never waits more than 30, and the most rows a query may return; each at least 1. |
| `BASIS_DATABASES__NAME__WRITE__CONNECTION_STRING`, or `__WRITE__USER` and `__WRITE__PASSWORD` | none | The orders database's write login, apart from the one that reads: a login that may insert into the orders table and its ledger, and read both. |
| `BASIS_DATABASES__NAME__WRITE__LEDGER` | none | The orders database's ledger, as `schema.table`, which `basis ddl --ledger` creates: a row per run that published orders, written in the same transaction. Required with the orders table. |
| `BASIS_OPTIM__DATABASES__NAME__MAX_CONCURRENT` | required with the database | Reads a run has open to the database at once, each a transaction of its own; at least 1. |
| `BASIS_OPTIM__REST__URL`, `__MAX_CONCURRENT`, `__TIMEOUT_S`, `__MAX_ROWS` | required with REST | The base URL of a JSON API that answers a GET with a list of records; calls open at once, seconds per call, and the most records a call may return, each at least 1. |
| `BASIS_OPTIM__CACHE_ROOT` | none | The dataset cache, a path or URL; without it nothing is cached. |
| `BASIS_OPTIM__WORKERS` | `1` | Worker processes per run, at least 1; `1` decides in the calling process. |
| `BASIS_OPTIM__ORDERS__DATABASE`, `__TABLE` | none | Production only: the database whose write login inserts the orders, and the table they are inserted into, as `run_id, portfolio_id, security_id, side, quantity`, which `basis ddl` creates. `TABLE` is `schema.table`. The database's block needs a write login and a ledger. Only `basis run --publish` and `basis resume` write to it, each statement bounded by the database's `TIMEOUT_S`. |
| `BASIS_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`, for `optim`'s and models' loggers. |
| `BASIS_LOG_FORMAT` | `json`; `text` from Python | `json` lines, or `text` for a person. |
| `BASIS_GIT_SHA`, `BASIS_IMAGE` | none | Set in an image: the commit and image a run records. |

## Snapshots

Every run is kept in a run store, `ROOT`: `BASIS_SNAPSHOT_ROOT` for `basis run` and `basis replay`, and the repo's
`snapshots/` for a run from Python. A run is a manifest, a JSON file that holds its record and names each dataset it
saved by the SHA-256 of its object, a Parquet file the store holds once however many runs save it, so the morning's
holdings that twenty runs load are stored once. The objects are written first and the manifest last, each under a
hidden name renamed into place, so a reader never sees half a run.

```text
ROOT/
  runs/optim/REPO.MODEL/RUN_ID.json   each run's manifest, under its repo's name and the model's
  objects/HASH.parquet                each dataset, once
```

A manifest's `datasets` names its objects:

```text
inputs/<dataset>     each dataset as loaded, validated on read by the model's row models
results/decisions    a row per portfolio
results/trades       a row per trade
results/reasons      a row per reason a decision was rejected
results/metrics      a row per portfolio and metric
outputs/orders       a row per order: each accepted portfolio's rounded trades, as `--publish` writes them
```

The manifest's record:

| Key | Value |
|---|---|
| `run_id` | A UUIDv7. |
| `optim_version` | The `optim` the run ran. |
| `model` | `name`, as the run named it, and `module`. |
| `as_of_date` | The business date. |
| `portfolios` | Every portfolio the run loaded, in the order asked or listed; a replay keeps its run's. |
| `params` | Every param, defaults included. |
| `params_file` | `path`, `sha256` and `git_commit` of the file they came from. |
| `labels` | The labels given. |
| `code` | `git_sha`, `image` and `dirty` of the model's code. |
| `workers` | Worker processes. |
| `started_at`, `loaded_at`, `finished_at` | UTC times: before loading, once loaded, after the last decision. |
| `reads` | Per dataset, its `source`, `read` (`live`, `hit`, `miss` or `refresh`) and `fetched_at`; null for a replay. |
| `order` | For a model that reserves, the order its portfolios decided in; otherwise null. |
| `replay` | For a replay, `of`, the run it replayed, and `portfolios`, the subset it decided; otherwise null. |
| `publishing` | For a run of `basis run --publish`, the orders table's `database` and `table`, and `again`; otherwise null. Saved before the orders are written. |
| `publishes` | Per database the run published its orders to, its `status` (`committed` or `failed`), `at`, the `counts` the database gave, by output, and the `error` of a failure; empty until then, and for a run with no orders. |
| `datasets` | Each dataset above, by its object's hash. |

`results/decisions` columns: `portfolio_id`, `status` (`accepted` or `rejected`), `trades`, `value` (before trading),
`cash` (after trading), `turnover`, `solved`, `objective`, `solver`, `solver_status`, `stop`, `gap`, `nodes`,
`iterations`, `solve_seconds` and `seconds`. `results/trades`: `portfolio_id`, `security_id`, `side`, `quantity`
(rounded; 0 where rounding left nothing) and `decided` (as the model decided it). `results/reasons`: `portfolio_id`,
`reason`. `results/metrics`: `portfolio_id`, `metric`, `value`. `outputs/orders`: `portfolio_id`, `security_id`, `side`
and `quantity`, the table's columns but for the run's id, which the manifest holds.

## `optim.testing`

What a model repo's tests use from `optim`.

`ContractTests` holds one model's contract tests. A model's `tests/test_contract.py` subclasses it:

```python
from optim.testing import ContractTests

from models.income import model


class TestContract(ContractTests):
    model = model
```

| Attribute or test | Default | Fails when |
|---|---|---|
| `model` | required | The model's `model.py` module. |
| `params` | `"params/default.json"` | Relative to the model's folder. |
| `fixture` | `"tests/fixture"` | Relative to the model's folder. |
| `test_its_default_params_validate` | | The params file is missing or does not validate. |
| `test_every_portfolio_of_its_fixture_is_accepted` | | The fixture lacks `fixture.json` or a file for a dataset, or deciding every portfolio it lists, or every one in its holdings, with the default params rejects one. Also fails when decisions report a metric `Metrics` does not declare, or do not report one it declares. |
| `test_its_code_cannot_reach_io_through_its_imports` | | An import chain leads from `model.py` to a source, the loader, the cache, storage, snapshots, outputs, the runs or the Python API, research, the settings, the command line, a database driver or an HTTP client (`FORBIDDEN`). The failure names the chain. |

`check_params`, `check_fixture` and `check_imports` are the checks the contract tests run, and raise `ContractError`,
an `AssertionError`. `basis check`, not a test, checks the repo's agent files.

| Builder | Description |
|---|---|
| `frame(dataset, *rows, default=None) -> DataFrame` | `dataset`'s validated frame from partial rows; a row's missing fields come from `default`, or for holdings and securities from `HOLDING` and `BOND`, and are otherwise null. |
| `data(Data, frames=None, /, *, as_of_date=AS_OF, portfolio_id=None, **more) -> Data` | A model's data from frames by attribute, in a mapping, as keywords, or both; a dataset not given has no rows. |
| `BOND` | Bond `A`, issuer `Issuer A`, rated `AA`, at par, in increments, minimum pieces and minimum remainings of 1,000. |
| `CASH` | Security `USD`, the cash security, valued at 1 a unit, in increments of 0.01. |
| `HOLDING` | P1's $1mm of cash. |
| `AS_OF` | 2026-10-16. |

A model's own `tests/builders.py`, copied from the example, builds its `Portfolio`: `portfolio(*bonds, cash=1_000_000.0,
**datasets)`, as `prepare_portfolio` starts it, before any rule; `params(**changes)`, the default params with changes;
and `MARKET`.

## `optim.research`

| Function | Returns |
|---|---|
| `open_snapshot(run, *, snapshots="snapshots")` (from `optim.runs`) | A `Snapshot`, read with the code you have now: its `info` (the manifest's record), `model`, `params` and `data`. `run` is a run's id in the run store at `snapshots`, a path relative to the working directory or a URL; or the path or URL of its manifest. |
| `trial(snapshot, portfolio_id, params=None)` | A `Trial`, deciding one portfolio in this process: `portfolio_id`, `portfolio` (what `prepare_portfolio` returned), `market` (after the reservations of the portfolios before it), `problem` (the cvxpy problem `optim.cvxpy` solved, or `None`) and `outcome`. |
| `sweep(snapshot, variants, *, workers=1)` | The sweep table, for params files' paths. |
| `regress(model, snapshots, *, last=20, labels=None, workers=1)` | The regression table, for a loaded model and the run store at `snapshots`, a path or URL. |
| `describe(model)` | The `basis describe` JSON, for a loaded model. |

`optim.models.load(name)` loads a model by name or module path.

## The pytest plugin

`optim` registers a pytest plugin, which adds no tests of its own: a model's contract tests are
[`ContractTests`](#optimtesting). It registers the `e2e` marker and these ini options:

| Option | Default | Description |
|---|---|---|
| `optim_qa_markers` | `["qa"]` | Text, matched without case, that every configured source and destination must contain. |
| `optim_e2e_writes` | `[]` | The only snapshot roots, cache roots, orders tables and ledgers the e2e suite may write to, as the environment sets them. The orders database's ledger is checked, for its marker and on this list, only for a run given `--publish`, which writes it. |
| `optim_e2e_command` | this interpreter's `-m optim` | The command that runs the deployed `optim`. |

The session fixture `optim_qa` reads the settings from the process's environment and fails, naming each problem, unless
every configured source and destination carries a marker and every write is on the allowlist. It returns a `QA`:

| Method | Does |
|---|---|
| `listed(model, count=3)` | The first `count` portfolios the model lists in its `portfolios` dataset today, read in a read-only transaction. A model that lists none raises `LookupError`. |
| `portfolios(query, *, source="mssql", **params)` | Runs the `SELECT` or `WITH` query in the SQL file `query`, in a read-only transaction, rolled back, and returns its `portfolio_id`s. |
| `run(model, params, portfolios, *args)` | Runs `basis run MODEL --params PARAMS --portfolios ... --label purpose=e2e` with the command, from the repo's root, and returns the run's id, exit code and snapshot. |
| `replay(run)` | Runs `basis replay RUN_ID --label purpose=e2e` the same way. |
| `smoke(model, params, portfolios, *args)` | Fails unless the run completes, every portfolio is accepted or rejected with reasons, and its replay gives the same trades for the decisions that did not stop at a time limit. |
