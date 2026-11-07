# How to add a constraint

Limit what a portfolio's decision may do, such as a cap on a sector, a client's own turnover limit, or how many bonds
it buys. A constraint is cvxpy: a function in the model's `optimizer/constraints.py`, which `build` in
`optimizer/problem.py` adds to the problem. Its limit, the number, comes from the params, the same for every portfolio,
or from the portfolio, where a rule set it from a dataset.

Prerequisites:

- A model made by `optim new`, with its tests passing.
- For a limit from a dataset, the dataset (see [how to add a dataset](add-a-dataset.md)).

## Change a limit for every portfolio, in the params

The example's constraints read their limits from the params: `max_bond_weight`, `max_issuer_weight`, `sector_caps` and
`max_turnover`, which `uv run optim describe MODEL` lists with their docstrings.

1. Set the limit in `params/default.json`:

   ```json
   "max_issuer_weight": 0.08,
   "sector_caps": {"Industrials": 0.2},
   ```

   A weight cap leaves the portfolio what it may not sell, however much it weighs. A cap of `1.0` caps nothing, so it
   turns a weight limit off, and `sector_caps` caps only the sectors it names.
2. Run `uv run pytest`. Params are strict, and the tests read `default.json`, so a misspelled key fails them, naming
   it:

   ```text
   max_issuer_wieght
     Extra inputs are not permitted [type=extra_forbidden, input_value=0.08, input_type=float]
   ```

3. If the README's "Constraints" names the old limit, change it there too.

To try a limit in one params file instead, such as a variant a sweep compares, copy `default.json` whole, such as to
`params/tight.json`, and change the limit in the copy: a key a params file leaves out takes the default in `Params`,
not the one in `default.json`. See [how to compare params with a sweep](compare-params-with-a-sweep.md).

## Write a new constraint

A new constraint is a function in `optimizer/constraints.py`, on the `buy` and `sell` variables that `build` makes, one
entry per row of the universe, and a param for its limit. This one caps how many bonds a portfolio buys:

1. Write it in `optimizer/constraints.py`:

   ```python
   def most_buys(universe: pd.DataFrame, buy: cp.Variable, count: int, *, value: float) -> list[cp.Constraint]:
       """At most `count` bonds bought: each buy needs its bond chosen, and at most `count` are chosen."""
       chosen = cp.Variable(len(universe), boolean=True)
       return [buy <= cp.multiply(value / universe["unit_value"].to_numpy(dtype=float), chosen), cp.sum(chosen) <= count]
   ```

2. Add its param to `Params` in `model.py`, with a docstring and a default, and to `params/default.json`:

   ```python
   max_buys: int = Field(default=10, ge=0)
   """The most bonds a portfolio buys in one rebalance."""
   ```

3. Add it to the list of constraints in `build`, in `optimizer/problem.py`:

   ```python
   limits = [
       *constraints.sizes(universe, buy, sell, value=value),
       *constraints.cash(portfolio, buy, sell),
       *constraints.turnover(universe, buy, sell, params.max_turnover, value=value),
       *constraints.max_weight(universe, buy, sell, "security_id", params.max_bond_weight, value=value),
       *constraints.max_weight(universe, buy, sell, "issuer", params.max_issuer_weight, value=value),
       *constraints.max_weight(universe, buy, sell, "sector", portfolio.sector_caps, value=value),
       *constraints.most_buys(universe, buy, params.max_buys, value=value),
   ]
   ```

4. Test it in `tests/test_optimizer.py`, which solves a portfolio of a few bonds:

   ```python
   def test_most_buys_caps_how_many_bonds_are_bought() -> None:
       found = portfolio({"security_id": "A"}, {"security_id": "B"}, {"security_id": "C"})

       decision = decide(found, max_bond_weight=0.1, max_buys=2)

       assert len(decision.buy) == 2
   ```

## Take a limit per portfolio, from a dataset

A client's own limits are rows of a dataset. A rule reads the portfolio's rows and sets the limit on the portfolio, and
`build` reads it there: the example's `mandates` dataset and `rules/mandates.py` set each portfolio's `sector_caps` this
way. A constraint that only some portfolios have works the same way: its field on the portfolio is empty unless a rule
fills it, and its function returns no constraints then, as `max_weight` does for a portfolio with no sector caps. This
takes a client's own turnover limit:

1. Declare the dataset in `datasets/turnover_limits.py`, read per portfolio, with a row for each portfolio that has a
   limit; write its query in `datasets/sql/turnover_limits.sql`; add it to `Data` in `model.py` as
   `turnover_limits = TURNOVER_LIMITS`; and add a CSV of it to the fixture, with a row for some of the fixture's
   portfolios but not all, so the contract test decides a portfolio without one. See
   [how to add a dataset](add-a-dataset.md).

   ```python
   """Each portfolio's own turnover limit, from SQL Server, where its client set one."""

   from pathlib import Path

   from optim import Dataset, Row
   from optim.schema import Id, Weight

   SQL = Path(__file__).parent / "sql"


   class TurnoverLimit(Row):
       """A client's limit on how much of their portfolio trades in one rebalance."""

       portfolio_id: Id
       max_turnover: Weight
       """The most the portfolio may buy and sell together, as a fraction of its value."""


   TURNOVER_LIMITS = Dataset("turnover_limits", TurnoverLimit, source="mssql", query=SQL / "turnover_limits.sql", key=("portfolio_id",))
   ```

2. Add the limit to `Portfolio` in `model.py`, and start it at the params' limit in `prep.portfolio`, as
   `max_turnover=params.max_turnover`:

   ```python
   max_turnover: float
   """The most it buys and sells together, as a fraction of its value: the params' limit, or its own if tighter."""
   ```

3. Write a rule that tightens it, in `rules/limits.py`:

   ```python
   """Limits a client set on their own portfolio, on top of the params' own."""

   from dataclasses import replace

   from models.income import model


   def turnover(portfolio: model.Portfolio, market: model.Market, params: model.Params) -> model.Portfolio:
       """The portfolio's own turnover limit, where it has one and it is tighter than the params'."""
       own = portfolio.data.turnover_limits["max_turnover"]
       return replace(portfolio, max_turnover=min([portfolio.max_turnover, *own]))
   ```

4. Import `limits` beside the other rules in `model.py`, and call it in `prepare_portfolio`, after
   `mandates.sector_caps`: `portfolio = limits.turnover(portfolio, market, params)`.
5. Test the rule in `tests/test_rules.py`, importing `TURNOVER_LIMITS` and `limits`, on a portfolio with no limit of its
   own and one with a tighter limit:

   ```python
   def test_a_portfolios_own_turnover_limit_applies_where_it_is_tighter() -> None:
       own = frame(TURNOVER_LIMITS, {"portfolio_id": "P1", "max_turnover": 0.1})

       assert limits.turnover(portfolio({"security_id": "A"}), MARKET, params()).max_turnover == 0.25
       assert limits.turnover(portfolio({"security_id": "A"}, turnover_limits=own), MARKET, params()).max_turnover == 0.1
   ```

6. In `build`, read the portfolio's limit instead of the params':

   ```python
   limits = [
       *constraints.sizes(universe, buy, sell, value=value),
       *constraints.cash(portfolio, buy, sell),
       *constraints.turnover(universe, buy, sell, portfolio.max_turnover, value=value),
       *constraints.max_weight(universe, buy, sell, "security_id", params.max_bond_weight, value=value),
       *constraints.max_weight(universe, buy, sell, "issuer", params.max_issuer_weight, value=value),
       *constraints.max_weight(universe, buy, sell, "sector", portfolio.sector_caps, value=value),
   ]
   ```

7. In `tests/test_optimizer.py`, `decide` sets the limits a test gives in the params, which `build` no longer reads
   for turnover, so set the portfolio's limit from them too. Without it, the tests that solve keep the default 25% and
   fail:

   ```python
   def decide(found: model.Portfolio, **limits: float) -> optim.Decision:
       chosen = params(**ROOM | limits)
       return solve.solve(replace(found, max_turnover=chosen.max_turnover), chosen)
   ```

How limits from different places combine is the rules' logic: a rule may also loosen the params' limit for a
portfolio. See [how to write a rule](write-a-rule.md).

To confirm it worked, run `uv run pytest`, then run the model on its fixture, with `data="tests/fixture"` in
`main.py`, before and after the change, and compare the trades. Name the constraint, in business terms, under
"Constraints" in the model's `README.md`, and a new dataset under "Data".

A constraint needs no second check after the solve. The solver proved its decision meets every constraint before the
core rounds it, and the core rejects a decision that rounding moves by more than a millionth of the portfolio's value,
so what the solver proved still holds. A portfolio whose constraints cannot all be met is rejected as `not solved:
infeasible`. For the objective, see [how to change the objective](change-the-objective.md).
