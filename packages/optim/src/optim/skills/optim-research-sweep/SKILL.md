---
name: optim-research-sweep
description: Run a research sweep of params variants over a saved optim run and compare the outcomes, without touching production data. Use when asked to try, tune, compare or sweep parameters, constraints or limits of a model.
---

# Run a research sweep

Follow `uv run optim docs how-to/compare-params-with-a-sweep`. In short:

1. Learn what the model takes and reports: `uv run optim describe MODEL` prints its params schema, datasets and
   metrics as JSON.
2. Pick a saved run to sweep: a run id in the repo's `snapshots/`, or under the settings file's `OPTIM_SNAPSHOT_ROOT`,
   such as production's in `.env.prod`.
3. Write one params file per variant beside `default.json`, in `models/MODEL/params/`, each a whole, valid params file.
   A variant changes params alone, such as a limit or a term's weight; a new constraint or term is code (see
   `uv run optim docs how-to/add-a-constraint`). Params are strict: a misspelled key fails the sweep before anything
   decides.
4. Sweep from Python, `sweep(model, RUN, params=[...])`, or `uv run --env-file .env optim sweep RUN DIR --out
   sweep.csv`. Each row is a variant and a portfolio: whether it was accepted, its objective, turnover, trades, cash,
   solve time, the model's metrics and its reasons.

A sweep reads only the snapshot and saves nothing. `uv run optim docs reference` describes the sweep table.
