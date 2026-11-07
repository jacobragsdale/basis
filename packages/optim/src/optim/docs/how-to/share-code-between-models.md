# How to share code between models

Use one copy of the code two or more of a repo's models need, such as a dataset of trading controls, the rule that
applies them, or trade sizing. It goes in `models/shared/`, which is laid out like a model's folder, without
`model.py`, `main.py` or `params/`, and the models import from it.

Prerequisites:

- A repo with two models. `uv run optim new model NAME` adds the second, copied from optim's example, with its entry
  point and its row in the README, and creates `models/shared/` with a README.

Move code there when a second model needs it, not before: a repo with one model keeps everything in the model's
folder. When a second repo needs a dataset, it belongs in `optim.datasets` instead, which the package's owner reviews.

To share a dataset:

1. Move its module and its SQL from the model's `datasets/` to `models/shared/datasets/`, with `git mv`, and add an
   empty `__init__.py` there:

   ```sh
   mkdir -p models/shared/datasets/sql
   touch models/shared/datasets/__init__.py
   git mv models/income/datasets/controls.py models/shared/datasets/controls.py
   git mv models/income/datasets/sql/controls.sql models/shared/datasets/sql/controls.sql
   ```

   Delete the other model's copy, if it has one. The module finds its SQL beside it, as
   `SQL = Path(__file__).parent / "sql"`, so it needs no change.
2. Import it from there in each model's `model.py`, and in their tests:

   ```python
   from models.shared.datasets.controls import CONTROLS
   ```

3. Keep the dataset's file in each model's fixture, `tests/fixture/controls.csv`: each model's contract test loads every
   dataset it reads from its own fixture.

To share a rule:

1. Write the shared logic in `models/shared/rules/`, with an empty `__init__.py`, as a function of frames and values,
   not of a model's `Portfolio`: each model has its own `Portfolio`, and shared code imports no model.

   ```python
   """A portfolio's trading controls, which bind whatever the params say: what every model of the repo applies the same
   way. Each model's own rule calls `apply` with its portfolio's universe and controls."""

   import numpy as np
   import pandas as pd


   def apply(universe: pd.DataFrame, controls: pd.DataFrame) -> pd.DataFrame:
       """The universe with `no_buy`, `no_sell`, `hold` and `must_sell` applied to its `buyable`, `sellable` and
       `must_sell` flags."""
       ...
   ```

2. Keep each model's rule, with the one signature, as a line that calls it:

   ```python
   """The portfolio's trading controls, which bind whatever the params say, as every model of the repo applies them."""

   from dataclasses import replace

   from models.income import model
   from models.shared.rules import controls


   def apply(portfolio: model.Portfolio, market: model.Market, params: model.Params) -> model.Portfolio:
       """`no_buy`, `no_sell`, `hold` and `must_sell`, from the portfolio's controls: see `models.shared.rules.controls`."""
       return replace(portfolio, universe=controls.apply(portfolio.universe, portfolio.data.controls))
   ```

   `prepare_portfolio` and each model's rule tests then stay as they were.
3. Test the shared function where it lives, in `models/shared/tests/`, or through each model's rule tests, which still
   pass the same rows through it.

Optimizer code moves the same way. Trade sizing, `sizes` in `optimizer/constraints.py`, takes the universe and the
cvxpy variables, not a model's classes, so it moves whole, to `models/shared/optimizer/sizes.py`, and each model's
`solve.py` calls `sizes.sizes(universe, built.buy, built.sell, value=value)`. A function that takes a model's
`Portfolio`, such as `cash`, takes the frames and values it reads instead, or stays with the model.

A model never imports another model: what two share goes in `models/shared/`, so either can change or be deleted on its
own.

To confirm it worked, run `uv run pytest`: both models' contract tests decide their fixtures, and their import checks
follow the models' imports into `models/shared/`, so shared code that reaches I/O fails both:

```text
models.income.model -> models.income.rules.controls -> models.shared.rules.controls -> httpx
```

Then run `uv run ruff check .` and `uv run ty check`, which find an import still pointing at the old place.
