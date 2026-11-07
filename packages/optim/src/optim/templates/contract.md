## The optim contract

Written by `optim sync` from optim {version}. Edit outside this block; `uv run optim sync` rewrites it, and
`tests/test_agent_files.py` fails when it does not match the installed optim.

This repo holds models that run on `optim`. `optim` loads each model's data in the parent process, decides each
portfolio in worker processes, rounds each decision to its securities' trading increments, checks it, saves the run as
a snapshot that replays, and, in production alone, writes accepted orders. A model holds only what is particular to it.
The README's "To change something, edit this" table says which file each kind of change goes in.

### What a model is

A model is a folder in `models/`, and its `model.py` is listed in `pyproject.toml` under
`[project.entry-points."optim.models"]`. `model.py` exports:

| Name | Runs | What it is for |
|---|---|---|
| `Params(optim.Params)` | Validated once per run | The model's parameters. Strict: unknown keys and coercion are refused. |
| `Data(optim.Datasets)` | Loaded once per run | The datasets it reads, as typed attributes; `holdings` and `securities` are always there, and `portfolios`, if declared, lists the portfolios it manages. |
| `decide(portfolio, market, params)` | Once per portfolio | Returns the portfolio's `optim.Decision`. |
| `prepare_market(data, params)` | Once per run, optional | What every portfolio shares. Without it, `market` is the run's `Data`. |
| `prepare_portfolio(data, market, params)` | Once per portfolio, optional | Builds the portfolio from its slice of the data, then applies the rules in order. Without it, `portfolio` is that slice. |
| `reserve(market, decision)` | After each accepted decision, optional | Makes portfolios decide one at a time; returns the market the next one sees. |
| `order(portfolios, market, params)` | Once, with `reserve`, optional | The portfolio ids in the order they decide. |
| `check(portfolio, decision)` | Once per decision, optional | Reasons to reject, beyond the core's checks. |
| `Metrics(optim.Metrics)` | Optional | The numbers decisions report in `metrics`, for sweeps. |

Every rule has one signature, `(portfolio, market, params) -> portfolio`: it may drop rows of the universe, add columns,
set flags, and change the limits the optimizer reads from the portfolio, but never solves. Constraints are cvxpy, one
function each in `optimizer/constraints.py`, which `build` in `optimizer/problem.py` adds to the problem with the
objective; their limits are params, or fields a rule sets on the portfolio. A cvxpy problem goes through
`optim.cvxpy.solve(problem, params.solve, securities=..., buy=..., sell=...)`, which returns the `Decision`; a model with
no solver returns `optim.Decision(buy={...}, sell={...})`.

### Rules to keep

- Model code does no I/O: no files, databases or HTTP. Data arrives in `Data`; `tests/test_contract.py` fails on an
  import of `model.py`'s that can reach a source, the loader, the cache, outputs, a database driver or an HTTP client.
  `main.py` and the tests are not model code: workers never import them.
- Every query takes only the as-of date and, for a dataset read per portfolio, the portfolio ids. Params never change
  what is loaded: select rows after loading instead, so a sweep can run every variant from one snapshot.
- Rules are plain functions in `rules/`, tested on a few rows without a solver, with a docstring that states the rule
  in business terms.
- Quantities are positive floats in the holdings' units. The core rounds them to each security's increment and rejects
  a decision that rounding moves, that sells more than it holds, trades an unknown security or overdraws cash, or that
  was not solved. Trade in whole increments, and no constraint needs checking again after rounding.
- Code two models use goes in `models/shared/`; a model never imports another model.
- Log with `logging.getLogger(__name__)`, never `print`. Document every field with a docstring under it, and open every
  file with a docstring that says what it holds.
- Keep each model's `README.md` true: what it maximizes, its constraints, its data and its known limits.
- Never write orders from Python: a run from `main.py` cannot, and only `optim run` in production does.

### Where things are

| Path | Holds |
|---|---|
| `models/MODEL/main.py` | Press Run: one `optim.run(...)` call, whose docstring shows `replay` and `sweep`. Paths are the model's folder's. |
| `models/MODEL/model.py` | `Params`, `Data`, `Metrics`, `Market`, `Portfolio`, and the hooks. |
| `models/MODEL/prep.py` | Joins the datasets into the market, and each portfolio's universe before its rules. |
| `models/MODEL/datasets/` | One module per dataset: its row model and `Dataset`; its SQL in `sql/`. |
| `models/MODEL/rules/` | One module per group of rules. |
| `models/MODEL/optimizer/` | `solve.py`; `problem.py`, which builds the problem and its objective; and `constraints.py`. |
| `models/MODEL/params/` | `default.json`, and variants beside it. |
| `models/MODEL/tests/` | `test_contract.py`, the behaviour tests, `builders.py`, and `fixture/`: a CSV per dataset and `fixture.json`. |
| `models/shared/` | What two or more models use, laid out like a model's folder. |
| `tests/` | `test_agent_files.py`, and `e2e/`, the QA suite, marked `e2e`, which runs only against QA. |
| `.env`, `.env.qa`, `.env.prod` | The settings files, copied from `.env.example`, which git ignores. |
| `snapshots/` | Every run from Python, which git ignores. |

### Commands

```sh
uv run python models/MODEL/main.py               # what pressing Run does: decide portfolios, print a row for each
uv run pytest                                    # behaviour and contract tests
uv run ruff check . && uv run ty check           # lint and types
uv run optim describe MODEL                      # params schema, datasets and metrics, as JSON
uv run --env-file .env optim run MODEL --params models/MODEL/params/default.json --data models/MODEL/tests/fixture
uv run --env-file .env optim replay RUN_ID [--params FILE]   # decide a saved run again with this code
uv run --env-file .env optim sweep RUN_ID DIR    # one table: a row per params file in DIR and portfolio
uv run optim new model NAME                      # add a model, and models/shared/ with the second
uv run optim sync                                # rewrite this block and the optim-* skills
uv run optim docs [PAGE]                         # optim's own docs: tutorial, how-to, reference, explanation
```

The `optim-*` skills in `.agents/skills/` cover starting a new model, porting a legacy optimizer, running a research
sweep, finding out why a portfolio was rejected, and triaging an e2e failure; each points at a page of `optim docs`.
