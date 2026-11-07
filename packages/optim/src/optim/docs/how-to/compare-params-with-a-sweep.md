# How to compare params with a sweep

Decide one saved run once per params file, and compare the outcomes in one table, to tune a limit, try a constraint or
weigh an objective term without touching live data. A sweep reads only the run's snapshot and saves nothing, so it can
try a hundred variants on production's data from your machine.

Prerequisites:

- A saved run of the model to sweep: its id, in the repo's `snapshots/` or under the settings file's
  `OPTIM_SNAPSHOT_ROOT`, such as production's in `.env.prod`. Any run works, a replay included; a replay of some
  portfolios sweeps those alone.
- The model's params, which `uv run optim describe MODEL` prints as a JSON schema, with each field's description.

To sweep params files:

1. Write one params file per variant, beside `default.json` in `models/MODEL/params/`. Copy `default.json` and change
   what the variant tries: each file is a whole params file. A variant may change any param, such as a looser
   Financials cap in `params/loose.json`:

   ```json
     "sector_caps": {"Financials": 0.04},
   ```

   Params are strict: every file is validated before any variant decides, so a misspelled key or a number written as a
   string fails the sweep at once, naming the file.
2. In the model's `main.py`, import `sweep` instead of `run`, and replace the call with one that names the run and the
   files, which are the model's folder's:

   ```python
   from optim import sweep

   from models.income import model

   if __name__ == "__main__":  # each worker process the run starts imports this file, and must not start a run itself
       table = sweep(model, "01a169a6-1c9f-7638-8326-80b6ee410ff7", params=["params/default.json", "params/loose.json"])
   ```

   Pass `env=".env.prod"` for a run under production's snapshot root, and `workers=8` to decide in parallel.
3. Run the file, and look at `table` in your editor's debugger, or from a terminal with
   `uv run python -i models/MODEL/main.py`:

   ```text
   >>> table[["variant", "portfolio_id", "accepted", "objective", "turnover", "trades", "income"]]
      variant portfolio_id  accepted  objective  turnover  trades     income
   0  default           P1      True   0.003340  134498.0       2  3744.8105
   1  default           P2      True   0.003359   33492.0       2  1696.2240
   2    loose           P1      True   0.004174  112344.0       2  4653.1245
   3    loose           P2      True   0.004296   43572.0       2  2169.9840
   ```

   The table is a pandas `DataFrame`: `table.groupby("variant")["income"].sum()` totals each variant, and
   `table.to_csv("sweep.csv", index=False)` keeps it.

The table has a row per params file and portfolio, with these columns:

| Column | Holds |
|---|---|
| `variant` | The params file's name, without `.json`. |
| `portfolio_id` | The portfolio. |
| `accepted` | Whether the decision passed the core's checks and the model's: whether it could trade. |
| `objective` | The objective's value at the solution, in the model's own units; empty where the solve found none. |
| `turnover` | The value bought plus the value sold, after rounding, in the portfolio's currency. |
| `trades` | How many securities it buys or sells. |
| `cash` | Cash after the rounded trades. |
| `solve_seconds` | Time in the solver itself. |
| `seconds` | Time to prepare, decide and check the portfolio. |
| one per metric | Each number the model's `Metrics` declares, such as the income example's `income` and `cost`. |
| `reasons` | Why it was rejected, the reasons joined by `; `; empty when accepted. |

## Sweep from the command line

`optim sweep` sweeps every params file in a folder and writes the same table as CSV, which suits an agent or a
pipeline. Put the variants in a folder of their own, such as `models/income/params/sweep/`, then:

```sh
uv run --env-file .env optim sweep RUN_ID models/income/params/sweep --out sweep.csv
```

`RUN_ID` is a run under `OPTIM_SNAPSHOT_ROOT`, which `.env.example` sets to the repo's `snapshots/`, or a snapshot's
path. Without `--out`, the table is printed as CSV; `--out` writes a `.csv` or `.parquet` file, by its suffix.

To look inside one portfolio of one variant, replay it with that variant, as
[how to find out why a portfolio was rejected](find-out-why-a-portfolio-was-rejected.md) shows.
