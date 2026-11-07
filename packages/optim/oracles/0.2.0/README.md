# Oracle: optim 0.2.0's example model on its fixture

This folder is roadmap step 3.1's oracle: what optim 0.2.0 decided when its example model ran on the model's fixture,
kept so that step 3.6 can show that the ported optim decides the same. The fixture is synthetic.

The run was made on 2026-11-07 (UTC) with optim 0.2.0 at portfolio-optim commit
[`69bc6b4`](https://github.com/jacobragsdale/portfolio-optim/commit/69bc6b4e6cf428afcda4450abfc18dfdb51c0268), with no
changes, from a model repo generated from that checkout. It used uv 0.13.6, Python 3.14.7, cvxpy 1.9.3, highspy 1.15.1
(HiGHS), numpy 2.5.4, scipy 1.18.1, pandas 3.1.1, pyarrow 26.0.1 and pydantic 2.14.3; `scratch-repo.uv.lock` holds
every version.

## What it decided

Two portfolios, both accepted, none rejected, as of the fixture's date, 2026-10-16, with the example's default params:

| Portfolio | Status | Trades | Reasons |
|---|---|---|---|
| P1 | accepted | buy 54,000 A; buy 55,000 E; sell 46,000 B | none |
| P2 | accepted | buy 24,000 A; buy 19,000 C; buy 25,000 E | none |

These are the trades optim's tutorial shows for the same run. They exercise the example's rules and its integer
program: P1 sells B down under the 5% cap per bond, neither portfolio buys D, which is rated below `min_rating`, P1
buys no C, which its `no_buy` control forbids, and P2 buys no B, which is below its own AA- floor, and only 19,000 of C,
under its 4% Financials mandate.

The oracle does not exercise everything optim does. No portfolio is rejected, so no reason is recorded; rounding moved
no trade, since each `decided` equals its `quantity`; the run decided in one process, at once rather than in turn; and
a run from Python writes no orders. A port that broke any of those could still match this oracle, so they rest on
optim's own tests.

## Files

| Path | Holds |
|---|---|
| `snapshot/01a1b494-791e-7032-b8f4-c80e7e00dda8/` | The run's snapshot as optim 0.2.0 saved it: `run.json`, `inputs/` with each dataset as loaded, and the result tables `decisions`, `trades`, `reasons` and `metrics`, as Parquet. It is unchanged but for a final newline in `run.json`, which this repo's hooks add. |
| `decisions.json` | What must match exactly: the as-of date, the portfolios in the order they were decided, each one's status and reasons, and every trade that rounding left, by security and side. |
| `details.json` | What must match within 1e-9, relative or absolute, with its names and flags exactly: each decision's value, cash, turnover, objective and metrics, whether it solved, its solver and how the solve stopped, and every trade as the model decided it before rounding. |
| `fixture/` | The fixture the run read, a copy of `src/optim/example/tests/fixture/` at that commit. |
| `scratch-repo.uv.lock` | The generated model repo's lockfile. Named so that no tool takes it for this repo's. |
| `record.py` | Writes `decisions.json` and `details.json` from a snapshot, and compares a snapshot with them. |

The paths in `run.json` are the scratch repo's on the host that made the run, and its `code` is null because the
scratch repo had no commit yet. Neither is compared.

## Reproduce the run

You need uv, git and a clean checkout of portfolio-optim.

1. In the portfolio-optim checkout, check out the oracle's commit and generate a model repo whose first model is
   `income`, taking optim from the checkout:

   ```sh
   git checkout 69bc6b4e6cf428afcda4450abfc18dfdb51c0268
   uv run optim new repo sma-optimizer income --optim-path . --dir SCRATCH
   ```

   `SCRATCH` is an empty folder.

2. Install the oracle's versions in the new repo:

   ```sh
   cd SCRATCH/sma-optimizer
   cp BASIS/packages/optim/oracles/0.2.0/scratch-repo.uv.lock uv.lock
   uv sync --locked
   ```

   `BASIS` is this repo's checkout. The lockfile names the portfolio-optim checkout at `/home/jacob/dev/portfolio-optim`;
   for a checkout elsewhere, replace that path in the copied `uv.lock` with the checkout's before `uv sync --locked`.

3. Run the model on its fixture, as optim's tutorial does: in `models/income/main.py`, add `data="tests/fixture"` to
   the `run(...)` call, so that it reads

   ```python
       results = run(model, params="params/default.json", portfolios=None, env=".env", data="tests/fixture")
   ```

   and run the file:

   ```sh
   uv run python models/income/main.py
   ```

   After its log lines, the run prints its id and a row per portfolio. Your id and times will differ:

   ```text
   run 01a1b494-791e-7032-b8f4-c80e7e00dda8
   portfolio_id   status  trades  solved  objective    stop  seconds
             P1 accepted       3    True   0.006741 optimal 0.029628
             P2 accepted       3    True   0.006447 optimal 0.021891
   ```

4. From this repo's root, compare the run's snapshot with the oracle, where `RUN_ID` is the id the run printed:

   ```sh
   uv run packages/optim/oracles/0.2.0/record.py compare SCRATCH/sma-optimizer/snapshots/RUN_ID
   ```

   It prints `matches the oracle` and exits 0, or prints each difference and exits 1.

`uv run packages/optim/oracles/0.2.0/record.py record SNAPSHOT packages/optim/oracles/0.2.0` writes the two JSON files
again from a snapshot.

## The oracle is deterministic

Two runs, each in a freshly generated repo with the same lockfile and in a process of its own, gave byte-identical
`decisions.json` and `details.json`. Every Parquet file in their snapshots but `decisions.parquet` was byte-identical,
and `decisions.parquet` differed only in the timing columns `seconds` and `solve_seconds`. Their `run.json` files
differed only in the run id, the times and the scratch folder. A third run, made by following
[Reproduce the run](#reproduce-the-run) from a fresh clone of portfolio-optim in another folder, matched too. optim 0.2.0's own regress over this snapshot,
`optim regress income --snapshots BASIS/packages/optim/oracles/0.2.0/snapshot` from the generated repo, replayed both
portfolios and listed no change.

## Compare the ported optim

Step 3.6 is done for this oracle when the ported optim passes both checks below. First confirm it is asked the same
question: `diff -r` of this folder's `fixture/` and the ported example's `tests/fixture/` prints nothing, and the run's
params, defaults included, equal `params` in the oracle's `run.json`. A run on another fixture or other params says
nothing about the port.

1. **A fresh run decides the same.** Generate a model repo from the basis checkout with its first model named `income`,
   run that model on its fixture from `main.py` as in step 3 above, and compare its run with `record.py compare`. It
   must print `matches the oracle`. The ported optim saves its runs in the run store, a manifest at
   `runs/optim/income/RUN_ID.json` naming `objects/HASH.parquet`, so give `compare` the manifest,
   `SCRATCH/sma-optimizer/snapshots/runs/optim/income/RUN_ID.json`; `record.py` reads both layouts, through a `load`
   for each that returns the same `Tables`.
2. **`basis regress` over this snapshot lists no change.** From the generated repo, regress over this folder's
   `snapshot/` must replay the run with the ported code, list nothing and exit 0. It finds the run by its model's name,
   `income`, or its module, `models.income.model`, so the generated repo's model must keep that name. If the ported
   optim cannot read an optim 0.2.0 snapshot, convert this one into the run store first, check that the conversion
   keeps every dataset and result table's values, and regress over the converted run.

Regress compares only each portfolio's status and its rounded trades, so the first check is the one that covers the
reasons, the as-of date, the order and the numbers. Together they compare this:

| Must match | Fields |
|---|---|
| Exactly | `decisions.json`: the as-of date; the portfolios and their order; each one's status; its reasons, as a sorted list; and its trades with a nonzero rounded quantity, by security and side, with that quantity. |
| Within 1e-9, relative, or absolute near zero | `details.json`'s numbers: value, cash, turnover, objective, each metric, and each trade's rounded and decided quantity. Its other fields, `solved`, `solver`, `solver_status` and `stop`, exactly. |
| Not compared | The run id; every time, including `seconds` and `solve_seconds`; paths and dataset sources; `labels`, `code` and `workers`; the solver's `gap`, `nodes` and `iterations`, which change with its version; and the Parquet files' column types. |

If a check fails, run the ported optim again with the oracle's versions, from `scratch-repo.uv.lock`, before reading
the difference as the port's. A difference that goes away came from a dependency: a newer HiGHS, for one, may stop at
a different optimum of equal objective. If the port must ship on the new versions, remake the oracle by this procedure
with optim 0.2.0 on those versions, and keep the comparison as tight as it is.
