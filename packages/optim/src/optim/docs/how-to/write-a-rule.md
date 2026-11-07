# How to write a rule

Add one of the model's rules: logic that decides what a portfolio may trade and on what terms, such as a rating floor,
a restricted list, a client's no-sell list or a client's own limits. Rules are much of a model's particular logic, so
each is a plain function in the model's `rules/` folder, tested on a few rows without a solver.

Prerequisites:

- A model made by `optim new`, whose `prepare_portfolio` in `model.py` builds the portfolio with `prep.portfolio` and
  then applies the rules in order.
- Any data the rule reads, as a dataset of the model (see [how to add a dataset](add-a-dataset.md)).

To write a rule:

1. Write it in the module of `rules/` it belongs with, or in a new module there, named for the group, such as
   `rules/maturity.py`. Every rule has one signature: it takes the portfolio, the market and the params, and returns
   the portfolio, changed with `dataclasses.replace`. Its docstring states the rule in business terms:

   ```python
   """Maturity: what the portfolio may buy, by when it matures."""

   from dataclasses import replace

   import pandas as pd

   from models.income import model


   def min_years(portfolio: model.Portfolio, market: model.Market, params: model.Params) -> model.Portfolio:
       """Never buy a bond that matures within `params.min_years` of the business date: it would soon be cash again. One
       the portfolio holds may still be sold."""
       universe = portfolio.universe
       soonest = pd.Timestamp(portfolio.data.as_of_date) + pd.Timedelta(days=365.25 * params.min_years)
       return replace(portfolio, universe=universe.assign(buyable=universe["buyable"] & (universe["maturity"] > soonest)))
   ```

   A rule may change anything about the portfolio but solve it:

   - Set the universe's flags, `buyable`, `sellable` and `must_sell`, as above. A security the portfolio may not buy
     may still be held, and sold.
   - Drop rows of the universe, as `screens.exclude_sectors` drops the excluded bonds the portfolio does not hold. A
     held bond dropped from the universe stays held, and is not traded.
   - Add columns, which later rules and the optimizer read.
   - Change a limit the optimizer reads from the portfolio, as `mandates.sector_caps` tightens the portfolio's
     `sector_caps` with its mandates: `replace(portfolio, sector_caps=caps)`. See
     [how to add a constraint](add-a-constraint.md#take-a-limit-per-portfolio-from-a-dataset).

   The universe has a row per security the portfolio holds, cash aside, or may buy, with every column of the shared
   securities (`issuer`, `sector`, `rating`, `maturity` and the rest), `unit_value`, `expected_return`, `held` and the
   flags. The portfolio's own rows of each dataset are in `portfolio.data`, such as `portfolio.data.controls`, and the
   business date is `portfolio.data.as_of_date`. Since every rule shares the signature, most leave an argument unused,
   which ruff allows in `rules/`.
2. If a param drives the rule, add it to `Params` in `model.py`, with a docstring under it and a default, and to
   `params/default.json`:

   ```python
   min_years: float = Field(default=1.0, ge=0)
   """Never buy a bond that matures sooner than this many years from the business date."""
   ```

3. Call it in `prepare_portfolio` in `model.py`, one line, where it applies in the order the rules run, and import its
   module beside the others:

   ```python
   def prepare_portfolio(data: Data, market: Market, params: Params) -> Portfolio:
       portfolio = prep.portfolio(data, market, params)
       portfolio = controls.apply(portfolio, market, params)
       portfolio = mandates.sector_caps(portfolio, market, params)
       portfolio = credit.min_rating(portfolio, market, params)
       portfolio = screens.exclude_sectors(portfolio, market, params)
       portfolio = maturity.min_years(portfolio, market, params)
       return screens.exclude_issuers(portfolio, market, params)
   ```

   What every portfolio must obey whatever the params, such as its mandatory controls, comes first, so that no params
   file can leave it out.
4. Test it in `tests/test_rules.py`, on a few bonds. `tests/builders.py` makes what the test needs in a line:
   `portfolio(*bonds)` is P1 as `prepare_portfolio` starts it, each bond filled from a valid default bond with the
   fields given, held where it gives `held`; `params(**changes)` is `default.json` with changes; and `MARKET` is a
   market for rules that leave it unused:

   ```python
   from datetime import date

   from models.income.rules import maturity
   from models.income.tests.builders import MARKET, params, portfolio


   def test_a_bond_that_matures_too_soon_may_not_be_bought_but_may_be_sold() -> None:
       found = portfolio({"security_id": "A", "maturity": date(2027, 3, 1), "held": 5000.0}, {"security_id": "B", "maturity": date(2030, 1, 15)})

       after = maturity.min_years(found, MARKET, params(min_years=1.0))

       assert after.universe.set_index("security_id")[["buyable", "sellable"]].to_dict("index") == {"A": {"buyable": False, "sellable": True}, "B": {"buyable": True, "sellable": False}}
   ```

   A rule that reads a dataset takes it as a keyword of `portfolio`, such as
   `portfolio({"security_id": "A", "held": 5000.0}, controls=frame(CONTROLS, ...))`.
5. Name the rule, in business terms, under "Constraints" in the model's `README.md`.

To confirm it worked, run `uv run pytest`, then decide a saved run again with a params file that sets the new param,
such as `params/long.json` with `"min_years": 10.0`, which leaves the fixture's portfolios nothing to buy. In
`main.py`, call:

```python
replay(model, "RUN_ID", params="params/long.json")
```

Its trades hold only sales. A rule needs no check after the solve: the optimizer keeps to the flags and constraints the
rules set, and the core rejects a decision that rounding moves, so what the solver proved still holds.

A rule that needs the whole market at once, such as ranking every bond, belongs in `prepare_market` or `prep.market`,
which run once per run; a rule about one portfolio belongs in `prepare_portfolio`, which runs in parallel. For why the
hooks are split so, see [how optim is put together](../explanation.md).
