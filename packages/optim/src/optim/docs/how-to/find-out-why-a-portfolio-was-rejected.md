# How to find out why a portfolio was rejected

Find out why `optim` rejected a portfolio, or why it traded as it did, by reading its reasons and deciding it again
from its run's snapshot, with the code you have now and a breakpoint where you need one. A replay reads only the
snapshot, so it decides on exactly the data the run saw, and touches no source.

Prerequisites:

- The run's id, the first line of what the run printed, such as `run 01a169a8-29f0-7511-b24d-c60bc4ad6051`, and the
  portfolio's id.
- Read access to the run's snapshot. A run from `main.py` saves it in the repo's `snapshots/`. A production run saves
  it under production's snapshot root: set that root as `OPTIM_SNAPSHOT_ROOT` in your `.env.prod`, with read-only
  access (see [how to run a model on live data](run-on-live-data.md)).

To find out why a portfolio was rejected:

1. Read its reasons. A run prints them below its table, one line each:

   ```text
   run 01a169a8-29f0-7511-b24d-c60bc4ad6051
   portfolio_id   status  trades  solved  objective       stop  seconds
             P1 rejected       0   False        NaN infeasible 0.030571
             P2 accepted       0    True        0.0    optimal 0.028045
   P1: not solved: infeasible
   ```

   The same reasons are in `results.reasons`, from a run in `main.py`, and in the snapshot's `reasons.parquet`, one
   row per reason. The table below says what each of the core's reasons means; any other reason is the model's own,
   from its `check`.
2. Decide the portfolio again. In the model's `main.py`, import `replay` instead of `run`, and replace the call with
   one that names the run and the portfolio:

   ```python
   from optim import replay

   from models.income import model

   if __name__ == "__main__":  # each worker process the run starts imports this file, and must not start a run itself
       results = replay(model, "01a169a8-29f0-7511-b24d-c60bc4ad6051", portfolios=["P1"], env=".env.prod")
   ```

   Run the file. The replay finds the run in the repo's `snapshots/`, or else under the settings file's
   `OPTIM_SNAPSHOT_ROOT`, decides P1 again with the run's params and the code you have now, prints what the run printed
   for it, and saves itself as a new run in the repo's `snapshots/`:

   ```text
   run 01a169a8-971e-71ff-9b26-f57d7da58213
   portfolio_id   status  trades  solved  objective       stop  seconds
             P1 rejected       0   False        NaN infeasible 0.030742
   P1: not solved: infeasible
   ```

   For a model that decides portfolios in turn, the portfolios before P1 in its order decide again too, since what they
   reserve is what P1 sees. In place of an id, `run` may be a snapshot's absolute path or its URL.
3. Look inside. Set a breakpoint on the line a reason names, or in the rule or optimizer code you suspect, and debug
   `main.py`. The replay decides in this process, where the debugger stops, unless `OPTIM_WORKERS` is above 1; pass
   `workers=1` to be sure.
4. Optional: to see what the portfolio was decided on without a breakpoint, decide it from Python with
   `optim.research.trial`, which hands back the portfolio as `prepare_portfolio` returned it, the market, the cvxpy
   problem and the outcome. Run it from the repo's root:

   ```python
   from optim.research import trial
   from optim.runs import open_snapshot
   from optim.storage import Root

   found = trial(open_snapshot(Root.at("snapshots/01a169a8-29f0-7511-b24d-c60bc4ad6051")), "P1")
   print(found.outcome.reasons, found.problem.status)
   print(found.portfolio.universe[["security_id", "held", "buyable", "sellable"]])
   ```

   ```text
   ['not solved: infeasible'] infeasible
     security_id      held  buyable  sellable
   0           B  100500.0     True      True
   1           A       0.0     True     False
   2           C       0.0    False     False
   3           D       0.0    False     False
   4           E       0.0    False     False
   ```

5. Tell the data from the model. The snapshot's `inputs/` holds every dataset as the run loaded it, one Parquet file
   each, such as `inputs/holdings.parquet` and `inputs/controls.parquet`; `run.json` holds the params the run decided
   with. Compare the portfolio's rows with what the model assumed.
6. Try a fix. Replay with other params, `params="params/wider.json"`, or with a change to the model's code, and run the
   file again. In the example above, P1's params capped turnover at 1%, while selling B under its 5% cap needs about
   4%; with `params="params/default.json"`, the replay accepts P1.

## What the core's reasons mean

| Reason | Why |
|---|---|
| `not solved: infeasible` | No trades meet every constraint: two that contradict, such as a turnover cap smaller than the sale a weight cap forces, or a control that forbids the sale a cap needs. |
| `not solved: limit` | The solve stopped at its time limit, `solve.time_limit_s` in the params, before proving its answer optimal. Its trades are in the snapshot, but may not trade. |
| `not solved: imprecise`, `unbounded` or `failed` | The solver doubts its answer, or found the problem unbounded, or failed: usually a constraint or objective term built wrong. |
| `not solved: the model says so` | The model's `decide` returned a decision with `solved=False`. |
| `KeyError: 'matures', at models/income/rules/maturity.py:14 in max_years` | A hook raised: the error, and the last line of the model's own code it passed through. The run's log holds its whole traceback. |
| `rounding moved its buy of A from 1,499.00 to 1,000.00: decide whole increments` | The decision was not in whole trading increments, so rounding it changed it, and it might no longer meet its constraints. |
| `sells 100,503.00 of B, more than its holding of 100,500.00` | A sale larger than the holding. |
| `leaves cash of -2.00: it spends more than it has` | Buys that cost more than the cash and the sales provide. |
| `trades securities that are not loaded: ['X']` | A trade in a security the run's securities do not hold. |
| `buys quantities that are not positive: ['A']` | A quantity of zero or less. |
| `it has no holdings: is its id right?` | The holdings hold nothing for the portfolio, as for a misspelled id. |
| `not one of the portfolios the model lists on 2026-10-16` | The portfolio was given by id, and the model's `portfolios` dataset does not list it on the business date. |
| `error: its worker process died` | The process deciding it died, as when it ran out of memory. |

For the snapshot's files and the result tables' columns, see the [reference](../reference.md).
