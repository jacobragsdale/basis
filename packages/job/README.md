# basis-job

The run loop that ETL jobs share. A job is a folder of plain Python whose `job.py` declares what it reads, what it
writes and how, and transforms inputs to outputs in a pure function. basis-job runs everything else the same way for
every job: reading from SQL Server, Oracle and files, checking every output against its row and its destination
table, saving a snapshot that replays, and publishing each database in one transaction, recorded in a ledger. Jobs live
in their own repos, which depend on basis-job, and run in a container.

```python
class Inputs(etl.Inputs):
    trades = etl.Query(Trade, database="source", sql="sql/trades.sql", empty="allow")
    accounts = etl.Query(Account, database="source", sql="sql/accounts.sql", empty="fail")


class Outputs(etl.Outputs):
    positions = etl.Table(DailyPosition, database="warehouse", table="daily_positions", key=("trade_date", "account_id", "security_id"), write=etl.ReplaceWhere(interval="trade_date"))


def transform(inputs: Inputs, params: Params) -> Outputs: ...
```

## Start a job repo

You need [uv](https://docs.astral.sh/uv/) and git.

basis has no release yet, so a job repo takes basis-job from the commit of basis that `uvx` installs from GitHub:

```sh
uvx --from 'git+https://github.com/jacobragsdale/basis#subdirectory=packages/job' basis-job new repo pricing-etl prices
cd pricing-etl
uv run basis check
```

To work on basis itself, make a repo from a checkout instead, which it installs editable:
`uv run --project basis basis-job new repo pricing-etl prices --framework-path basis`.

The repo holds one job, `jobs/prices/`, copied from the package's example: daily positions from trades. Around it are
a README that maps each kind of change to the file it goes in, a `.env.example`, and the framework files `basis sync`
keeps current: the `Dockerfile`, the deploy pipeline in `azure-pipelines.yml`, the commit and push hooks in
`.githooks/`, which `basis check`, `sync` and `new` turn on, `.gitattributes`, and the agent files. Its `pyproject.toml` declares the job part, as `[tool.basis] parts = ["job"]`, so its one
command, `basis`, offers the job verbs: `uv run basis --help` lists them, and `uv run basis new job NAME` adds a job.
To take every package from an internal package index instead, pass `--index URL`: it replaces PyPI for the whole repo,
so it must proxy PyPI.

## Documentation

The docs ship with the package, so a job repo reads the ones for the version it installed: `uv run basis docs` lists
them and `uv run basis docs job/PAGE` prints one.

- [Run, replay and check your first job](src/basis/job/docs/tutorial.md): a tutorial, on the example's fixture,
  with no database.
- [How-to guides](src/basis/job/docs/how-to.md): publish to a database for the first time, finish a run that
  published partly, find out why a run failed, check a change against production runs, and write a table whose changes
  depend on what it holds.
- [Reference](src/basis/job/docs/reference.md): the job contract, rows, write modes, checks, commands, settings,
  snapshot files, the ledger and the testing API.
- [How basis-job is put together](src/basis/job/docs/explanation.md): why the framework owns the loop, why
  nothing about a table is assumed, and how reruns, the ledger and all-or-nothing publishing fit together.
- [The design](docs/design.md): what was decided, what was decided against, and what is not yet verified.

## Develop basis-job

basis-job is a member of the [basis](https://github.com/jacobragsdale/basis) workspace, and its plumbing (the
databases, settings, logs, storage, snapshots and publishing) is basis-core's, in `packages/core`. From the
workspace's root:

```sh
uv sync
uv run pre-commit run --all-files   # ruff, ty, the lockfile and the import contracts
uv run pytest                        # every package's unit tests, and the example's own
uv run pytest -m integration         # against real SQL Server and Oracle: see tests/integration/README.md
```

[AGENTS.md](AGENTS.md) maps every module, in both packages, and lists the rules to keep.
