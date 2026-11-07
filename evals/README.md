# The eval

Five tasks that a developer's agent does in a job repo, each run in a fresh repo from `etl new repo` on etl-framework
0.1.0 and graded by that repo's own checks plus the task's own assertions. [Harbor](https://github.com/harbor-framework/harbor)
runs each trial in a container, and the agent is Claude Code on the maintainer's subscription. The report gives the pass
rate, how consistently each task passes across trials, the loop time and the tokens. Spike 0.3's result is in
[docs/spikes/eval.md](../docs/spikes/eval.md).

## How to run the eval

You need uv, Docker, and a Claude subscription token. Trials run one at a time, because parallel trials skew each other's
loop times. Each trial first installs Claude Code in its container, which took 84 s here, before the agent starts.

1. Make a long-lived token, once. `claude setup-token` signs in to the subscription in a browser and prints it; save
   it to `~/.config/harness-evals/claude-oauth-token` with mode 600. harness-evals reads the same file.

   ```sh
   claude setup-token
   ```

2. Check that every task can be trusted: its nop run, which changes nothing, must fail, and its oracle run, the
   reference solution, must pass.

   ```sh
   uv run evals/run.py validate
   ```

   A task that fails this prints an `ERROR` line naming it; fix its tests or its solution before running trials.
3. Run three trials of every task:

   ```sh
   uv run evals/run.py trials
   ```

   `--tasks` runs some tasks only, `--trials` sets the number per task, and `--model` and `--effort` choose what Claude
   Code runs (by default `claude-sonnet-5-5` at `high`). Without the token, the command prints an `ERROR` line and runs
   nothing; it never falls back to an API key.
4. Write the report, `evals/results/report.md`, and every trial's numbers, `evals/results/trials.json`:

   ```sh
   uv run evals/report.py
   ```

Each trial is a Harbor job under `evals/jobs/`, named `AGENT__TASK__N`, with the agent's transcript, the verifier's
output and Harbor's `result.json`. A trial with a `result.json` is skipped when the command runs again, so a stopped run
resumes; to run a trial again, delete its folder.

## How a trial is graded

Each task's `tests/test.sh` runs every check, whatever fails first, and the trial's reward is 1 only when every check
passes. Harbor records each check's result in `result.json`, and the verifier's logs hold each check's output and
milliseconds.

| Check | Passes when |
|---|---|
| `lock` | `uv sync --locked` succeeds: `uv.lock` matches `pyproject.toml`. |
| `pytest` | The repo's tests pass, including every job's contract tests and the agent-files test. |
| `ruff_check`, `ruff_format` | `ruff check .` and `ruff format --check .` pass, with the repo's settings. |
| `ty` | `ty check` passes, with every rule an error. |
| `task` | The task's own assertions in `tests/test_task.py` pass. The agent never sees them. |
| `untouched` | The files in `tests/protected.txt`, and the `[tool]` tables of `pyproject.toml`, are as the task began. |

## The tasks

| Task | The agent is asked to | The assertions check |
|---|---|---|
| `etl-new-job` | Add a `security_master` job from a short spec: a table read whole, written with `Replace()`, refusing a shared ISIN | the entry point, the declared input and output, the column types, contract tests, and runs on fixtures of the test's own |
| `etl-add-output` | Add an `account_totals` output to the `positions` job, upserted by trade date and account | the declared output and its key, the totals, closed accounts, and `etl ddl` |
| `etl-add-param` | Add an `exclude_securities` param to the `positions` job, with a description | `etl describe`, params read from JSON, and both outputs with and without it |
| `etl-fix-check` | Fix the `positions` job, which refuses its own fixture, without changing the fixture, params or tests | that its check passes good data over a several-day interval and still stops late positions, unknown accounts and bad sides |
| `etl-port-script` | Port `/legacy/daily_cash.py`, a pyodbc script, into a `daily_cash` job that does what it did | the query's binds, the delete of the interval, the totals, a cancelled movement, an empty day, and the script's currency check |

Every task's image starts from the same lines: `uvx --from git+https://github.com/jacobragsdale/etl-framework@v0.1.0
etl new repo trades-etl positions`, moved to `/app`, committed and tagged `eval-base`. Claude Code's web tools are off,
since this repo, which holds the solutions, is public.

## What the report measures

- **Pass rate**: trials whose reward is 1, over all trials.
- **Consistency**: how many tasks passed in every trial, in some, and in none. A task no trial passes is more often a
  broken task than a hard one; check its validation and its transcripts first.
- **Loop time**: for a passing trial, from the agent's start to the end of the checks it passed, which is the time from
  a task to a change that every check accepts. Agent time and verifier time are reported apart.
- **Check suite**: the repo's own checks alone (pytest, ruff and ty), the loop a commit hook would run, against the
  design's budget of a minute.
- **Tokens**: Claude Code's prompt tokens (cache reads and writes included), cache reads and output tokens, and the API
  price of those tokens, which the subscription does not charge.
- **Check runs**: how many of the agent's shell commands ran pytest, ruff, ty or pre-commit.

## How to add a task

1. Copy a task folder under `tasks/`. Its Dockerfile's lines down to the task's own stay as they are, so every image
   shares them.
2. Write `instruction.md` as a developer would ask, `solution/solve.sh` as the reference, and `tests/test_task.py`
   with the assertions; keep `tests/test.sh` the same as every other task's.
3. Run `uv run evals/run.py validate --tasks NAME` until its nop run fails and its oracle passes.
