---
name: optim-new-model
description: Start a new optim model in this repo, from optim's example, through to passing contract tests. Use when asked to add, create or scaffold a model, optimizer or strategy that runs on optim.
---

# Start a new optim model

1. Read the tutorial: `uv run optim docs tutorial`. It builds a model from a new repo to a run, a rule and a
   constraint.
2. Add the model: `uv run optim new model NAME`. It writes `models/NAME/`, copied from optim's example, with its
   `main.py`, `model.py`, `prep.py`, `datasets/`, `rules/`, `optimizer/`, `params/default.json` and `tests/`, a fixture
   included, and lists it in `pyproject.toml` and the README. With the repo's second model, it adds `models/shared/`.
3. Change it from the inside out: `Params` and `params/default.json`; `Data`, one module per dataset in `datasets/`
   (`uv run optim docs how-to/add-a-dataset`); the rules in `rules/` (`uv run optim docs how-to/write-a-rule`); the
   constraints and the objective (`uv run optim docs how-to/add-a-constraint`); then the optimizer.
4. Keep the fixture, the default params, the folder READMEs and the model's `README.md` true as you go, and write a
   behaviour test per rule, on a few rows built with `tests/builders.py`. To see a change work, run the model on its
   fixture: `run(model, data="tests/fixture")` in its `main.py`, or `uv run python models/NAME/main.py`.
5. Done when `uv run pytest`, `uv run ruff check .` and `uv run ty check` pass: the model's `tests/test_contract.py`
   decides every portfolio of the fixture and refuses any import that reaches I/O.

The contract is in this repo's `AGENTS.md`; `uv run optim docs reference` holds every hook, setting and command.
