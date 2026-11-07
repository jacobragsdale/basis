# Spike 0.3: eval

`evals/` holds five tasks against etl-framework 0.1.0, each graded by the generated repo's own checks plus assertions of
its own, a runner that drives them through Harbor, and the report. Every task passed validation in Harbor: its nop run
fails and its reference solution passes. The agent trials have not run. Harbor's Claude Code adapter uses the
subscription only through a long-lived `claude setup-token` token, and this host has none, so there is no first report
of pass rates, loop times or tokens yet. Run on 2026-11-07.

## Versions

Harbor 0.25.0, run with `uvx --from harbor==0.25.0`, on Docker 29.9.0 with Compose 5.6.0, and uv 0.13.6 on the host.
Each task's image is `python:3.14.7-slim-trixie` with uv 0.13.7, holding a repo made by `etl new repo` from
etl-framework's tag `v0.1.0` (commit `dd2bab8`). The repo's checks are its pinned ruff 0.17.4, ty 0.0.99 and pytest
9.2.0, with polars 2.1.0. Harbor's `claude-code` adapter installs Claude Code 2.1.319, the host's version, in each trial.
The runner's default model is `claude-sonnet-5-5` at effort `high`, with Claude Code's web tools off.

## The tasks

Each task is something a developer's agent does in a job repo. Its instruction reads as a developer would write it, and
ends "When you're done, the repo's checks should pass", without naming the checks.

| Task | The agent is asked to |
|---|---|
| `etl-new-job` | add a `security_master` job from a short spec: one table read whole, written with `Replace()`, refusing two active securities with one ISIN |
| `etl-add-output` | add an `account_totals` output to the `positions` job, upserted by trade date and account |
| `etl-add-param` | add an `exclude_securities` param to the `positions` job, with a description `etl describe` shows |
| `etl-fix-check` | fix the `positions` job, whose check refuses its own fixture, without changing the fixture, params or tests |
| `etl-port-script` | port a pyodbc nightly script into a `daily_cash` job that does what it did |

A trial passes when the repo's checks pass (`uv sync --locked`, pytest with the contract tests, `ruff check`,
`ruff format --check` and `ty check`), the task's hidden assertions pass, and the agent left the protected files and
the `[tool]` tables of `pyproject.toml` alone. No task needs a database: the assertions build fixtures of their own and
run the job through the framework's own run loop. [evals/README.md](../../evals/README.md) has the commands and the
checks in full.

## Answers

| Question | Answer |
|---|---|
| Does Harbor install and run here? | Yes. Harbor 0.25.0 runs through `uvx`, builds each task's image, and cleans up after each trial with `docker compose down --rmi local` on its own project, so no containers or images are left. A trial without an agent takes about 20 s, of which the image build from cache takes 3 to 4 s and the checks 2 to 3 s. |
| Can five tasks against etl-framework 0.1.0 be graded by its own checks? | Yes. In Harbor, every task's nop run scored 0 and its reference solution scored 1. Locally, its assertions rejected 15 plausible wrong solutions, such as `>` for `>=` at an interval's end, a port that counts settled movements only, and `Upsert()` for `Replace()`, and accepted three valid alternatives to the reference, such as a `list` param with `default_factory` and a table named without its schema. |
| Can Harbor drive Claude Code on the subscription? | No, not yet: there is no token. Everything up to the token works. With an invalid token, Harbor installed Claude Code 2.1.319 in the container and ran it with `--effort high`, web tools off and `bypassPermissions`; the CLI reported `apiKeySource: none`, sent the token as a bearer token, got `401 authentication_failed`, spent no tokens, and the verifier still graded the trial (reward 0). The token went in Harbor's own environment, and appears in no file Harbor wrote. |
| What are the pass rate, consistency, loop time and tokens? | Not measured, since no trial ran. The check suite alone (pytest, ruff and ty) took 1.0 to 2.3 s in the oracle runs, against the design's one-minute budget for the commit hook. Installing Claude Code adds about 84 s to each trial, outside the loop time. |
| Which agents can the Harbor setup drive? | The design lists this as unverified. harness-evals' notes from 2026-10-16 record pi, opencode, oh-my-pi, DeepSeek Harness and the Copilot CLI driven through Harbor 0.24.0 on an API key, with all 100 of their trials passing; this spike did not run them again. Claude Code on the subscription is the open one. |

## Fallbacks

**Claude Code on the subscription.** The maintainer runs `claude setup-token` once and saves the token to
`~/.config/harness-evals/claude-oauth-token`, mode 600; then `uv run evals/run.py trials` and `uv run evals/report.py`
make the first report, with no other change. If no long-lived token can be had, the fallbacks are, in order:

1. Claude Code on an API key, through harness-evals' usage-logging proxy with its spend cap. This is paid, per
   token.
2. Claude Code on the host's own login, outside Harbor, with `claude -p` in a scratch clone of each task's repo. It
   costs nothing, but the agent then runs on the home server itself rather than in a container.

**The first report.** Until the trials run, the report holds the task validation only.

## What building the tasks showed

- **The generated repo's agent files list only pytest as a check.** `AGENTS.md`'s commands and the README give `uv run
  pytest`. ruff and ty appear only in the `etl-new-job` skill's last step and in `.pre-commit-config.yaml`, and the
  repo installs no commit hook. An agent that reads `AGENTS.md` alone can stop with ruff or ty failing. The tasks' "the
  repo's checks should pass" measures that. basis's `basis check`, listed in `AGENTS.md`, closes it.
- **Claude Code sees none of the repo's skills.** The repo has `AGENTS.md` and `.agents/skills/`, but no `CLAUDE.md`
  and no `.claude/skills/`, which roadmap step 1.6 adds. Whether Claude Code 2.1.319 loads `AGENTS.md` without a
  `CLAUDE.md` was not checked: its stream lists a built-in `cc-plugin-agents-md` plugin, which suggests it does.
- **The checks are fast.** At 1 to 2.3 s for the whole suite on a repo with two jobs, the one-minute budget leaves room
  for `regress` and many more jobs before tests need to move to the push hook.
- **This repo's checks now cover `evals/`.** The root `pyproject.toml` excludes the task environments (an agent's
  starting point, such as the legacy script) and the reference solutions (job-repo code that the task's own checks
  lint with the job repo's settings) from ruff, lets the eval's scripts print, and excludes the tasks' tests from ty,
  since they import modules that exist only inside a task's container.
