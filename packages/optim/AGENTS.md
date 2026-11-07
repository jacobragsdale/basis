# Agent guide

This repo is `optim`, the package that quants' model repos depend on. A model is a folder in a repo's `models/`, whose
`model.py` exports `Params`, `Data` and `decide`, plus optional hooks; `optim` loads its data in the parent process,
decides each portfolio in worker processes, rounds and checks each decision, and saves a snapshot that replays.
[docs/design.md](docs/design.md) is the design it implements, with its deviations, and
[docs/developer-experience.md](docs/developer-experience.md) specifies how a quant works in a model repo; the docs for
quants are in `src/optim/docs/`, and ship with the package.

## Where things are

A run flows top to bottom through this table. Every module opens with a docstring that says what it holds. All paths
are under `src/optim/`.

| Path | Holds |
|---|---|
| `__init__.py` | The model contract's names, and `optim.run`, `optim.replay` and `optim.sweep`, which load `api.py` on first use. |
| `api.py` | `run`, `replay` and `sweep`, as a model's `main.py` calls them: paths from the model's folder, a settings file set for the call alone, snapshots in the repo's `snapshots/`, and no orders. |
| `cli.py` | Every `optim` command, one handler each; `run` exits 0 only when every portfolio was accepted, and is the only thing that writes orders. |
| `settings.py`, `context.py` | `OPTIM_*` environment variables, and the only place sources, the cache and the orders table open. |
| `models.py` | Finding a model by its `optim.models` entry point or module path, and reading its hooks. |
| `contract.py` | `optim.Params` (strict), `optim.Datasets` (typed dataset attributes, `portfolios` among them), `optim.Decision`, `optim.Metrics`. |
| `datasets/` | The shared datasets every model reads, `HOLDINGS` and `SECURITIES`, with their SQL. |
| `data/` | `dataset.py` declares a dataset and `frames.py` validates rows, with no I/O; `loader.py` loads a model's datasets, its portfolios listing first, in batches and read-only transactions, and `cache.py` caches them in files. |
| `sources/` | Oracle (thin or thick), SQL Server, REST and files: bounded fetches, read-only transactions. |
| `storage.py` | The snapshot and cache roots, a path or URL each, through `pyarrow.fs`, with atomic writes. |
| `runs.py` | `run_model`, `replay_run` and `open_snapshot`, which the command line and `api.py` share, and `Run`, a run's result with its tables. |
| `decide.py` | `decide_all`, which calls a model's hooks for every portfolio, at once or in turn when the model reserves; a hook's error rejects its portfolio with a reason that names the model's line. |
| `engine.py` | One piece of work per portfolio, in this process or a `spawn` process pool. |
| `acceptance.py` | Rounding each decision to its securities' increments, and the core's checks of whether it may trade, rounding that moved a trade among them. |
| `cvxpy.py` | `optim.cvxpy`: solving a model's cvxpy problem the same way everywhere, and reading it as a `Decision`. |
| `snapshots.py` | The snapshot: `run.json` (`RunInfo`), each dataset as loaded, and the result tables. |
| `orders.py` | Writing accepted portfolios' orders to the orders table, where the environment configures one. |
| `research.py` | `sweep`, `regress`, `describe`, and `trial`, which decides one portfolio of a snapshot from Python. |
| `testing.py` | What a model repo's tests use: `ContractTests`, which each model's `tests/test_contract.py` subclasses, the import check's `FORBIDDEN`, `check_agent_files`, and builders. |
| `pytest_plugin.py` | The `pytest11` plugin: the e2e suite's `optim_qa` fixture and its safeguards. It injects no tests. |
| `sync.py`, `templates/contract.md`, `skills/` | The agent files `optim sync` writes into a model repo: the contract block in `AGENTS.md` and the `optim-*` skills in `.agents/skills/`. |
| `new.py`, `templates/repo/`, `templates/shared/` | `optim new repo` and `optim new model`: a model repo, or a model in one, from the example and the templates, and `models/shared/` with a repo's second model. |
| `example/` | The example model, laid out as a model's folder in a repo, with its fixture, params and tests, which `optim new` copies into `models/MODEL/`. Its `ruff.toml`, which `optim new` leaves out, sorts imports as a model repo does. |
| `docs/` | The docs for quants: one tutorial, the how-to guides, the reference and the explanation. `optim docs` prints them. |

## Rules to keep

- Model code and workers do no I/O. The parent loads, the workers compute, and the parent saves; replay and snapshots
  depend on it. `lint-imports` enforces it for the core's pure modules and the example's model, and each model's
  contract tests enforce it in model repos, from `model.py`'s imports: `main.py` and the tests are not model code.
- `optim/__init__.py` loads `api.py` only on first use of `optim.run`, `optim.replay` or `optim.sweep`, so model code,
  which imports `optim`, imports no source; the import contract and the contract tests' graph both drop that one edge.
- A run from Python never writes orders; only `optim run` does.
- Snapshots hold each dataset as loaded, never a frame derived from one, so replay reruns the model's hooks.
- Every query takes only the as-of date and, for a dataset read per portfolio, the portfolio ids. Params never change
  what is loaded, so a sweep can run every variant from one snapshot.
- A decision may trade only when it is accepted, never on the solver's status alone.
- Every value is a float; rounding to each security's trading increment makes trades exact.
- `optim` defines no kind of constraint: a model writes its constraints and objective in cvxpy, in its `optimizer/`,
  and takes their limits from its params and data, as the example shows.
- A field added to `RunInfo` gets a default, so snapshots from older versions still load.
- The example, templates and skills are what every new model repo starts from: after changing one, generate a repo
  and run its own checks (see Checks).
- Say "portfolio", never "account", except in SQL Server's own table and column names.
- Document model fields with a docstring under them, not a comment: it becomes the field's description.

## Changing the core

- A shared dataset is a row model and a `Dataset` in `datasets/`, with its SQL in `datasets/sql/`. Add one only when two
  models read it.
- A source is a module in `sources/` with a settings model and a `fetch(query, params)` and `read_only()`, a name in
  `SourceName`, a field in `Settings`, a line in `open_context`, and a line in `qa_problems` in `pytest_plugin.py`, so
  the e2e suite checks it for the QA marker. Add its module, and its driver, to the import contract in `pyproject.toml`
  and to `FORBIDDEN` in `testing.py`. Bound every call with a deadline and `max_rows`.
- A solver is a reader in `optim.cvxpy._READERS` and a name in `SolveSettings.solver`.
- A command is a handler in `cli.py`, registered with `_command`; its Python twin, if it has one, is in `api.py`.
- A docs page is a Markdown file in `docs/`, and a link from the README.

## Checks

```sh
uv run pre-commit run --all-files   # ruff, ty, lockfile, import contracts
uv run pytest                        # the core's tests and the example's
```

To check what `optim new` generates, make a repo in a scratch directory that takes `optim` from this checkout, add a
second model, then run its checks, its model's `main.py` on the fixture, and the command line:

```sh
uv run optim new repo sma SCRATCH_MODEL --dir SCRATCH --optim-path .
cd SCRATCH/sma
uv run optim new model ladder
uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run ty check
cp .env.example .env
uv run python -c 'from optim import run; from models.ladder import model; run(model, data="tests/fixture")'
uv run python models/ladder/main.py   # live data: stops, naming the settings to set
uv run --env-file .env optim run ladder --params models/ladder/params/default.json --data models/ladder/tests/fixture
```

`SCRATCH_MODEL` is the first model's name, such as `income`. `snapshots/` is not in git, and a snapshot written before
a schema change may not load.
