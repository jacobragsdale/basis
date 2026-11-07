# The developer experience

This page specifies how a quant works in a model repo: where to start, how the repo is organized, how a model runs,
and where each kind of change goes. It was agreed on 2026-10-23 and is implemented in `optim` 0.2.0, which breaks
0.1's model contract and repo layout. [The optim framework](design.md) is the design underneath it; this page replaces
its sections on the repo layout and on running from an IDE.

The reader it is designed for is an analyst who knows Python at a high level. They clone a model repo, or make one,
open it in PyCharm or VS Code, probably with GitHub Copilot beside them, and must be able to see where to start, run
and debug a model, and add a rule, a dataset or a constraint without reading `optim`'s code.

## Principles

- The IDE is the analyst's main tool. Every everyday task starts from a file: run a model by running its `main.py`,
  debug it with a breakpoint, run its tests from the test panel. The command line is for Airflow, Docker, CI and
  agents, and both reach the same functions.
- Nothing is specific to one IDE or one agent vendor: no `.vscode/`, no `.idea/`, no params schema wiring, and the
  agent files are `AGENTS.md` and `.agents/skills/` alone. A repo makes sense in any editor because of where things
  are and what each file says, not because of editor settings.
- One obvious place for each kind of change. Folders are what grow by adding files; a single file becomes a folder of
  the same name when it outgrows one file.
- `optim` owns as little vocabulary as it can. It defines the hooks, datasets and decisions, never the kinds of
  constraint, which a model writes in cvxpy.
- Everything a run uses is written out where the analyst looks: `main.py` names its params, settings file and
  portfolios, and nothing is inferred from where a file sits.

## The repo

```text
sma-optimizer/
  README.md               the start page: run the tests, run a model, then a "To ... edit ..." table
  AGENTS.md               the optim contract, as a block that optim sync writes
  .agents/skills/         the optim-* skills, which optim sync copies
  pyproject.toml          the optim pin, an entry point per model, and pytest, ruff and ty settings
  .env.example            copied to .env, .env.qa and .env.prod, which git ignores
  Dockerfile, .dockerignore, azure-pipelines.yml
  snapshots/              ignored by git: every run from Python saves here; delete it whenever you like
  tests/
    test_agent_files.py   AGENTS.md's block and the skills match the installed optim
    e2e/test_smoke.py     the QA suite, marked e2e, which runs the deployed image against QA
  models/
    __init__.py
    income/               everything about one model
    shared/               only once a second model exists: what two or more models use
```

Every repo's package is `models`, installed in editable mode, so `from models.income import model` works whether a
file runs from PyCharm, VS Code or a terminal. Each model is listed as an entry point,
`income = "models.income.model"`, which is how `optim run income` and a replay find it.

## A model's folder

```text
models/income/
  __init__.py      empty
  README.md        what it does, in business terms: its objective, constraints, data and known limits
  main.py          press Run
  model.py         Params, Data, Metrics, Market, Portfolio, and the hooks
  prep.py          builds the market, and each portfolio's starting universe and limits
  datasets/        README.md; one module per dataset, its row model and Dataset; its SQL in datasets/sql/
  rules/           README.md; one module per group of rules
  optimizer/       README.md; solve.py, problem.py, constraints.py
  params/          default.json, and variants beside it
  tests/           builders.py, test_contract.py, test_*.py, and fixture/: a CSV per dataset and fixture.json
```

`model.py` reads as the model's table of contents: its classes, and hooks of a few lines each that call `prep.py`, the
rules and the optimizer. Workers import `model.py` and what it imports, never `main.py` or `tests/`, so the import
check covers exactly the model's code. Every file opens with a docstring saying what it holds and what you would
change in it, and each folder that grows has a short README saying what goes there and how to add one.

## The hooks

| Hook | Runs | Returns |
|---|---|---|
| `prepare_market(data, params)` | Once per run, optional | The `Market` every portfolio shares. Without it, the market is the run's `Data`. |
| `prepare_portfolio(data, market, params)` | Once per portfolio, optional | The `Portfolio`: `data` is the portfolio's slice. Without it, the portfolio is that slice. |
| `decide(portfolio, market, params)` | Once per portfolio | The `optim.Decision`. |
| `check(portfolio, decision)` | Once per decision, optional | Reasons to reject, beyond the core's. |
| `reserve(market, decision)` | After each accepted decision, optional | The market the next portfolio sees. Defining it makes portfolios decide in turn. |
| `order(portfolios, market, params)` | Once, with `reserve`, optional | The portfolio ids in the order they decide. |

`Market` and `Portfolio` are the model's own frozen dataclasses, in `model.py`. The example's `Portfolio` holds its
`data` (the portfolio's slice of the datasets), its `universe` (a row per security it may hold or trade, with
`buyable`, `sellable` and `must_sell` flags), its `sector_caps`, its `cash`, and its `value`, from its holdings, so a
row a rule drops still counts.

## Rules

Every rule has one signature, `(portfolio, market, params) -> portfolio`, and `prepare_portfolio` applies them in
order, one per line. A rule does anything short of solving: it drops rows of the universe, adds columns, flips flags,
and changes the limits the optimizer reads from the portfolio. A held security dropped from the universe stays held,
and is not traded. How limits from different origins combine is the rules' logic; `optim` prescribes nothing.

Rules are tested with a few rows and no solver. Since rules share one signature, most leave an argument unused, and
the generated `pyproject.toml` lets them (`ARG001` is off for `rules/` and `model.py`).

## Constraints and the objective

A constraint is cvxpy: a function in the model's `optimizer/constraints.py` that takes the trades and returns cvxpy
constraints. `optim` defines no constraint format and no constraint dataset. A constraint's limit comes from one of four
places, and every constraint applies, so the tightest binds:

- **Params**: a field of `Params`, such as `max_issuer_weight` or `sector_caps`, with a docstring and a default.
  Changing one is a JSON edit, and a sweep varies it like any value.
- **A dataset**: the model declares its own row model, such as a client's mandates, and one rule sets the limit on the
  portfolio from its rows, as the example's mandates tighten its `sector_caps`.
- **Code**: what holds whatever the params say: trade sizes and cash.
- **Prep or a rule**: computed from data each run, such as bounds around a benchmark's weights.

Eligibility can be a flag a rule sets or a constraint; a rule may do either. The objective is cvxpy too: `terms` in
`optimizer/problem.py` returns each term's amount, which the decision reports as a metric, and `build` maximizes them,
with their signs. A term's weight, such as `cost_bps`, is a param.

## The optimizer folder

```text
optimizer/
  solve.py         solve(portfolio, params) -> Decision: build the problem, solve it
  problem.py       build(portfolio, params): the variables, the objective's terms, and the list of constraints
  constraints.py   each constraint in cvxpy: trade sizes, cash, turnover and caps on weights
```

`build` makes `buy` and `sell` plain variables, one per row of the universe, and `constraints.py` ties them to whole
increments, minimum pieces, minimum remaining and whole-lot sales, and never buys back what it sells. The example's
other constraints cap turnover and the weight of each bond, each issuer and each sector a cap names, and its objective
is income less cost. `optim.cvxpy` only solves a problem (`solve`) or reads one the model solved itself (`read`).

## What the core accepts

The core rounds each decision to its securities' trading increments, then rejects it unless every trade is in a known
security, every quantity is positive, no sale exceeds its holding, cash after trading is not negative, the decision
was solved, and rounding moved no trade by more than the tolerance, a millionth of the portfolio's value. The solver
proved the decision meets every constraint, within its tolerance, before rounding; the last check proves rounding
changed nothing material, so no constraint is written a second time to check it after rounding. A model that trades
whole increments always passes it. `check` stays, optional, for anything a desk wants beyond the problem.

## Running from Python

`main.py` holds one call, under `if __name__ == "__main__":`, which spawned worker processes need, since each imports
the file that started the run:

```python
"""Decide portfolios with this model: press Run.

To decide a saved run again with the code you have now, call this instead:
    replay(model, "0193f2...", portfolios=["P123"], env=".env.prod")
To compare params files on one saved run:
    sweep(model, "0193f2...", params=["params/default.json", "params/tight.json"])
"""

from optim import run

from models.income import model

if __name__ == "__main__":
    results = run(model, params="params/default.json", portfolios=None, env=".env")
```

- `optim.run(model, *, params, portfolios, as_of, env, data, workers, labels, snapshots, cache)` decides portfolios
  and returns the `Run`, whose `decisions`, `trades`, `reasons` and `metrics` are DataFrames to inspect in the
  debugger. It prints what `optim run` prints.
- Relative paths are the model's folder's, wherever the IDE runs the file from. `env` is found in the model's folder or
  a folder above it, up to the repo's root, where `.env` lives.
- Data is live by default, from the sources the settings file configures. `data="tests/fixture"` reads every dataset
  that has a file there instead, and then `as_of` defaults to the fixture's date. A run whose datasets need a source
  and whose settings file does not exist stops and says to copy `.env.example`.
- `portfolios=None` decides every portfolio the model lists (see below), or every portfolio in the fixture's holdings.
- The settings file's variables are set for the run alone and restored after it, so two calls in one process with
  different settings files do not mix.
- A run from Python saves its snapshot in the repo's `snapshots/`, whichever settings file it reads, and never writes
  orders. `snapshots=` saves elsewhere.
- `optim.replay(model, run, ...)` reads the run from the repo's `snapshots/` or the settings file's
  `OPTIM_SNAPSHOT_ROOT`, such as production's in `.env.prod`, or from a path, decides it again with the code you have
  now, and saves the replay in `snapshots/`. `optim.sweep(model, run, params=[...])` returns the comparison table.
- `workers` defaults to `OPTIM_WORKERS`, or 1, which decides in this process, where a breakpoint stops.
- A hook's error rejects only its portfolio, and its reason names the line of model code that raised, as in
  `KeyError: 'sector', at models/income/rules/screens.py:14 in exclude_sectors`; the log holds the full traceback.

## Production

`optim run MODEL --params FILE [--portfolios ...] [--as-of DATE] [--data DIR]` reads the process's environment, saves
the snapshot under `OPTIM_SNAPSHOT_ROOT`, which it requires, and writes accepted portfolios' orders where
`OPTIM_ORDERS__*` configures a table. It is the only thing that writes orders. Its paths are the working directory's,
as any command's are. It and `optim.run` share everything from loading to saving.

## Settings files

`.env`, `.env.qa` and `.env.prod` sit at the repo's root, and git ignores all of them but `.env.example`. `main.py`
picks one with `env=`. `OPTIM_SNAPSHOT_ROOT` is where `optim run` saves and where a replay looks for a run id; a run
from Python does not need it. A dataset's source that a settings file does not configure fails its load, naming the
variables to set. No run ever falls back from a source to the fixture.

## The portfolios dataset

A model may give its `Data` a `portfolios` attribute, declared like any dataset, whose query takes only the as-of date
and whose rows are the portfolios the model manages, one each, with any columns the model wants. `portfolios=None`, or
`optim run` without `--portfolios`, then decides every portfolio it lists. Each portfolio's hooks see only its own row,
as with any per-portfolio dataset, and `prepare_market` sees every row. A portfolio given by id that the dataset does
not list is rejected as not one of the model's portfolios on that date. The fixture holds `portfolios.csv`. A model
without the dataset decides the portfolios it is given, or every portfolio in a holdings file.

## Tests

- `models/income/tests/test_contract.py` holds `class TestContract(optim.testing.ContractTests)` with `model` set to
  the model's module. Its tests check that the default params validate, that every portfolio of the fixture is
  accepted and reports exactly the metrics `Metrics` declares, and that the model's code cannot reach I/O through its
  imports. They appear and run in the IDE's test panel like any test; `optim` injects no tests.
- `tests/test_agent_files.py` checks that `AGENTS.md`'s block and the skills match the installed `optim`.
- `models/income/tests/builders.py` builds what a model's tests need in a line: `portfolio(*rows, **datasets)`, the
  portfolio a run would prepare from a few securities, before its rules; `MARKET`; and `params(**changes)`,
  `default.json` with changes. `optim.testing` keeps builders for what `optim` owns: `frame`, `data`, and the default
  bond, cash and holding rows.
- The QA suite, `tests/e2e/`, decides the first three portfolios each model lists, through the deployed image, and
  refuses to run unless every source and destination is QA's.

## Shared code

`optim new model` creates `models/shared/`, with a README, when it adds a repo's second model. It is laid out like a
model's folder, without `model.py`, `main.py` or `params/`. Models import from it, as in
`models.shared.rules.controls`, and never from each other. Code moves there when a second model needs it, and into
`optim.datasets` when a second repo does. Each model's fixture still has a file for every dataset it reads. A repo with
one model keeps everything in the model's folder.

## How a repo explains itself

- The repo's `README.md` is the start page: run the tests, which need no credentials, then copy `.env.example` and run
  a model's `main.py`, then a table that maps each task to the files to edit and the guide (`optim docs PAGE`).
- The `AGENTS.md` block holds the contract (the hooks, the rules to keep, the commands) and points at the README's
  table, so there is one copy of where each change goes.
- The skills cover the long procedures: start a model, port a legacy optimizer, run a research sweep, triage an e2e
  failure, and explain why a portfolio was rejected. Each points at its how-to guide, so the steps exist once.
- The docs that ship with `optim`: a tutorial that runs on the fixture, how-to guides (run on live data, add a dataset,
  write a rule, add a constraint, change the objective, find out why a portfolio was rejected, compare params with a
  sweep, share code between models, decide portfolios in turn, port a legacy model, and run the QA suite), the
  reference, and the explanation.

## Making a repo

`optim new repo REPO MODEL (--index URL | --optim-path PATH)` makes `REPO/` with `models/MODEL/` copied from the one
example, runs `git init` and `uv sync`, and leaves the first commit to the quant. `optim new model NAME`, in a repo, adds
`models/NAME/`, its entry point and its README row, and `models/shared/` with the second model. The example is a
taxable SMA rebalance with four datasets (`portfolios`, with a client's own rating floor; `signals`, one table for
every portfolio; `controls` and `mandates`, per portfolio), four rule modules (controls, credit, screens, and mandates,
which tightens a portfolio's sector caps with its mandates), the optimizer folder above, and default params that cap
each security, each issuer and turnover. The mandates cap sectors in some portfolios, so limits arrive from both params
and a dataset.

## What 0.2.0 removes

`optim.ide` and each model's `__main__.py`; the contract tests the pytest plugin injected; the `plain` example and
`--from`; `CLAUDE.md` and `.claude/skills/`; `src/`, and the separate `params/MODEL/` and `tests/MODEL/` trees; the
hook names `prepare`, `shared` and `prepared`; the example's `lines`, which is now `universe`; the example's `check`
and its pandas copy of every limit; `qa.env`, which is now `.env.qa`; and, for a model that lists its portfolios, the QA
suite's `portfolios.sql`.

## Decided against

| Option | Why not |
|---|---|
| A `.vscode/` folder, PyCharm notes, a params JSON schema, Copilot's `.github/` files | Vendor-specific; the layout and the docstrings must carry the repo in any editor. |
| `optim.ide.run(__file__)`, which inferred params, fixture and repo from the file's place | Invisible to the analyst; `main.py` writes every value out instead. |
| A second file per model for replays | The docstring of `main.py` shows the other calls where the analyst looks first. |
| Trade sizing in the core | Quants own and change their solver code; sizing is visible code in the model. |
| Constraint checks in `optim`, or a limit's constraint paired with a pandas check | The rounding check makes a second copy of each constraint unnecessary. |
| Raising a hook's error in a run from Python, so the debugger stops there | Debugging that way is uncommon; a reason that names the line, and the logged traceback, are enough. |
| Pruning old snapshots automatically | `snapshots/` is safe to delete by hand, and deletion stays the analyst's choice. |
| `models/shared/` in every new repo | An empty folder raises a question before there is an answer. |
