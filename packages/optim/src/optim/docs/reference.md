# Reference

The repo layout, the model contract, the Python API, the command line, the settings, the snapshot files,
`optim.testing` and the pytest plugin of `optim` 0.2.0.

## The repo

`optim new repo REPO MODEL` makes this layout. Every repo's package is `models`, at the repo's root, installed in
editable mode, so `from models.MODEL import model` works whichever folder a file runs from.

| Path | Holds |
|---|---|
| `README.md` | The start page: a table of models, how to start, and which file each kind of change goes in. |
| `AGENTS.md` | The optim contract, as a block between `<!-- optim:begin -->` and `<!-- optim:end -->` that `optim sync` writes. |
| `.agents/skills/optim-*/` | The skills `optim sync` copies. |
| `pyproject.toml` | The `optim` version range and its source, an entry point per model, and the pytest, ruff and ty settings. |
| `.env.example` | The settings, commented; copied to `.env`, `.env.qa` and `.env.prod`, which git ignores. |
| `Dockerfile`, `.dockerignore`, `azure-pipelines.yml` | The image Airflow runs, and a stub of the pipeline that tests, builds and runs the QA suite. |
| `snapshots/` | Every run from Python, which git ignores. Safe to delete at any time. |
| `tests/test_agent_files.py` | Fails when the agent files do not match the installed `optim`. |
| `tests/e2e/test_smoke.py` | The QA suite, marked `e2e`. |
| `models/MODEL/` | One model. |
| `models/shared/` | What two or more models use, laid out like a model's folder; `optim new model` adds it with the second model. |

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
`optim describe` prints. A params file is one JSON object; a field it leaves out takes its default.

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
Dataset(name, row, source, query, key, cache=None, check=None)
```

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Names its frame, its fixture file and its snapshot file. |
| `row` | `type[optim.Row]` | The row model. Field names are column names; each field's annotation sets its dtype and holds its rules. A row model with a `@field_validator` or `@model_validator` is refused. |
| `source` | `"oracle"`, `"mssql"`, `"rest"` or `"file"` | Where rows come from. |
| `query` | `str \| Path` | A SQL file for `oracle` and `mssql`, a URL path for `rest`, a Parquet or CSV file's path or URL for `file`. `{as_of_date}` in a file's path becomes the run's date. |
| `key` | `tuple[str, ...]` | Columns whose values no two rows share; a duplicate fails the load. |
| `cache` | `timedelta \| None` | How long a cache entry stays fresh; `None` reads the dataset live on every load. |
| `check` | `Callable[[DataFrame], None] \| None` | Rules across rows, which raise `ValueError` on rows that disagree. |

`per_portfolio` is true when `row` has a `portfolio_id` field. `file(path)` returns the same dataset read from a file,
uncached.

Every query binds the business date as `as_of_date` (`:as_of_date` for Oracle, `%(as_of_date)s` for SQL Server, a query
parameter for REST). A per-portfolio dataset other than `portfolios` also binds `portfolios`, a JSON array of up to
1,000 ids per query, which SQL Server unpacks with `OPENJSON(%(portfolios)s) WITH (id VARCHAR(64) '$')` and Oracle with
`JSON_TABLE`. Oracle refuses a bind its SQL does not use. A file source filters a per-portfolio file to the batch's
`portfolio_id`s; a CSV's values are read as text and validated into the row model's types, and an empty field is null.
Field types for rows are in `optim.schema`: `Id`, `Cusip`, `Isin`, `Currency`, `Price`, `Quantity`, `Weight` and
`Rating`, an ordered category best first. A `Literal` field becomes an ordered category in its declared order.

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
| `stop` | `optimal` (proved optimal within the gap asked), `limit` (stopped by a limit, usually the time), `imprecise`, `infeasible`, `unbounded` or `failed`. |
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
| `options` | `dict` | `{}` | Passed to the solver by its own option names; they override the fields above and COPT's one thread. |

`solve(problem, settings, *, securities, buy=None, sell=None, metrics=None) -> Decision` solves `problem` with
`warm_start=False`, then reads it. `read(problem, *, securities, buy=None, sell=None, metrics=None) -> Decision` reads a
problem the model solved itself. `buy` and `sell` are cvxpy expressions in the holdings'
units, one entry per id in `securities`. `metrics` maps names to expressions or numbers, evaluated at the solution. A
trade smaller than 1e-6 is dropped. The decision is solved only when the solver stopped `optimal`; without a solution
it has no trades. A problem with no variables is solved with no trades, without calling a solver.

## Rounding and acceptance

After `decide`, the core rounds each quantity to the nearest whole number of its security's `increment`, sells the whole
holding where a sale is within half an increment of it, and drops a quantity that rounds to zero. It then rejects the
rounded decision, with these reasons, in this order, when:

| Reason | When |
|---|---|
| `it has no holdings: is its id right?` | The portfolio has no rows in `holdings`. |
| `not solved: STOP` | `solved` is false. |
| `trades securities that are not loaded: [...]` | A trade's security is not in `securities`. |
| `buys quantities that are not positive: [...]`, `sells ...` | A quantity is zero or negative. |
| `sells Q of S, more than its holding of H` | A sale exceeds the holding by more than the tolerance. |
| `rounding moved its SIDE of S from X to Y: decide whole increments` | Rounding changed a trade's value by more than the tolerance. |
| `leaves cash of C: it spends more than it has` | Cash holdings plus sales less buys, at unit values, is below minus the tolerance. |

The tolerance is 1e-6 of the portfolio's value, its holdings at unit values: $1 per $1mm. The rounding check is why no
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

They run as `optim run`, `optim replay` and `optim sweep` do, through the same code, with these differences:

| | From Python | From the command line |
|---|---|---|
| Relative paths | The model's folder's, wherever the process runs | The working directory's |
| Settings | The settings file `env` names, set in the process for the call alone and restored after it, over the process's environment | The process's environment |
| Snapshots saved | In the repo's `snapshots/`: the folder beside the nearest `pyproject.toml` above the model's, or `snapshots/` in the working directory without one | Under `OPTIM_SNAPSHOT_ROOT`, which it requires |
| Orders | Never written | Written where `OPTIM_ORDERS__*` configures a table |
| Logs | Text, unless `OPTIM_LOG_FORMAT` says otherwise | JSON lines, unless it says otherwise |
| Errors | Raise, with their traceback | Logged once, and exit 1 |

`env` is found in the model's folder or the nearest folder above it that has it, up to the repo's root. A run whose
datasets need a source, and whose settings file is not found, stops: `there is no .env in FOLDER or a folder above it:
copy .env.example to .env ...`. With `env=None`, a call reads the process's environment alone.

### `optim.run`

```python
run(model, *, params="params/default.json", portfolios=None, as_of=None, env=".env", data=None, workers=None, labels={}, snapshots=None, cache=True) -> Run
```

Decides portfolios with the model, saves the run, prints what `optim run` prints, and returns the `Run`.

| Argument | Default | Description |
|---|---|---|
| `params` | `"params/default.json"` | The params file. |
| `portfolios` | every portfolio the model lists | Portfolio ids. See [the portfolios dataset](#the-portfolios-dataset); a model that lists none decides every portfolio in a holdings file read from `data`. |
| `as_of` | the fixture's date with `data`, or today in New York | A `date`, or `YYYY-MM-DD`. The fixture's date is `fixture.json`'s, where `data` has one. |
| `env` | `".env"` | The settings file, which says where each dataset's source is; `None` reads the process's environment alone. |
| `data` | none | A folder whose files, `NAME.csv` or `NAME.parquet`, replace those datasets' sources, such as `"tests/fixture"`; every other dataset is read from its source. |
| `workers` | `OPTIM_WORKERS`, or 1 | Processes that decide; 1 decides in this process, where a breakpoint stops. |
| `labels` | none | Recorded in `run.json`, as `--label` records them. |
| `snapshots` | the repo's `snapshots/` | Where to save the run: a path, relative to the model's folder, or a URL. |
| `cache` | on | Reads cached datasets from `OPTIM_CACHE_ROOT`, where it is set; off reads every dataset live. |

### `optim.replay`

```python
replay(model, run, *, params=None, portfolios=None, env=".env", workers=None, labels={}, snapshots=None) -> Run
```

Decides a saved run of the model again, with the code you have now and other params if given, saves it as a new run,
prints what `optim replay` prints, and returns it. `run` is a run's id, found in the repo's `snapshots/`, then under the
settings file's `OPTIM_SNAPSHOT_ROOT`, such as production's in `.env.prod`; or a snapshot's path, relative to the model's
folder, or URL. A run of another model is refused. `params` defaults to the params the run used. `portfolios` decides
those alone; for a model that reserves, the portfolios before them in its order decide again too. It reads no source
and writes no orders.

### `optim.sweep`

```python
sweep(model, run, *, params, env=".env", workers=None, snapshots=None) -> DataFrame
```

Decides a saved run of the model once per params file in `params`, each relative to the model's folder, every file
validated before any decides, saves nothing, and returns the [sweep table](#optim-sweep). `run` is found as `replay`
finds it. `workers` defaults to `OPTIM_WORKERS`, or 1.

### `Run`

| Attribute | Description |
|---|---|
| `run_id` | The run's id, which names its snapshot. |
| `ok` | Every portfolio was accepted. |
| `decisions` | A row per portfolio, as `decisions.parquet`. |
| `trades` | A row per trade, as `trades.parquet`. |
| `reasons` | A row per reason a portfolio was rejected, as `reasons.parquet`. |
| `metrics` | A row per portfolio and metric, as `metrics.parquet`. |
| `outcomes` | Each portfolio's `Outcome`: `portfolio_id`, `decision` (rounded), `decided` (as the model decided it), `reasons`, `numbers` (`value`, `cash`, `turnover`), `seconds` and `accepted`. |
| `summary()` | What `optim run` prints: `run RUN_ID`, a row per portfolio, and each reason. |

## The command line

Every command reads its settings from the process's environment, as `uv run --env-file .env optim ...` sets them, logs
JSON lines on stderr, and exits 1 on an error, logged once with its root cause. Paths are the working directory's.

### `optim run`

```text
optim run MODEL --params FILE [--as-of DATE] [--portfolios ID ...] [--data DIR] [--label KEY=VALUE] [--workers N] [--no-cache]
```

Validates the params, loads the model's data, decides each portfolio, saves the snapshot under `OPTIM_SNAPSHOT_ROOT`,
which it requires, and writes accepted orders where `OPTIM_ORDERS__*` configures a table. It is the only thing that
writes orders. Prints `run RUN_ID`, a row per portfolio, and each reason. Exits 0 only when every portfolio was
accepted.

| Option | Default | Description |
|---|---|---|
| `--params` | required | The params JSON file. |
| `--as-of` | the fixture's date with `--data`, or today in New York | The business date, `YYYY-MM-DD`. The fixture's date is `fixture.json`'s, where `--data`'s folder has one, as for `optim.run`. |
| `--portfolios` | every portfolio the model lists, or with `--data`, every one in its holdings | Portfolio ids, or `@FILE` with one per line. |
| `--data` | none | Reads each dataset that has a file in the directory, `NAME.parquet` or `NAME.csv`, instead of its source. |
| `--label` | none | `KEY=VALUE`, recorded in `run.json`; repeat for more. |
| `--workers` | `OPTIM_WORKERS`, or 1 | Worker processes. |
| `--no-cache` | off | Reads every dataset live. |

### `optim replay`

```text
optim replay RUN [--params FILE] [--portfolios ID ...] [--label KEY=VALUE] [--workers N]
```

Decides a saved run again with the code installed now, and other params if given, and saves it as a new run under
`OPTIM_SNAPSHOT_ROOT`. `RUN` is a run id under `OPTIM_SNAPSHOT_ROOT`, or a snapshot's path or URL. Reads no source and
writes no orders. A replay of a subset decides that subset; for a model that reserves, the portfolios before them in
its order decide again too. A replay of a replay of a subset replays that subset. Exit status as `run`.

### `optim sweep`

```text
optim sweep RUN VARIANTS [--out FILE] [--workers N]
```

Decides the run once per `*.json` params file in the directory `VARIANTS`, every file validated first, and writes one
table, as CSV on stdout or to `--out` as `.csv` or `.parquet`. Reads only the snapshot and saves none. `--workers`
defaults to 1. Columns: `variant` (the file's stem), `portfolio_id`, `accepted`, `objective`, `turnover`, `trades`,
`cash`, `solve_seconds`, `seconds`, one column per metric, and `reasons`, joined with `; `. Exits 0.

### `optim regress`

```text
optim regress MODEL [--snapshots ROOT] [--last N] [--label KEY=VALUE] [--out FILE] [--workers N]
```

Replays the model's `--last` (default 20) most recent runs under `--snapshots` (default `OPTIM_SNAPSHOT_ROOT`), replays
aside and only those with every `--label` given, with each run's own params, and writes a row per change: `run_id`,
`portfolio_id`, `status_before`, `status_after`, `limit` (either decision stopped at a time limit), and for a changed
trade `security_id`, `side`, `before` and `after`, the rounded quantities. Exits 1 if anything changed.

### `optim describe`

```text
optim describe MODEL
```

Prints JSON: `model`, `module`, `optim_version`, `hooks` (the optional hooks it defines), `params` (the JSON schema),
`datasets` (by attribute: `name`, `source`, `per_portfolio`, which is false for `portfolios`, `key`, `cache_seconds`,
`columns`) and `metrics` (the JSON schema, or null).

### `optim new repo` and `optim new model`

```text
optim new repo NAME MODEL (--index URL | --optim-path PATH) [--dir DIR] [--no-sync]
optim new model NAME [--no-sync]
```

`new repo` makes `DIR/NAME` (default here), which must be empty or not exist: the templates, the agent files, and one
model, `models/MODEL/`, copied from optim's example, then runs `git init` and `uv sync`, and leaves the first commit to
you. The repo depends on `optim>=0.2,<0.3`, from the source its `pyproject.toml` names: `--index`, or `UV_INDEX`, the
internal index, or `--optim-path`, a checkout of `optim`, editable, to try a change to it. It refuses to make a repo
with neither, or both: PyPI has an unrelated package named `optim`, which a repo with no source would install.

`new model` adds `models/NAME/`, copied from the example, to the repo in the working directory, with its entry point
and a row in the `README.md`'s table of models, and adds `models/shared/`, with a `README.md`, when it is the repo's
second model; then it runs `uv sync`. A name that is taken, or cannot name a Python module, is refused. `--no-sync`
leaves `uv sync` out.

### `optim sync`, `optim docs` and `optim warm`

`optim sync` writes the contract block into the working directory's `AGENTS.md`, keeping what is written around it, and
copies the `optim-*` skills into `.agents/skills/`, deleting copies of skills `optim` no longer ships; it prints each
path it wrote. `optim docs [PAGE]` prints a page of these docs, such as `how-to/add-a-dataset`, or lists them.
`optim warm MODEL ... [--as-of DATE]` reads every cached dataset the models read whole, the `portfolios` dataset
included, live, and overwrites its cache entry; it needs `OPTIM_CACHE_ROOT` and exits 1 if any load failed.

## Settings

Settings come only from `OPTIM_*` environment variables: on the command line, the process's; from Python, the settings
file `env` names, over the process's. Nested fields take a double underscore, as in `OPTIM_ORACLE__DSN`. A source that
is not set cannot be read: a dataset that reads from it fails its load, naming the variables to set.

| Variable | Default | Description |
|---|---|---|
| `OPTIM_SNAPSHOT_ROOT` | none | Where `optim run` saves snapshots, and where a replay finds a run by its id: a path, or a URL `pyarrow.fs` opens, such as `abfs://...`. Required by `optim run`, `optim replay`, and `optim sweep` and `optim regress` given a run id; a run from Python saves in the repo's `snapshots/` instead. |
| `OPTIM_CACHE_ROOT` | none | The dataset cache, a path or URL; without it nothing is cached. |
| `OPTIM_WORKERS` | `1` | Worker processes per run, at least 1; `1` decides in the calling process. |
| `OPTIM_ORACLE__DSN`, `__USER`, `__PASSWORD`, `__MODE` | required with Oracle | `MODE` is `thin` (no client libraries) or `thick`, which loads Oracle Instant Client. |
| `OPTIM_ORACLE__CLIENT_LIB_DIR` | none | Thick mode: where Oracle Instant Client is, if not on the library path. |
| `OPTIM_MSSQL__CONNECTION_STRING` | required with SQL Server | The primary, not a readable secondary: holdings must include the trades just booked. The database must allow snapshot isolation. |
| `OPTIM_REST__URL` | required with REST | The base URL of a JSON API that answers a GET with a list of records. |
| `OPTIM_<SOURCE>__MAX_CONCURRENT`, `__TIMEOUT_S`, `__MAX_ROWS` | required with the source | Calls open at once, seconds per call, and the most rows a call may return; each at least 1. `TIMEOUT_S` bounds every query, round trip or request, and a database's login too, which never waits more than 30 seconds. `<SOURCE>` is `ORACLE`, `MSSQL` or `REST`. |
| `OPTIM_ORDERS__CONNECTION_STRING`, `__TABLE` | none | Production only: a login that may insert, and the table accepted orders are inserted into, as `run_id, portfolio_id, security_id, side, quantity`. `TABLE` is `schema.table`. Only `optim run` writes to it. |
| `OPTIM_ORDERS__TIMEOUT_S` | `60` | Seconds for the orders write. |
| `OPTIM_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`, for `optim`'s and models' loggers. |
| `OPTIM_LOG_FORMAT` | `json`; `text` from Python | `json` lines, or `text` for a person. |
| `OPTIM_GIT_SHA`, `OPTIM_IMAGE` | none | Set in an image: the commit and image a run records. |

## Snapshots

`ROOT/RUN_ID/` appears only once it is complete, and nothing changes it afterwards. `ROOT` is `OPTIM_SNAPSHOT_ROOT` for
`optim run` and `optim replay`, and the repo's `snapshots/` for a run from Python.

```text
RUN_ID/
  run.json
  inputs/<dataset>.parquet   each dataset as loaded, validated on read by the model's row models
  decisions.parquet          a row per portfolio
  trades.parquet             a row per trade
  reasons.parquet            a row per reason a decision was rejected
  metrics.parquet            a row per portfolio and metric
```

`run.json`:

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
| `datasets` | Per dataset, its `source`, `read` (`live`, `hit`, `miss` or `refresh`) and `fetched_at`; null for a replay. |
| `order` | For a model that reserves, the order its portfolios decided in; otherwise null. |
| `replay` | For a replay, `of`, the run it replayed, and `portfolios`, the subset it decided; otherwise null. |

`decisions.parquet` columns: `portfolio_id`, `status` (`accepted` or `rejected`), `trades`, `value` (before trading),
`cash` (after trading), `turnover`, `solved`, `objective`, `solver`, `solver_status`, `stop`, `gap`, `nodes`,
`iterations`, `solve_seconds` and `seconds`. `trades.parquet`: `portfolio_id`, `security_id`, `side`, `quantity`
(rounded; 0 where rounding left nothing) and `decided` (as the model decided it). `reasons.parquet`: `portfolio_id`,
`reason`. `metrics.parquet`: `portfolio_id`, `metric`, `value`.

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

`check_agent_files(repo)` fails when the repo's `AGENTS.md` block or its `optim-*` skills do not match what `optim sync`
writes; the repo's `tests/test_agent_files.py` calls it. `check_params`, `check_fixture` and `check_imports` are the
checks the contract tests run, and raise `ContractError`, an `AssertionError`.

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
| `open_snapshot(root)` (from `optim.runs`) | A `Snapshot`: its `root`, `info` (`run.json`), `model`, `params` and `data`, read with the code you have now. |
| `trial(snapshot, portfolio_id, params=None)` | A `Trial`, deciding one portfolio in this process: `portfolio_id`, `portfolio` (what `prepare_portfolio` returned), `market` (after the reservations of the portfolios before it), `problem` (the cvxpy problem `optim.cvxpy` solved, or `None`) and `outcome`. |
| `sweep(snapshot, variants, *, workers=1)` | The sweep table, for params files' paths. |
| `regress(model, snapshots, *, last=20, labels=None, workers=1)` | The regression table, for a loaded model and a `Root`. |
| `describe(model)` | The `optim describe` JSON, for a loaded model. |

`optim.storage.Root.at(location)` opens a path or URL as a `Root`, and `optim.models.load(name)` loads a model by name or
module path.

## The pytest plugin

`optim` registers a pytest plugin, which adds no tests of its own: a model's contract tests are
[`ContractTests`](#optimtesting). It registers the `e2e` marker and these ini options:

| Option | Default | Description |
|---|---|---|
| `optim_qa_markers` | `["qa"]` | Text, matched without case, that every configured source and destination must contain. |
| `optim_e2e_writes` | `[]` | The only snapshot roots, cache roots and orders tables the e2e suite may write to, as the environment sets them. |
| `optim_e2e_command` | this interpreter's `-m optim` | The command that runs the deployed `optim`. |

The session fixture `optim_qa` reads the settings from the process's environment and fails, naming each problem, unless
every configured source and destination carries a marker and every write is on the allowlist. It returns a `QA`:

| Method | Does |
|---|---|
| `listed(model, count=3)` | The first `count` portfolios the model lists in its `portfolios` dataset today, read in a read-only transaction. A model that lists none raises `LookupError`. |
| `portfolios(query, *, source="mssql", **params)` | Runs the `SELECT` or `WITH` query in the SQL file `query`, in a read-only transaction, rolled back, and returns its `portfolio_id`s. |
| `run(model, params, portfolios, *args)` | Runs `optim run MODEL --params PARAMS --portfolios ... --label purpose=e2e` with the command, from the repo's root, and returns the run's id, exit code and snapshot. |
| `replay(run)` | Runs `optim replay RUN_ID --label purpose=e2e` the same way. |
| `smoke(model, params, portfolios, *args)` | Fails unless the run completes, every portfolio is accepted or rejected with reasons, and its replay gives the same trades for the decisions that did not stop at a time limit. |
