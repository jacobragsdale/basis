# Rules

What a portfolio may trade, and on what terms, before the optimizer sees it. One module per group of rules, and every
rule has one signature: it takes the portfolio, the market and the params, and returns the portfolio, changed.

```python
def exclude_issuers(portfolio: model.Portfolio, market: model.Market, params: model.Params) -> model.Portfolio:
    universe = portfolio.universe
    return replace(portfolio, universe=universe.assign(buyable=universe["buyable"] & ~universe["issuer"].isin(params.exclude_issuers)))
```

A rule may do anything short of solving: drop rows of the universe, add columns, set its `buyable`, `sellable` and
`must_sell` flags, and change what the optimizer reads from the portfolio, such as its `sector_caps`. A held bond
dropped from the universe stays held, and is not traded. Rules apply in the order `prepare_portfolio` in `model.py`
calls them, one per line.

To add one:

1. Write it in the module it belongs with, or a new one here, with a docstring that says the rule in business terms.
2. Call it in `prepare_portfolio`, where it applies.
3. Test it in `tests/test_rules.py`, on a few rows: `builders.portfolio(...)` makes the portfolio.

`uv run optim docs how-to/write-a-rule` says more.
