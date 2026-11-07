# The optim framework

This repository is `optim`, a Python package that quants' model repos depend on. A model is one module that declares
its parameters and datasets and decides each portfolio's trades in a pure function. `optim` runs everything else the
same way for every model: loading, parallelism, rounding, acceptance, snapshots, replay, logging and saving orders. The
design was agreed on 2026-10-22 and implemented in `optim` 0.1.0, by the steps in
[Getting there from this repository](#getting-there-from-this-repository). `optim` 0.2.0 reworked how a quant works in a
model repo, as [The developer experience](developer-experience.md) specifies: one folder per model, a `main.py` to run,
and renamed hooks. The taxable SMA optimizer this repository held was a mock that existed to test
the engine; it is the package's example. The docs for quants are in `src/optim/docs/` and ship with the package; this
page records the design and its reasons.

## Where the implementation differs

The implementation follows this page, with these differences and additions, each the smallest change that kept the
design's intent:

| Where | What the code does | Why |
|---|---|---|
| `Data` | `optim.Datasets` itself declares `holdings` and `securities`; a model's `Data` lists only its own datasets, and may replace either shared one with a dataset of the same row model. A dataset declared as `portfolios`, whose query takes only the as-of date, lists the portfolios the model manages: a run given none decides every one, each portfolio sees its own row, and one it does not list is rejected. | Every model must read both, so the core declaring them removes a touch point and a way to forget one. Most runs are live, and the model, not the scheduler, knows which portfolios it manages. |
| Hooks | `prepare` is `prepare_market(data, params) -> market`, `prepare_portfolio(data, market, params) -> portfolio`, `decide(portfolio, market, params)` and `check(portfolio, decision)`; "shared" and "prepared" are gone. | Names an analyst reads as what they hold. |
| `reserve`, `order` | `reserve(market, decision)` returns the market the next portfolio sees. `order(portfolios, market, params)` takes every prepared portfolio by id and returns the ids in order; one that drops a portfolio fails the run. | The page left `order`'s arguments open. Returning the state keeps `reserve` pure, and the prepared portfolios are what a fairness rule ranks. |
| Rules and constraints | Every rule is `(portfolio, market, params) -> portfolio`, in the model's `rules/`, and may drop rows, add columns, and change the limits the optimizer reads from the portfolio. Constraints and the objective are cvxpy, in the model's `optimizer/`: `constraints.py` holds each constraint, trade sizes among them, and `problem.py` builds the problem and its objective. Their limits come from params, datasets, rules and code. | A constraint in cvxpy needs no format of its own for `optim` to own. Trade sizing stays visible in the model, for quants to change. |
| `Metrics` | A model may export `Metrics`, a model of float fields, so `optim describe` can print its metrics; a contract test fails when decisions report metrics it does not declare. | `describe` must print a model's metrics without running it. |
| Repo layout | Each model is a folder, `models/MODEL/`, holding everything about it: `main.py`, `model.py`, `prep.py`, `datasets/`, `rules/`, `optimizer/`, `params/` and `tests/`, its fixture `tests/fixture/` with one file per dataset and `fixture.json`. Every repo's package is `models`; code two models share is in `models/shared/`, which `optim new model` adds with the second model. | One folder per model means one place for each change, and the `models` package imports the same way from any editor. |
| `optim run --data DIR` | Reads each dataset that has a file in `DIR` instead of its source, as of the date in `DIR/fixture.json` unless `--as-of` says otherwise. Without `--portfolios`, a run decides every portfolio the model lists, or with `--data`, every portfolio in the holdings file. | It is how a laptop and the contract tests run on files without editing `Data`, and a fixture's rows are of its date. |
| Caching | A dataset's cache is `cache=timedelta(...)`; `optim warm MODEL ...` fills every cached dataset read whole. Warm hooks, `live_on_miss` and `optim clear` are gone. | Every query takes only the as-of date, so warming needs no parameters, and an entry past its age is read live. |
| Holdings | The shared holdings are working holdings; `--holdings sod` is gone. | Queries take only the as-of date and the ids. A model that needs start-of-day holdings declares its own dataset with the same row model. |
| Sources | Every source is optional, so a run on files needs only `OPTIM_SNAPSHOT_ROOT`. The REST source is `rest`, `OPTIM_REST__URL`. | A laptop on files must not need database settings. |
| Rounding | To the nearest whole increment; a sale within half an increment of the holding sells the holding, so an odd lot sells whole. The core rejects a decision that rounding moved by more than its tolerance, and a portfolio with no holdings, as a likely wrong id. | Nearest rounding keeps a solver's near-integers exact; the holding rule keeps odd lots tradable. The solver proved the decision meets its constraints, so a decision rounding left alone meets them too, and no model checks its constraints a second time. |
| Solved | A decision is solved when the solver stopped `optimal`, within `SolveSettings.mip_gap`; one stopped at its time limit is rejected. There is no separate acceptance gap. | The page names one MIP gap in `Params`; one number means one rule. |
| Replay | `optim replay` saves its snapshot under `OPTIM_SNAPSHOT_ROOT`, and a replay from Python in the repo's `snapshots/`, wherever the run it replays is. `run.json` keeps the run's portfolios, and `replay.portfolios` the subset decided. | Replaying a production snapshot on a laptop must write nothing to production storage, and a replay of a replay of a subset must decide the same sequence. |
| `optim regress` | Takes `--snapshots ROOT`, `--last N` and `--label KEY=VALUE`, such as `purpose=production`, and exits 1 when anything changed. | Production runs are found by their labels, and a non-zero exit lets a pipeline gate an upgrade on it. |
| `optim sweep` | Writes the table as CSV on stdout, or to `--out`. | Agents read CSV; a file suits a person. |
| `optim docs` | Prints a page of the docs that ship with the installed `optim`, which the skills point at. | A model repo's agents read the docs for the version the repo installed, without a copy in each repo. |
| Python API | `optim.run(model, ...)`, `optim.replay(model, run, ...)` and `optim.sweep(model, run, params=...)`, which a model's `main.py` calls, share `optim.runs` with the command line. Their paths are the model's folder's, their settings file is set for the call alone, they save in the repo's `snapshots/`, and they never write orders. `optim.runs.open_snapshot(root)` and `optim.research.trial(snapshot, portfolio_id, params=None)` return what a portfolio was prepared with, the market, the cvxpy problem and the outcome. | Analysts run and debug models from their IDEs by running a file, which must spell out what it runs; production keeps one `optim run` for every repo. |
| Contract tests | Each model's `tests/test_contract.py` subclasses `optim.testing.ContractTests`: its params, its fixture and its imports, from `model.py`'s. The repo's `tests/test_agent_files.py` checks the agent files. The plugin injects no tests. | Tests an analyst can see and run from the IDE's test panel, like any other. |
| e2e | The plugin's `optim_qa` fixture checks the markers (`optim_qa_markers`) and the allowlist (`optim_e2e_writes`), picks each model's first listed portfolios (`QA.listed`), or portfolios with a `SELECT` alone, and runs `optim_e2e_command` from the repo's root with repo-relative params paths, such as `docker run ... IMAGE`; `QA.smoke` is each model's smoke case, in the generated `tests/e2e/test_smoke.py`. | The page named the safeguards; these are where they live. Relative paths resolve both in a checkout and in the image, whose workdir holds the repo, and the image holds no pytest. |
| Orders | Written as `run_id, portfolio_id, security_id, side, quantity`, through `OPTIM_ORDERS__CONNECTION_STRING`, a login that may insert, into `OPTIM_ORDERS__TABLE`. | The read-only credentials that load data must not write. The real table's columns are still to confirm. |
| `optim.testing` | `ContractTests` and `check_agent_files`, and builders for behaviour tests: `frame(dataset, *rows)` and `data(Data, frames, **more)`, with default shared rows. Each model's own `tests/builders.py` builds its portfolio and params. | The page names the core's builders. |
| Example | One example, a taxable SMA rebalance laid out as a model's folder, with a `ruff.toml` that sorts imports as a model repo does and that `optim new` leaves out. The `plain` example is gone. | Copied code must pass the generated repo's own ruff; one example means one variant of the layout to keep. |
| `optim new repo` | `optim new repo REPO MODEL` needs `--index`, or `--optim-path`, a checkout, editable, and writes that source for `optim` into the repo's `pyproject.toml`; `--from` is gone. | PyPI has an unrelated package named `optim`, which a repo with no source would install. A repo and its first model are named apart. |
| Agent files | `optim sync` writes `AGENTS.md`'s block and `.agents/skills/` alone. | The open conventions every agent reads, Copilot's included; no vendor's own files. |
| Image | The generated `Dockerfile` runs `optim` under `tini`, the container's first process. | The first process ignores a signal's default action, so `optim` run as the first process would log the scheduler's SIGTERM and carry on, writing orders, until it was killed. |

What is not done here, because it needs what this repository cannot reach: a run of the tests on Windows, publishing by
tag, and the real pipeline templates in the `azure-pipelines.yml` stub.

## The problem it solves

Quants at a fixed income research firm write optimizers for SMA products (many small portfolios) and for institutional
portfolios (fewer, larger, and with more constraints). There are hundreds of them, in many flavours. Most solve linear
or mixed-integer programs in cvxpy, and a few decide in plain Python with no solver. They share one shape: load holdings
and other data, apply each portfolio's constraints and rules, optimize an objective, and save the orders. Each repeats
that shape in its own code, with I/O anywhere, no types, no replay and inconsistent logs, so they are hard to scale to
many portfolios, to describe and to test. One engineer owns all of them in production.

`optim` takes the shape out of each model and keeps it in one place, and a quant writes only what is particular to the
model. The guarantees come from owning the run loop. Data loads only in the parent process, so model code does no I/O,
every run can be replayed from its snapshot, and every model logs, scales and is tested the same way. The same
properties let agents drive research: an agent can rerun a saved run with a hundred variants of its parameters without
touching production data.

## A package, not a template or a monorepo

Each model lives in its own repo, or in one repo with a few related models, such as a production rebalance and the
model-portfolio runs that share most of its logic. Quants work independently, and a monorepo of hundreds of models
would be too large. A template repo would put a copy of the engine in each of hundreds of repos, where the copies drift
apart, which is the problem this replaces. A skill or a guide alone gives agents the conventions but no guarantee at
run time, and a quant writing code by hand never reads it.

So `optim` is one package, which the firm's pipeline publishes to the internal PyPI server when a version is tagged,
and each model repo depends on a version range. Everything that must stay the same across repos ships in the package
and upgrades with it: the run loop, the shared datasets, the contract tests and the no-I/O check, the e2e safeguards,
the examples the generator copies, and the agent guidance that `optim sync` writes into each repo. A model repo holds
only the quant's work.

The package stays at 0.x until one real SMA model and one real institutional model run on it in production, because
the contract will change during those first ports, and a 0.x minor release may break it. From 1.0 it follows semver,
with a deprecation warning in a minor release before any breaking change. Repos upgrade when their owners re-lock.
Every `run.json`, and every run's first log line, records the `optim` version it ran with, so the production snapshots
show which model runs which version. Automated bump PRs, such as Renovate run as a scheduled Azure pipeline, need no
design change and can come later if old versions pile up.

## What a model is

A model is a module that exports `Params`, `Data` and `decide`, plus any of five optional hooks. This is the target API;
names may change while it is built:

```python
import optim
from optim.datasets import HOLDINGS, SECURITIES

from ladder.datasets import SIGNALS


class Params(optim.Params):
    max_weight: float = 0.05
    """The most of the portfolio's value in any one security."""


class Data(optim.Datasets):
    holdings = HOLDINGS
    securities = SECURITIES
    signals = SIGNALS


def decide(prepared: Data, shared: Data, params: Params) -> optim.Decision:
    # Build a cvxpy problem and solve it through optim.cvxpy, or decide in plain Python.
    ...
```

| Name | Runs | What it is for |
|---|---|---|
| `Params` | Validated once per run | The model's parameters, in any shape the quant wants. |
| `Data` | Loaded once per run | The datasets the model reads, as typed attributes. |
| `decide(prepared, shared, params)` | Once per portfolio | Returns the portfolio's `Decision`. Runs in parallel unless the model defines `reserve`. |
| `prepare(data, params)` | Once per run | Work every portfolio shares, such as joining market data. Without it, `shared` is the run's `Data`. |
| `prepare_portfolio(portfolio, shared, params)` | Once per portfolio, always in parallel | The model's rules. Without it, `prepared` is the portfolio's slice of `Data`. |
| `reserve(shared, decision)` | After each accepted decision | Defining it makes portfolios decide one at a time, each seeing the shared state the earlier ones left. |
| `order(...)` | Once, when `reserve` is defined | The order portfolios decide in. Without it, the run's portfolio order. |
| `check(prepared, decision)` | Once per decision | The model's own acceptance checks, returning the reasons to reject. |

`Params` subclasses a strict base class that rejects unknown keys and does not coerce types, so a misspelled key in a
variant an agent wrote fails before any data loads instead of being ignored. A run takes one params JSON file from any
path, including the separate templates repo. The run validates it before loading and stores it in the snapshot with its
path, hash and git commit. Constraints that differ by portfolio come from a dataset or from inside that JSON, keyed by
portfolio, as the model chooses. The framework fixes only the model, the as-of date and the portfolios.

Each dataset in `Data` is a typed attribute, so `prepared.holdings` is a DataFrame that ty checks and editors complete;
a string key would be a typo away from a `KeyError` in the morning run. Datasets read per portfolio are sliced to the
portfolio before `prepare_portfolio`, and the others arrive whole.

`decide` is one pure function for every kind of model. A cvxpy problem goes through the core's helper, `optim.cvxpy`.
The helper solves with the solver, time limit and MIP gap from `Params`, passes `warm_start=False` (the default made
HiGHS re-solves about six times slower), reads the solver's status, records its statistics, and returns a `Decision`.
The framework does not call `solve()` itself, so `decide` stays one function whether a model solves one problem, several
or none. A model with no solver builds its `Decision` directly. A `Decision` holds the trades by security as float
quantities, and a report: whether the problem solved, the objective value, solver statistics, and any metrics the model
wants in sweeps.

The rules a model applies per portfolio are much of each model's particular logic: narrowing the buy universe, or
adjusting constraints from current holdings. They are plain functions in the model's `rules.py`, tested without a
solver and named in its README. The framework has no rule registry. `prepare_portfolio` is a separate step from
`decide` so that rules still run in parallel when decisions cannot.

Decisions run one portfolio at a time when portfolios compete for a limited buy universe, such as a new issue or a
scarce bond. Only accepted decisions reserve, so a rejected portfolio leaves its supply to the next one. The order is
recorded in `run.json`, and replaying one portfolio of such a run reruns the portfolios before it. Who gets a scarce
bond is a client-fairness decision, so the order is always explicit. Allocating the supply up front in `prepare`, pro
rata or by rotation, is the other way to handle competition, and it keeps `decide` parallel. Each solve covers one
portfolio; the framework has no joint solves across portfolios.

## One run

Every run, in Airflow or on a laptop, takes the same steps:

1. Validate the params file against the model's `Params`.
2. Create the run's id, a UUIDv7. Airflow's DAG run and try number arrive as labels.
3. Load the model's `Data` in the parent process. Every query takes only the as-of date and the portfolio ids.
4. Run `prepare` once, then `prepare_portfolio` for each portfolio in worker processes.
5. Run `decide` for each portfolio: in parallel, or one at a time in `order` when the model defines `reserve`.
6. Round each decision's quantities to each security's trading increment, from the shared securities dataset.
7. Check each rounded decision for acceptance, as described after these steps.
8. Write the snapshot.
9. Write the accepted portfolios' orders, with the run id, to the SQL Server orders table.

Acceptance rejects a decision unless every trade is in a known security, every quantity is positive, no sale exceeds
the holding, and cash after the trades is not negative. One tolerance applies, in weights of the portfolio's value. The
model's `check` then adds its own reasons, because the core cannot see the constraints a model builds in cvxpy. A
solver's status alone never makes a result acceptable. A rejected portfolio writes no orders, its decision and reasons
stay in the snapshot, and the run exits non-zero; the exit code is 0 only when every portfolio was accepted.

Only production writes orders. A run writes them to the table that its environment configures, and only the
production environment configures one, so a laptop's orders stay in its snapshot. `optim` never submits orders.

Models log with the standard library's `logging.getLogger(__name__)`. The core's handler adds the run id, the model and
the portfolio id to every record, worker processes included, and writes JSON lines. Ruff rejects `print` in model code.

## Data

A dataset is declared once: its row schema, source, query, key and cache policy. Shared datasets, which two or more
models read, live in `optim.datasets`, and the package's owner reviews them. A dataset only one model reads lives in
that model's repo and is declared the same way; it moves into `optim.datasets` when a second model needs it. A model
that needs an extra column on a shared dataset declares a private dataset with the same key and joins it in `prepare`,
rather than forking the shared one. Every model reads the shared holdings dataset, because acceptance checks sales
against it, and the shared securities dataset, which holds trading increments.

Queries take only the as-of date and the portfolio ids, and params never change what is loaded. A choice that depends on
params, such as which benchmark to track, selects rows after loading. That rule is what makes sweeps possible: if a
variant's params could change a query, the snapshot would not hold the data the variant needs.

The sources are Oracle, SQL Server and REST, read in read-only transactions, plus a `file` source that reads Parquet or
CSV by path. Swapping one entry of `Data` for a file-backed dataset with the same row schema covers what-ifs, portfolios
that are not in the books, small tables that no system holds, and test data. Laptops use Oracle's thin mode, which
needs no Oracle client libraries.

Values are floats everywhere, booked values included. With exact `Decimal` columns, a quant's `holdings.par * 0.5`
raises `TypeError`, and arithmetic on object columns is slow. Rounding to each security's trading increment is what
makes trade quantities exact.

The cache stores each cached dataset as a Parquet file under `OPTIM_CACHE_ROOT`, with the time its source was read, and
writes through the same `pyarrow.fs` layer as snapshots. Each dataset declares how long an entry stays fresh, and
`optim warm` fills the cache before the day's first run. A write goes to a temporary file followed by a rename, so a
concurrent run never reads half an entry. Without `OPTIM_CACHE_ROOT`, nothing is cached. A laptop points the setting at
a local folder, and production at shared storage, so Airflow tasks share entries. Redis would be infrastructure in
every environment, bounded by memory and awkward on Windows laptops. The cache sits behind a small interface, so
another backend can be added if files prove too slow for some dataset.

## Where runs happen

Airflow runs `optim run MODEL --params FILE --as-of DATE [--portfolios ...]` in the model repo's image. A repo lists
its models as entry points, and `MODEL` is one of their names:

```toml
[project.entry-points."optim.models"]
real-optim = "ladder.real_optim"
model-portfolio = "ladder.model_portfolio"
```

Two models that share most of their logic are two small modules that import shared ones, such as `ladder/common.py`,
and differ only in `Params`, `Data` or a single function.

Quants run the same command on their laptops, which are mostly Windows, often against production data because the QA
data is often poor. A laptop run uses the quant's own read-only credentials from `.env`, writes its snapshot to local
disk, and writes nothing to a shared system. Settings come only from `OPTIM_*` environment variables: the source hosts
and credentials, the snapshot root, the cache root and the orders table are the same for every model, so no model repo
holds a connection string, and moving storage is one deployment change.

Worker processes start with `spawn` on every platform. `forkserver`, today's start method, does not exist on Windows,
and one start method means laptops behave like production. The shared state goes to each worker once, through the pool
initializer, and each worker imports numpy, pandas and cvxpy itself, which adds a second or two to a run. The package
requires Python 3.14 and is set up with uv only. Every dependency had a Windows wheel for Python 3.14, or was pure
Python, when checked on 2026-10-22.

## Snapshots, replay and research

A snapshot holds the run's request and params, each dataset as loaded, and the results, and it is written before any
orders. `OPTIM_SNAPSHOT_ROOT` is a path or URL read and written through `pyarrow.fs`. It is a file path to start with,
whether local disk, NFS or Azure Files, and moving to Azure Data Lake changes only the URL. Snowflake is a database, not
a file store, so it does not hold snapshots. Production snapshots go to shared storage, so that quants and agents can
replay them by run id, and they are kept as long as trade records.

`optim replay RUN [--params FILE] [--portfolios ...]` solves a saved run again with the current code, and other
params if given. `optim sweep RUN VARIANTS_DIR` solves the same snapshot once per params file and writes one comparison
table, with a row per variant and portfolio: objective, turnover, number of trades, cash, whether it was accepted,
solve time, and the model's own metrics. A sweep reads only the snapshot, so an agent can run a hundred variants
without loading anything from production. From Python, a quant or an agent can load a snapshot and decide one
portfolio, getting back its prepared inputs, the cvxpy problem if there is one, and the decision. `optim describe MODEL`
prints the model's params JSON schema, datasets and metrics as JSON, so an agent can drive research without reading
the model's code.

## Tests

Testing has four layers, and a CI pipeline runs them on Linux and Windows. The generated `azure-pipelines.yml` is a
stub for the firm's Azure DevOps pipeline framework.

1. Contract tests come from a pytest plugin that registers itself when `optim` is installed. For every model the repo
   lists, they check that the default params validate, that `decide` runs on the model's fixture and passes acceptance,
   that the model's modules cannot reach a source, the loader, the cache, outputs, a database driver or an HTTP client
   through their imports, and that the agent files match the installed version. No repo carries import-linter
   configuration that could drift.
2. Behaviour tests are the quant's own, on small inputs built with the core's builders, especially for the rules in
   `prepare_portfolio`.
3. `optim regress MODEL` replays the model's recent production snapshots with the working tree's code and installed
   `optim`, and lists every trade that changed. It is how an upgrade or a model change is proven, and it needs read
   access to the snapshot root. Production snapshots never go in git, because they hold client holdings.
4. An e2e pytest suite in each repo runs the deployed image's `optim run` against the QA environment. It refuses to run
   unless every configured source and destination carries the QA marker, writes only to an allowlist (the QA orders
   table and snapshot root), and picks test portfolios by what they hold rather than by fixed ids. Every model gets a
   smoke case: the run completes, each portfolio is accepted or rejected with reasons, and replaying the snapshot gives
   the same trades for solves that did not stop at a time limit. The safeguards come from the `e2e-harness` template.

## Agents

`optim sync` writes the core's one-page contract into a marked block in the repo's `AGENTS.md`, whose `CLAUDE.md` is
`@AGENTS.md`, and copies the core's skills into `.agents/skills/optim-*`, which Codex reads, and
`.claude/skills/optim-*`, which Claude Code reads. They are copies rather than symlinks because Git on Windows checks a
symlink out as a plain text file unless Developer Mode and `core.symlinks` are on. A contract test fails when the copies
do not match the installed `optim`. The skills cover starting a new model, porting a legacy optimizer (keeping a
`PORTING.md` log, following [how to port a legacy model](../src/optim/docs/how-to/port-a-legacy-model.md)), running a
research sweep, and triaging an e2e failure.

Each model's `README.md` states its behaviour in business terms: what it maximises, its constraints, its data and its
known limits. The machine-readable surfaces are `optim describe`, the JSON logs and the sweep tables.

## A model repo

`optim new repo NAME [--from cvxpy|plain]` creates a model repo, and `optim new model NAME [--from ...]` adds a model to
the repo it runs in. Before any repo exists, a quant runs `uvx --index INDEX_URL optim new repo NAME`, where `INDEX_URL`
is the internal PyPI server. A new repo gets:

- `pyproject.toml`, with the `optim` version range, the model's entry point, and ruff and ty settings
- the package, with one model copied from the chosen example, `params/default.json`, the model's `README.md` and one
  behaviour test
- a `Dockerfile`: Python 3.14 slim with uv, `uv sync --frozen --no-dev`, and `optim` as the entry point
- the `azure-pipelines.yml` stub, `.gitignore` (snapshots, cache, `.env`) and `.env.example` (the `OPTIM_*` settings)
- `AGENTS.md`, `CLAUDE.md` and the skills, written by `optim sync`

The generator runs `git init` and `uv sync`, and leaves the first commit to the quant. A new model gets its module,
params, README and test, and its entry-point line. The templates are the installed package's examples, so a generated
repo matches the `optim` version that made it.

## Docs for quants

The package's docs are one tutorial that takes a quant from nothing to a working model, replayed and swept, in about
fifteen minutes; how-to guides (add a dataset, write a rule, decide portfolios one at a time, port a legacy model, run
in QA); one reference page for the model contract, the CLI and the settings; and one explanation page,
[How optim is put together](../src/optim/docs/explanation.md), adapted from the engine's architecture page. The skills
point at these pages instead of repeating them, so agents and quants read the same text.

## Decided against

| Option | Why not |
|---|---|
| One monorepo for every model | Hundreds of models, and quants work independently. |
| A template repo with the engine copied in | Hundreds of copies drift apart. |
| The framework calling `solve()` | `decide` stays one function whether a model solves once, several times or not at all; `optim.cvxpy` standardizes solving instead. |
| Joint solves across portfolios | One solve per portfolio; portfolios that compete decide in turn with `reserve`. |
| Exact `Decimal` values | Arithmetic on them fails or is slow; rounding to increments makes trades exact. |
| Redis as the cache | Infrastructure in every environment, and awkward on Windows laptops. |
| Submitting orders to an OMS | Orders are saved, and released elsewhere. |
| The `hypothetical` job | Rarely needed; a dataset can be swapped for a file instead. |
| Throwaway models in a scratch folder | Not needed. |
| A runs table | `run.json` is the record of a run, and the core creates its id. |
| Several params files layered per run | One JSON file per run is enough; templates are passed in as that file. |
| Params that change a query | A sweep could no longer run from the snapshot. |
| Renovate from the start | Nothing depends on it, so it can come later. |
| Symlinked skills | Git on Windows checks symlinks out as text files by default. |
| A strategy JSON format in the framework | Each model's `Params` is its own; today's strategy JSON becomes the example's `Params`. |

## Getting there from this repository

Each step was a change that left the checks passing, and every step is done, but for step 4's run on Windows and step
11's tag:

1. Remove what the design drops: the `hypothetical` job (`jobs/hypothetical.py`) and the runs table (`new_run_id` in
   `context.py`, and `--run-id`). The core creates UUIDv7 run ids.
2. Make every value a float: the `Decimal` columns in `data/frames.py`, the sources, the mock's datasets, rounding and
   acceptance. Drop the exact-`Decimal` rule from `AGENTS.md`.
3. Move storage behind `pyarrow.fs`: `OPTIM_SNAPSHOT_ROOT`, and a file cache under `OPTIM_CACHE_ROOT` behind a cache
   interface. Remove Redis (`data/cache.py`, the dependency and its settings), and add the `file` source.
4. Start workers with `spawn` in `engine.py`, and run the tests on Windows.
5. Build the model contract: `optim.Params`, `optim.Datasets`, `optim.Decision`, `optim.cvxpy`, and a run loop that
   calls the hooks and finds models by entry point, with `optim run MODEL --params FILE`. Move the mock (`prep/`,
   `rules/`, `optimizer/model.py`, the constraints and objective terms, `strategy.py` and `strategies/`) into the
   `cvxpy` example, whose `Params` replaces the strategy JSON. Holdings and securities become `optim.datasets`, and the
   other datasets become the example's own.
6. Split rounding and acceptance (`optimizer/rounding.py`, `optimizer/acceptance.py`) into the core's checks and the
   example's `check`.
7. Add `reserve` and `order`.
8. Add the research commands: `replay --params`, `sweep`, `describe`, `regress`, and the Python API for one portfolio.
9. Add the pytest plugin: contract tests, the import check for model repos, the agent-files check, and the e2e
   fixtures. The core keeps its own `lint-imports` contract.
10. Add the generator and `optim sync`: the repo templates, the skills, and the `plain` example.
11. Write the docs for quants and rewrite `AGENTS.md`, the README and the other pages in `docs/` for the package. Set
    the version to 0.1.0 and publish it by tag.
