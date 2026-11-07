# etl-framework

The run loop that ETL jobs share. A job is a folder of plain Python whose `job.py` declares what it reads, what it
writes and how, and transforms inputs to outputs in a pure function. etl-framework runs everything else the same way for
every job: reading from SQL Server, Oracle and files, checking every output against its row and its destination
table, saving a snapshot that replays, and publishing each database in one transaction, recorded in a ledger. Jobs live
in their own repos, which depend on etl-framework, and run in a container.

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

```sh
uvx --from git+https://github.com/jacobragsdale/etl-framework@v0.1.0 etl new repo pricing-etl prices
cd pricing-etl
uv run pytest
```

The repo holds one job, `jobs/prices/`, copied from the package's example: daily positions from trades. Around it are
a README that maps each kind of change to the file it goes in, a `Dockerfile`, a `.env.example`, and the agent files
`etl sync` keeps current. `uv run etl new job NAME` adds a job. To take etl-framework from an internal package index
instead of GitHub, pass `--index URL`.

## Documentation

The docs ship with the package, so a job repo reads the ones for the version it installed: `uv run etl docs` lists
them and `uv run etl docs PAGE` prints one.

- [Run, replay and check your first job](src/etl_framework/docs/tutorial.md): a tutorial, on the example's fixture,
  with no database.
- [How-to guides](src/etl_framework/docs/how-to.md): publish to a database for the first time, finish a run that
  published partly, find out why a run failed, check a change against production runs, and write a table whose changes
  depend on what it holds.
- [Reference](src/etl_framework/docs/reference.md): the job contract, rows, write modes, checks, commands, settings,
  snapshot files, the ledger and the testing API.
- [How etl-framework is put together](src/etl_framework/docs/explanation.md): why the framework owns the loop, why
  nothing about a table is assumed, and how reruns, the ledger and all-or-nothing publishing fit together.
- [The design](docs/design.md): what was decided, what was decided against, and what is not yet verified.

## Develop etl-framework

```sh
uv sync
uv run pre-commit run --all-files   # ruff, ty, the lockfile and the import contract
uv run pytest                        # the unit tests and the example's own
uv run pytest -m integration         # against real SQL Server and Oracle: see tests/integration/README.md
```

[AGENTS.md](AGENTS.md) maps every module and lists the rules to keep.
