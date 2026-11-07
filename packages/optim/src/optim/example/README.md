# A taxable SMA rebalance

Rebalances each separately managed fixed income portfolio on its own, as an integer program solved through
`optim.cvxpy`. "Taxable" describes the mandates: the model trades holdings by security, and neither selects tax lots
nor harvests losses.

| File | Holds |
|---|---|
| `main.py` | Press Run: decides portfolios with the default params, from the sources `.env` configures. |
| `model.py` | The params, the data, the metrics, and the hooks that call prep, the rules and the optimizer. |
| `prep.py` | The market every portfolio shares, and each portfolio's universe before the rules. |
| `datasets/` | The datasets the model reads, besides the holdings and securities, with their SQL. |
| `rules/` | The rules, which narrow what each portfolio may trade and add its mandates. |
| `optimizer/` | The cvxpy problem: its objective, and its constraints, one function each. |
| `params/default.json` | The default params; files beside it are variants. |
| `tests/` | The tests, and `fixture/`, a CSV per dataset, which the contract tests decide. |

## What it maximizes

Expected income over a year after trading, less estimated trading costs, as a fraction of the portfolio's value. Each
bond's expected return comes from the signals API; a bond without a signal is not bought.

## Constraints

- Trades are sizes the market trades: a buy is whole increments of at least the minimum piece, and a sale is the whole
  holding, or whole increments of at least the minimum piece that leave at least the minimum remaining.
- A bond is bought or sold, never both: selling it to buy it back would be a wash sale.
- Cash after trading is not negative.
- The params' limits apply to every portfolio: by default, no bond is more than 5% of the portfolio's value after
  trading, no issuer more than 10%, and buys plus sales are at most 25% of its value; `sector_caps` caps sectors. A
  cap leaves a portfolio what it may not sell.
- Each portfolio's mandates cap sectors too; where the params also cap a sector, the tighter applies.
- The portfolio's controls bind whatever the params say: `no_buy`, `no_sell`, `hold`, and `must_sell`, which sells the
  whole holding and buys none back. Controls that both require and forbid a sale reject the portfolio.
- Rules from the params: buy only bonds rated `min_rating` or better, or better still where the portfolio's own floor is
  stricter; never buy the sectors in `exclude_sectors` or the issuers in `exclude_issuers`.

## Data

The shared holdings and securities; the portfolios the model manages, with each client's own rating floor
(`portfolios`); the expected returns of the signals API (`signals`); and each portfolio's trading controls (`controls`)
and sector caps (`mandates`), from SQL Server.

## Known limits

- One currency per portfolio, and no FX rates.
- The trading cost is one flat estimate in basis points, the same for every bond.
- Portfolios do not compete for a bond's supply: each decides alone.
