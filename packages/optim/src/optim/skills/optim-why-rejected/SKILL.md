---
name: optim-why-rejected
description: Find out why optim rejected a portfolio, by reading its reasons and deciding it again from the run's snapshot, and tell bad data from a model bug. Use when asked why a portfolio was rejected, did not trade, failed, or traded unexpectedly.
---

# Find out why a portfolio was rejected

Follow `uv run optim docs how-to/find-out-why-a-portfolio-was-rejected`. In short:

1. Read its reasons: the run's summary, `results.reasons` from `main.py`, or the snapshot's `reasons.parquet`. A
   hook's error names the line of model code that raised; the run's log holds its traceback.
2. Decide it again from the run's snapshot with the code you have now, in the model's `main.py`:
   `replay(model, RUN_ID, portfolios=["P1"], env=".env.prod")`, where `.env.prod` sets production's
   `OPTIM_SNAPSHOT_ROOT`. The replay saves in the repo's `snapshots/` and reads nothing live.
3. To look inside, put a breakpoint in the rule or the optimizer and debug `main.py`, or decide it from Python with
   `optim.research.trial(open_snapshot(root), "P1")`, which hands back the portfolio, the market, the cvxpy problem
   and the outcome.
4. Tell the data from the model: compare the portfolio's rows in the snapshot's `inputs/` with what the model assumed.

Report the reason, its cause and the fix. Never loosen a check of the core's to make a portfolio pass.
