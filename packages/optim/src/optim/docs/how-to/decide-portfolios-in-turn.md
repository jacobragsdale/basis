# How to decide portfolios one at a time

Make portfolios that compete for a limited supply, such as a new issue or a scarce bond, decide one at a time, each
seeing what the portfolios before it left. Define `reserve` in the model's `model.py`, and `order` to choose who goes
first. The steps below add a dealer's offerings to the example model; paths are in the model's folder, `models/MODEL/`.

Prerequisites:

- A model made by `optim new`, whose `prepare_market` returns a `Market` and whose `decide` calls `solve.solve`, as the
  example's do.
- A dataset of what is on offer, read whole: here `offerings`, with a `security_id` and a `quantity` per row, declared in
  `datasets/offerings.py`, added to `Data` as `offerings = OFFERINGS`, and given a `tests/fixture/offerings.csv` (see
  [how to add a dataset](add-a-dataset.md)).

To decide portfolios one at a time:

1. Give the `Market` the supply, in `model.py`. The market travels to worker processes, so it must pickle, as a frozen
   dataclass of frames, dicts and numbers does:

   ```python
   @dataclass(frozen=True)
   class Market:
       securities: pd.DataFrame
       left: dict[str, float]
       """Quantity still on offer, by security id."""
   ```

   and fill it in `prep.py`'s `market`, which `prepare_market` calls:

   ```python
   securities = joined.assign(unit_value=unit_value(joined), has_signal=joined["expected_return"].notna(), expected_return=joined["expected_return"].fillna(0.0))
   return model.Market(securities, left=dict(zip(data.offerings["security_id"], data.offerings["quantity"], strict=True)))
   ```

2. Cap each buy by what is left, as a constraint written in cvxpy, in `optimizer/constraints.py`:

   ```python
   def supply(universe: pd.DataFrame, buy: cp.Variable, left: Mapping[str, float]) -> list[cp.Constraint]:
       """Buy no more of a bond on offer than is left of it; a bond not on offer has no such cap."""
       on_offer = universe["security_id"].isin(list(left)).to_numpy()
       return [buy[on_offer] <= universe.loc[on_offer, "security_id"].map(left).to_numpy(dtype=float)] if on_offer.any() else []
   ```

   Give `solve` in `optimizer/solve.py` the supply, as `left: Mapping[str, float] | None = None` after its params, and
   add `*constraints.supply(universe, built.buy, left or {})` to its `own` constraints. With a default of none, the
   optimizer's tests, which solve a portfolio alone, still run.
3. Pass the market's supply to the solve, in `decide`. The supply must reach `decide` this way rather than through a
   rule: rules run in `prepare_portfolio`, for every portfolio in parallel, before any decides, so they see the supply
   as it started.

   ```python
   def decide(portfolio: Portfolio, market: Market, params: Params) -> optim.Decision:
       return solve.solve(portfolio, params, left=market.left)
   ```

4. Define `reserve(market, decision)`, which takes the market and an accepted decision, rounded, and returns the market
   the next portfolio decides with. A decision's `buy` maps security ids to quantities. Return a new market rather
   than changing the old one:

   ```python
   def reserve(market: Market, decision: optim.Decision) -> Market:
       left = {security: quantity - decision.buy.get(security, 0.0) for security, quantity in market.left.items()}
       return replace(market, left=left)
   ```

   Defining `reserve` is what makes the portfolios decide in turn. Only an accepted decision reserves, so a rejected
   portfolio leaves its supply to the next one.
5. Define `order(portfolios, market, params)`, which takes what `prepare_portfolio` returned for each portfolio, by id,
   the market and the params, and returns the ids in the order they decide. Who gets a scarce bond is a
   client-fairness decision, so state the rule in its docstring and in the model's `README.md`. With the example's
   `Portfolio`, which has the portfolio's `cash`:

   ```python
   def order(portfolios: Mapping[str, Portfolio], market: Market, params: Params) -> list[str]:
       """Largest cash first, then by id."""
       return sorted(portfolios, key=lambda portfolio_id: (-portfolios[portfolio_id].cash, portfolio_id))
   ```

   It must return every id once, or the run fails. Without `order`, portfolios decide in the run's order: the order
   given, or the order the model's `portfolios` dataset or the fixture's holdings list them in.
6. Import what the new code uses: `from collections.abc import Mapping` and `from dataclasses import dataclass,
   replace` in `model.py`, and `Mapping` in `optimizer/constraints.py` and `optimizer/solve.py`.
7. Test the sequence in a behaviour test: call `reserve` on a market and a decision, such as
   `optim.Decision(buy={"NEW": 1000.0})`, and assert on what it leaves, and call `order` on a few portfolios built with
   `tests/builders.py`.

`prepare_portfolio` still runs for every portfolio in parallel, before any decides; only `decide`, `check` and
`reserve` run one at a time, in the run's own process, so a run of many portfolios takes about the sum of their
decisions.

To confirm it worked, run the model on its fixture: add `data="tests/fixture"` to the `run(...)` call in `main.py` and
press Run. The second portfolio in the order buys only what the first left: with 60,000 of a bond on offer, a first
portfolio that buys 55,000 leaves the next 5,000. The run's `run.json`, in the repo's `snapshots/RUN_ID/`, records the
order under `order`.

Replaying one portfolio of such a run decides the portfolios before it again, so it sees the supply it saw. In
`main.py`, call this instead of `run`:

```python
replay(model, "RUN_ID", portfolios=["PORTFOLIO_ID"])
```

Replace `RUN_ID` with the id the run printed, and `PORTFOLIO_ID` with a portfolio of it; import `replay` from `optim`.

If the allocation rule does not depend on what each portfolio decides, such as pro rata by size, allocate the supply in
`prepare_market` instead, and keep `decide` parallel. For the trade-off, see
[how optim is put together](../explanation.md#portfolios-that-compete-for-supply).
