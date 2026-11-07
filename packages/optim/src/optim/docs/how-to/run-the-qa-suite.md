# How to run the QA suite

Run each model of a repo against the QA environment, through the deployed image's `optim`, and triage what fails. The
e2e suite in `tests/e2e/` does it, and `optim`'s `optim_qa` fixture refuses to run it anywhere but QA.

Prerequisites:

- The repo's image, which the pipeline builds from its `Dockerfile` and deploys to QA, or a checkout of the repo with
  `uv sync` run. A laptop counts as QA when every setting points at QA.
- QA's `OPTIM_*` settings, with read-only credentials for its sources, in `.env.qa` at the repo's root, which git
  ignores. Copy `.env.example` to start it; the [settings reference](../reference.md#settings) names each variable, and
  the environment's owner has their values. QA's settings include `OPTIM_SNAPSHOT_ROOT`, where the suite's runs save.

To run the e2e suite:

1. In `pyproject.toml`'s `[tool.pytest.ini_options]`, which the repo already has, list what the suite may write to,
   exactly as `.env.qa` sets them: `OPTIM_SNAPSHOT_ROOT`, and `OPTIM_ORDERS__TABLE` and `OPTIM_CACHE_ROOT` where it
   sets them.

   ```toml
   [tool.pytest.ini_options]
   optim_qa_markers = ["qa"]
   optim_e2e_writes = ["//qa-share/optim/snapshots", "qa.orders"]
   ```

   `optim_qa_markers` is text, not a pytest marker: every QA host, connection string, URL and root must contain one of
   its entries, ignoring case. Change it if QA's names carry another.
2. Check which portfolios each model decides. `tests/e2e/test_smoke.py`, which the repo already has, decides the first
   three portfolios each model lists in its `portfolios` dataset, read from QA, so the cases follow QA's data rather
   than fixed ids, which drift. For a model that lists none, pick its portfolios by what they hold, with a `SELECT` that
   returns `portfolio_id`, in a SQL file such as `tests/e2e/sql/portfolios.sql`, and pass
   `optim_qa.portfolios(Path(__file__).parent / "sql" / "portfolios.sql")` instead of `optim_qa.listed(model)`. It runs
   on SQL Server by default, in a read-only transaction.
3. Point the suite at the deployed image: set `optim_e2e_command`, in the same table, to the command that runs its
   `optim`, such as `["docker", "run", "--rm", "--env-file", ".env.qa", "IMAGE"]`; the image runs `optim`, under `tini`, with the arguments it is given.
   The suite appends `run MODEL --params models/MODEL/params/default.json --portfolios ... --label purpose=e2e`, and
   later `replay RUN_ID --label purpose=e2e`, and runs the command from the repo's root, so the relative params path
   resolves in the image too, whose workdir holds the repo. Without the setting, the suite runs this checkout's
   `optim`.
4. Run the suite with QA's settings:

   ```sh
   uv run --env-file .env.qa pytest -m e2e
   ```

You write no test code: `tests/e2e/test_smoke.py` gives every model the repo lists a smoke case. The run completes,
each portfolio is accepted or rejected with reasons, and replaying the snapshot gives the same trades for the decisions
that did not stop at a time limit. The suite passes when every case does, with one passed test per model. A plain
`uv run pytest` leaves the suite out.

If the suite reports `refusing to run the e2e suite outside QA`, a configured source or destination lacks the marker,
or a write is not on `optim_e2e_writes`. The message names each one, such as
`OPTIM_SNAPSHOT_ROOT (//qa-share/optim/snapshots) is not in optim_e2e_writes []`. Fix the settings, never the
safeguard.

## How to triage an e2e failure

Sort the failure before changing anything:

1. **Refused to run.** The environment is not QA's, or writes outside the allowlist. Fix the environment.
2. **The run did not complete.** The failure message ends with the run's stderr, its JSON log lines; every line
   carries the run's `run_id`. The `error` record names the root cause, the notes added on the way up, and `where` it
   surfaced. A source that timed out, or refused a login, is a QA outage: rerun once QA is back.
3. **A portfolio was rejected without reasons.** That is a bug in the model or in `optim`: report it with the run id.
4. **A portfolio was rejected with reasons**, which the smoke case accepts. Its reasons are in the snapshot's
   `reasons.parquet`, under QA's `OPTIM_SNAPSHOT_ROOT`; a hook's error names the line of model code that raised it, as
   in `KeyError: 'sector', at models/income/rules/screens.py:14 in exclude_sectors`. To look inside, decide the
   portfolio again from the model's `main.py`, with `.env.qa`, which tells `replay` where QA's snapshots are:

   ```python
   results = replay(model, "RUN_ID", portfolios=["PORTFOLIO_ID"], env=".env.qa")
   ```

   Replace `RUN_ID` and `PORTFOLIO_ID`, and import `replay` from `optim`. The replay reads only the snapshot, saves in
   the repo's `snapshots/`, and stops at any breakpoint you put in a rule or the optimizer.
   [How to find out why a portfolio was rejected](find-out-why-a-portfolio-was-rejected.md) goes further. If the data
   is wrong, as when QA's prices are stale, it is data drift: report it to QA's owner. If the model is wrong, fix it,
   with a behaviour test that reproduces the case first.
5. **The replay differs.** A decision that stopped at its time limit may stop elsewhere and is left out of the
   comparison. Any other difference means the model reads something outside its snapshot, such as the clock or a
   random number without a seed: make it read the run's `as_of_date`, or a seed from its params.

Report the run id, the portfolios and their reasons. Never weaken a check or an assertion to make the suite pass.

For the ini options and the `optim_qa` fixture's methods, see the [reference](../reference.md#the-pytest-plugin).
