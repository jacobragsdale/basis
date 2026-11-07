# optim

The run loop that a firm's portfolio models share. A model is a folder of plain Python, whose `model.py` declares its
parameters and datasets and decides each portfolio's trades in a pure function; `optim` runs everything else the same
way for every model: loading in the parent process, deciding in parallel worker processes, rounding to trading
increments, acceptance, snapshots that replay, research sweeps, logging and saving orders. Models live in their own
repos, which depend on `optim`, and an analyst runs one by pressing Run on its `main.py`.

## Start a model repo

You need [uv](https://docs.astral.sh/uv/) and git. `INDEX_URL` is the internal package index `optim` is published to.

```sh
uvx --index INDEX_URL optim new repo sma-optimizer income --index INDEX_URL
cd sma-optimizer
uv run pytest
```

The new repo holds one model, `models/income/`, copied from `optim`'s example: its `main.py`, `model.py`, `prep.py`,
`datasets/`, `rules/`, `optimizer/`, params, and tests with a fixture and the model's contract tests. Around it are a
README that maps each kind of change to the file it goes in, a `Dockerfile`, an `azure-pipelines.yml` stub, the
`.env.example` its settings files are copied from, and the agent files that `optim sync` keeps current. The second
`--index` is required: PyPI has an unrelated package named `optim`, which a repo without it would install.
`uv run optim new model ladder` adds a second model, and `models/shared/` for what they share.

## Documentation

The docs ship with the package, so a repo reads the ones for the `optim` it installed: `uv run optim docs` lists them,
and `uv run optim docs PAGE` prints one.

- [Build, replay and sweep your first model](src/optim/docs/tutorial.md): from a new repo to a run, a rule, a
  constraint, a replay and a sweep, on the fixture.
- How-to guides: [run on live data](src/optim/docs/how-to/run-on-live-data.md),
  [add a dataset](src/optim/docs/how-to/add-a-dataset.md), [write a rule](src/optim/docs/how-to/write-a-rule.md),
  [add a constraint](src/optim/docs/how-to/add-a-constraint.md),
  [change the objective](src/optim/docs/how-to/change-the-objective.md),
  [find out why a portfolio was rejected](src/optim/docs/how-to/find-out-why-a-portfolio-was-rejected.md),
  [compare params with a sweep](src/optim/docs/how-to/compare-params-with-a-sweep.md),
  [share code between models](src/optim/docs/how-to/share-code-between-models.md),
  [decide portfolios one at a time](src/optim/docs/how-to/decide-portfolios-in-turn.md),
  [port a legacy model](src/optim/docs/how-to/port-a-legacy-model.md),
  and [run the QA suite](src/optim/docs/how-to/run-the-qa-suite.md).
- [Reference](src/optim/docs/reference.md): the repo's layout, the model contract, the Python API, the command line,
  the settings, the snapshot files, `optim.testing` and the pytest plugin.
- [How optim is put together](src/optim/docs/explanation.md): why the core owns the loop, why model code does no I/O,
  how runs replay, and why constraints are cvxpy.
- [The optim framework](docs/design.md): the design this package implements, and what was decided against, and
  [The developer experience](docs/developer-experience.md): how a quant works in a model repo.

## Develop optim

```sh
uv sync
uv run pre-commit run --all-files   # ruff, ty, the lockfile and the import contract
uv run pytest                        # the core's tests, which also run the example's
```

`uv sync` also installs COPT, which the tests run without a license, size-limited. [AGENTS.md](AGENTS.md) maps every
module and lists the rules to keep. The firm's pipeline builds and publishes the package to the internal index when a
version is tagged; the version is in `pyproject.toml`.
