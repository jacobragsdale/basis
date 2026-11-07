"""Decide portfolios with this model: press Run, or Debug to stop at a breakpoint in a rule or a hook. The run prints a
row per portfolio, saves its snapshot in the repo's `snapshots/`, and never writes orders; `results` holds its
decisions, trades, reasons and metrics as tables. Paths are this folder's, and `env` names the settings file, at the
repo's root, that says where each dataset comes from: `.env`, `.env.qa` or `.env.prod`.

To run on the fixture, with no database, add `data="tests/fixture"`. To decide a saved run again with the code you have
now, as when a portfolio was rejected, call this instead:

    replay(model, "0193f2...", portfolios=["P1"], env=".env.prod")

To compare params files on one saved run, `table = sweep(model, "0193f2...", params=["params/default.json", ...])`.
`replay` and `sweep` come from `optim`, as `run` does."""

from optim import run

from optim.example import model

if __name__ == "__main__":  # each worker process the run starts imports this file, and must not start a run itself
    results = run(model, params="params/default.json", portfolios=None, env=".env")
