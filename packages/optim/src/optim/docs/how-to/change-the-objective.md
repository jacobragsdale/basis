# How to change the objective

Change what a model maximizes, for every run or for one params file: weigh trading costs differently, add a term such
as a penalty above a soft cap, or drop one. The objective is cvxpy, in the model's `optimizer/problem.py`: `terms`
returns each term's amount, which the decision reports as a metric, and `build` maximizes them, with their signs, as a
fraction of the portfolio's value. A term's weights, such as the example's `cost_bps`, are params.

Prerequisites:

- A model made by `optim new`, with its tests passing.

To weigh a term differently for one params file, such as a variant a sweep compares:

1. Copy `params/default.json` whole, such as to `params/costly.json`, and change the term's param in the copy, such as
   a trading cost of 15 basis points instead of 5. A key a params file leaves out takes the default in `Params`, not
   the one in `default.json`:

   ```json
   "cost_bps": 15.0
   ```

2. Decide a saved run with it, beside the default, in one table. In `main.py`:

   ```python
   table = sweep(model, "RUN_ID", params=["params/default.json", "params/costly.json"])
   ```

   Each row is a params file and a portfolio, with its objective, turnover and each term's amount as a metric.

To add a term:

1. Add its params to `Params` in `model.py`, with docstrings and defaults, and to `params/default.json`. This one is a
   soft cap: a penalty on each bond's weight above `soft_cap`, which the objective trades against income. A penalty of
   0 turns it off:

   ```python
   soft_cap: Weight = 0.03
   """The weight above which a bond's excess value is penalized."""
   soft_cap_penalty: float = Field(default=0.0, ge=0)
   """The penalty per unit of a bond's value above `soft_cap`, against income: 0 for none."""
   ```

2. In `terms`, in `optimizer/problem.py`, add its amount, in the portfolio's currency:

   ```python
   weights = cp.multiply(unit / portfolio.value, held + buy - sell)
   return {
       "income": (universe["expected_return"].to_numpy(dtype=float) * unit) @ (held + buy - sell),
       "cost": (unit * params.cost_bps / 1e4) @ (buy + sell),
       "excess": params.soft_cap_penalty * portfolio.value * cp.sum(cp.pos(weights - params.soft_cap)),
   }
   ```

   A term must keep the problem one the solver solves. The example's problem has integer variables, for trade sizes, and
   HiGHS solves linear and piecewise-linear terms, such as `cp.pos` of a linear expression, alongside them, but not
   quadratic ones; COPT solves those too (see the `solve` settings in the [reference](../reference.md)).
3. In `build`, add it to the objective, with its sign:

   ```python
   cp.Maximize((found["income"] - found["cost"] - found["excess"]) / value)
   ```

4. Declare its metric in `Metrics` in `model.py`, with a docstring. The contract test requires the fixture's decisions
   to report exactly the metrics `Metrics` declares, and fails otherwise:

   ```text
   income's decisions report metrics its Metrics does not declare, or declares metrics they do not report: ['excess']
   ```

5. Test it in `tests/test_optimizer.py`:

   ```python
   def test_an_excess_term_holds_weights_toward_its_soft_cap() -> None:
       found = portfolio({"security_id": "A"}, {"security_id": "B"})

       capped = decide(found, max_bond_weight=0.1, soft_cap_penalty=0.1)

       assert capped.buy == {"A": 30_000.0, "B": 30_000.0}
       assert capped.metrics["excess"] == pytest.approx(0.0)
   ```

To drop a term, remove it from `terms`, from the objective in `build`, and from `Metrics`.

Changing the objective changes what every portfolio decides, so the behaviour tests that solve, in
`tests/test_optimizer.py`, may need new expectations. To confirm it worked, run `uv run pytest`, then sweep a saved run
with a params file that sets the term's params, as in step 2 of the first procedure, and compare its rows with the
default's. Describe the objective, in business terms, under "What it maximizes" in the model's `README.md`, and in
`build`'s and `terms`' docstrings.

A term may differ by portfolio the way a limit does: a rule sets its weight on the portfolio, and `terms` reads it there
(see [how to add a constraint](add-a-constraint.md#take-a-limit-per-portfolio-from-a-dataset)).
