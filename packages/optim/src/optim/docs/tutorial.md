# Build, replay and sweep your first model

In this tutorial, we will make a model repo, run its model on its fixture from `main.py`, add a rule and a
constraint, replay the first run, and sweep two params files side by side, in about twenty minutes. The model
rebalances fixed income portfolios as an integer program. Everything runs on the model's fixture, a few rows of test
data, so you will need no database and no credentials.

Before you start, you need:

- [uv](https://docs.astral.sh/uv/), which installs Python 3.14 on first use, and git.
- An editor, such as PyCharm or VS Code, and a terminal: PowerShell on Windows, or any shell elsewhere. The commands
  are the same in both.
- The address of the firm's internal package index, where `optim` is published, which we call `INDEX_URL`. If the
  index needs a login, set uv's credentials for it first, as the index's owner describes. To try a checkout of `optim`
  instead, run `uv run optim new repo sma-optimizer income --optim-path . --dir DIR` in the checkout, where `DIR` is
  the folder to make the repo in, and go on from `cd DIR/sma-optimizer`.

## Make the repo

Make a model repo named `sma-optimizer`, with one model, `income`:

```sh
uvx --index INDEX_URL optim new repo sma-optimizer income --index INDEX_URL
cd sma-optimizer
```

The first `--index` is where `uvx` finds `optim`; the second is written into the new repo, so that its own `uv sync`
finds it too. The last line of the output names the folder it made:

```text
created /home/you/sma-optimizer: commit it once you have read it
```

`optim` has made the repo, run `git init`, and installed its dependencies; the first commit is yours, whenever you
like. Open the folder in your editor, with `.venv` as its Python interpreter. Notice that everything about the model
is in one folder, `models/income/`: its code, its params in `params/default.json`, and its tests, with its fixture in
`tests/fixture/`, one CSV file per dataset.

## Run the tests

```sh
uv run pytest
```

The output should end with:

```text
models/income/tests/test_contract.py ...                                 [ 21%]
models/income/tests/test_optimizer.py ....                               [ 50%]
models/income/tests/test_rules.py ......                                 [ 92%]
tests/test_agent_files.py .                                              [100%]

======================= 14 passed, 1 deselected in 1.57s =======================
```

The three tests in `test_contract.py` check that the model's default params are valid, that it accepts every
portfolio of its fixture, and that its code cannot reach a database or an API through its imports. The deselected
test is the QA suite, which runs only against QA.

## Look at the model

Open `models/income/model.py`. It is the model's table of contents:

- `Params`, the settings a run takes from its params file, such as `max_issuer_weight`, the most of a portfolio's
  value in one issuer, and `min_rating`, the rating floor for what it buys.
- `Data`, the datasets it reads: holdings and securities, which every model reads, and its own `portfolios`,
  `signals`, `controls` and `mandates`.
- The hooks: `prepare_market` and `prepare_portfolio`, which call `prep.py` and then each rule in `rules/`, one per
  line, and `decide`, which solves the portfolio's integer program in `optimizer/`.

The fixture has two portfolios. P1 holds $1mm of cash and 100,500 face of bond B, worth about $101,000, or 9% of its
value. P2 holds $500,000 of cash, has its own rating floor of AA-, and has a mandate that caps Financials at 4% of its
value.

## Run the model on its fixture

Open `models/income/main.py`. It holds one call, which decides portfolios with live data. Add
`data="tests/fixture"`, so that it reads every dataset from the fixture instead:

```python
    results = run(model, params="params/default.json", portfolios=None, env=".env", data="tests/fixture")
```

Paths are the model's folder's, wherever your editor runs the file from. Run the file: press Run in your editor, or
from a terminal:

```sh
uv run python models/income/main.py
```

The run logs what it does, then prints a row per portfolio:

```text
run 01a169a6-1c9f-7638-8326-80b6ee410ff7
portfolio_id   status  trades  solved  objective    stop  seconds
          P1 accepted       3    True   0.006741 optimal 0.031582
          P2 accepted       3    True   0.006447 optimal 0.022347
```

Your run id and times will differ; keep the id, which this tutorial calls `FIRST_RUN`. Both portfolios are
`accepted`: their trades may trade. The run decided every portfolio the fixture lists, as of the fixture's date,
2026-10-16, and saved a snapshot in the repo's `snapshots/FIRST_RUN/`, holding every dataset as it was loaded and
every decision. A run from `main.py` never writes orders.

## Look at the results

`results` holds the run's decisions, trades, reasons and metrics as tables. To see them, set a breakpoint on the
`results = run(...)` line, debug the file, and step over the line: your editor's debugger shows `results`. From a
terminal, `uv run python -i models/income/main.py` runs the file and leaves you at a Python prompt:

```text
>>> results.trades
  portfolio_id security_id  side  quantity  decided
0           P1           A   buy   54000.0  54000.0
1           P1           E   buy   55000.0  55000.0
2           P1           B  sell   46000.0  46000.0
3           P2           A   buy   24000.0  24000.0
4           P2           C   buy   19000.0  19000.0
5           P2           E   buy   25000.0  25000.0
```

Notice that P1 sells 46,000 of B, in whole lots of 1,000, which brings B under the 5% cap per bond in the params, and
that neither portfolio buys D, which is rated below `A-`, the params' `min_rating`. P2 buys no B, which is rated below
its own floor, and only 19,000 of C, a Financials bond, under its mandate's 4% cap. `quantity` is what may trade, and
`decided` what the model decided, before `optim` rounded it to whole lots.

## Add a rule

A rule changes what a portfolio may trade before the optimizer sees it. Add one that buys only bonds maturing within
a number of years. Make `models/income/rules/maturity.py`:

```python
"""Maturity: what the portfolio may buy, by when it matures."""

from dataclasses import replace

import pandas as pd

from models.income import model


def max_years(portfolio: model.Portfolio, market: model.Market, params: model.Params) -> model.Portfolio:
    """Buy only bonds that mature within `params.max_years` of the business date."""
    universe = portfolio.universe
    cutoff = pd.Timestamp(portfolio.data.as_of_date) + pd.DateOffset(years=params.max_years)
    return replace(portfolio, universe=universe.assign(buyable=universe["buyable"] & (universe["maturity"] <= cutoff)))
```

Every rule has this signature: it takes the portfolio, the market and the params, and returns the portfolio, changed.
Here it narrows the `buyable` flag of the portfolio's universe, a row per bond it holds or may buy.

In `model.py`, add the param to `Params`, after `exclude_issuers`:

```python
    max_years: int = Field(default=30, gt=0)
    """Buy only bonds that mature within this many years of the business date."""
```

Import the rule, by adding `maturity` to the `from models.income.rules import ...` line, and call it in
`prepare_portfolio`, after the rating floor:

```python
    portfolio = credit.min_rating(portfolio, market, params)
    portfolio = maturity.max_years(portfolio, market, params)
```

In `params/default.json`, add `"max_years": 6,` after `"exclude_issuers": [],`. Then test the rule on two bonds: in
`tests/test_rules.py`, add `from datetime import date` to the imports and `maturity` to the rules' import, and add:

```python
def test_a_bond_that_matures_after_the_limit_may_not_be_bought() -> None:
    found = portfolio({"security_id": "A", "maturity": date(2030, 10, 15)}, {"security_id": "B", "maturity": date(2040, 1, 15)})

    after = maturity.max_years(found, MARKET, params(max_years=6))

    assert after.universe.set_index("security_id")["buyable"].to_dict() == {"A": True, "B": False}
```

`portfolio` builds a portfolio from a few bonds, each filled in from a default bond, and `params` is `default.json`
with the changes given. Run the tests:

```sh
uv run pytest
```

The output should end with `15 passed, 1 deselected`. Run `main.py` again. The trades should now be:

```text
  portfolio_id security_id  side  quantity  decided
0           P1           A   buy   54000.0  54000.0
1           P1           B  sell   46000.0  46000.0
2           P2           A   buy   24000.0  24000.0
3           P2           C   buy   19000.0  19000.0
```

Neither portfolio buys E any more: it matures in May 2033, more than six years after the business date.

## Add a constraint

A model's limits are params, and its constraints, cvxpy functions in `optimizer/constraints.py`, hold the integer
program to them. Cap Financials at 2% of every portfolio's value. In `params/default.json`, change `sector_caps`:

```json
  "sector_caps": {"Financials": 0.02},
```

Run `main.py` again. The trades should now be:

```text
  portfolio_id security_id  side  quantity  decided
0           P1           A   buy   54000.0  54000.0
1           P1           B  sell   79000.0  79000.0
2           P2           A   buy   24000.0  24000.0
3           P2           C   buy    9000.0   9000.0
```

P1 sells more of B, a Financials bond, to bring Financials under 2%. P2 buys 9,000 of C instead of 19,000: its mandate
allows 4%, but the params' cap applies too, and the tighter one binds.

## Replay the first run

A replay decides a saved run again from its snapshot alone, with the code you have now. In `main.py`, import `replay`
instead of `run`, and replace the call with one that names your first run's id:

```python
from optim import replay

from models.income import model

if __name__ == "__main__":  # each worker process the run starts imports this file, and must not start a run itself
    results = replay(model, "FIRST_RUN")
```

Run the file. The output should look like this:

```text
run 01a169a7-3d4d-740c-954b-c5bacbd74f14
portfolio_id   status  trades  solved  objective    stop  seconds
          P1 accepted       3    True   0.006741 optimal 0.055126
          P2 accepted       3    True   0.006447 optimal 0.026879
```

The replay read nothing but the snapshot, and saved itself as a new run beside the first. Notice that it decided as
the first run did, E included: a replay takes its run's params, which had no Financials cap and no `max_years`, so the
rule's default of 30 years applied. Your new rule leaves an old run as it was. Add `params="params/default.json"` to
the call to decide the first run's data with today's params instead.

## Sweep two params files

A sweep decides a saved run once per params file, saves nothing, and returns one table. Make a variant of the params
that allows 4% in Financials: copy `params/default.json` to `params/loose.json`, and in the copy change the Financials
cap's `0.02` to `0.04`.

In `main.py`, import `sweep` instead of `replay`, and replace the call:

```python
    table = sweep(model, "FIRST_RUN", params=["params/default.json", "params/loose.json"])
```

Run the file with `uv run python -i models/income/main.py`, and look at the table's first columns:

```text
>>> table[["variant", "portfolio_id", "accepted", "objective", "turnover", "trades", "income"]]
   variant portfolio_id  accepted  objective  turnover  trades     income
0  default           P1      True   0.003340  134498.0       2  3744.8105
1  default           P2      True   0.003359   33492.0       2  1696.2240
2    loose           P1      True   0.004174  112344.0       2  4653.1245
3    loose           P2      True   0.004296   43572.0       2  2169.9840
```

There is a row per params file and portfolio. Notice that the looser cap earns more income in both portfolios, and
that P1 trades less, since it sells less of B. The table also has each portfolio's cash after trading, its solve
time, the model's other metric, `cost`, and the reasons for any rejection.

When you are done, put `main.py`'s import and call back as they were, with `run`. Its docstring shows the `replay`
and `sweep` calls, for when you need them again.

## Next steps

You have built a model repo, run its model on its fixture, added a rule and a constraint, replayed a run and swept two
params files. To run the model on real data, see [how to run a model on live data](how-to/run-on-live-data.md). To
make the model your own, see [how to add a dataset](how-to/add-a-dataset.md),
[how to write a rule](how-to/write-a-rule.md) and [how to add a constraint](how-to/add-a-constraint.md). For every
hook, setting and command, see the [reference](reference.md). To understand why runs replay and model code does no
I/O, read
[how optim is put together](explanation.md).
