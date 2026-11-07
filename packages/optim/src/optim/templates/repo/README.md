# {{repo}}

Portfolio models that run on `optim`, which loads their data, decides each portfolio, checks each decision, and saves
every run as a snapshot that replays. Each model is a folder in `models/`, and its own `README.md` says what it does in
business terms.

| Model | Folder |
|---|---|
| `{{model}}` | `models/{{module}}/` |

## Start here

1. Install [uv](https://docs.astral.sh/uv/), which installs Python on first use, then install the repo and run its
   tests, which need no database:

   ```sh
   uv sync
   uv run pytest
   ```

2. Open the repo in your editor, with `.venv` as its Python interpreter.
3. Copy `.env.example` to `.env`, and set the sources your machine reads: it says which variables each source needs.
4. Open `models/{{module}}/main.py` and press Run. It decides portfolios with live data and prints a row for each.
   Add `data="tests/fixture"` to its `run(...)` to run on the model's fixture instead, with no database.

Each run from `main.py` saves its snapshot in `snapshots/`, which git ignores; delete it whenever you like. A run from
`main.py` never writes orders: only `optim run`, in production, does.

## To change something, edit this

Paths are in a model's folder, `models/MODEL/`. `uv run optim docs PAGE` prints each guide.

| To | Edit | Guide |
|---|---|---|
| change a param | `params/default.json`; a new param is a field of `Params` in `model.py` | `reference` |
| add a dataset | a module in `datasets/`, an attribute of `Data` in `model.py`, and a CSV in `tests/fixture/` | `how-to/add-a-dataset` |
| join data, or add a column every rule reads | `prep.py` | `how-to/add-a-dataset` |
| add a rule | a function in `rules/`, called in `prepare_portfolio` in `model.py` | `how-to/write-a-rule` |
| add a constraint | cvxpy in `optimizer/constraints.py`, its limit a param, or a dataset and a rule | `how-to/add-a-constraint` |
| change the objective | `terms` and `build` in `optimizer/problem.py`; a term's weight in the params | `how-to/change-the-objective` |
| run on QA or production data | `env=".env.qa"` or `env=".env.prod"` in `main.py` | `how-to/run-on-live-data` |
| find out why a portfolio was rejected | `replay(...)` in `main.py`, as its docstring shows | `how-to/find-out-why-a-portfolio-was-rejected` |
| compare params | `sweep(...)` in `main.py`, as its docstring shows | `how-to/compare-params-with-a-sweep` |
| add a model | `uv run optim new model NAME` | `how-to/share-code-between-models` |
| share code between models | `models/shared/`, which the second model brings | `how-to/share-code-between-models` |

`uv run optim docs` lists every guide, and `uv run optim docs tutorial` is the place to start. `AGENTS.md` holds the
contract every model keeps, for people and agents alike.
