# Agent guide

This repo is etl-framework, the package that ETL job repos depend on. A job is a folder in a repo's `jobs/`, whose
`job.py` exports `Params`, `Inputs`, `Outputs`, `transform` and optionally `check`; the framework loads its inputs,
runs its transform, checks its changes, saves a snapshot, and with `etl run --publish` writes each database in one
transaction with a ledger row per table. [docs/design.md](docs/design.md) is the design and its decisions; the docs for
job authors are in `src/etl_framework/docs/` and ship with the package.

## Where things are

A run flows top to bottom through this table. All paths are under `src/etl_framework/`.

| Path | Holds |
|---|---|
| `__init__.py` | The job contract's names; `etl.run` and `etl.replay` load `api.py` on first use. |
| `api.py` | `run` and `replay`, as a job's `main.py` calls them: paths from the job's folder, a settings file set for the call alone, snapshots in the repo's `snapshots/`, never a publish. |
| `cli.py` | Every `etl` command, one handler each, its arguments as a frozen dataclass. |
| `settings.py` | `ETL_*` variables: the snapshot root and each named database, with an optional `WRITE` block. |
| `job.py` | Finding a job by entry point or module path, and reading its contract. |
| `rows.py` | `Row`, its column types and annotations, and `conform`, the boundary every frame passes through. |
| `params.py` | `Params` (strict) and the lax base model for records. |
| `inputs.py`, `outputs.py` | `Query`, `File` and `Inputs`; `Table`, the write modes, `Changes`, `Delete`, `Range` and `Outputs`. |
| `interval.py` | `Interval`: a run's span, its parsing and its binds. |
| `loader.py` | Loading a job's inputs, each database in one read-only transaction, or from a folder of files. |
| `changes.py` | From outputs to conformed `Changes` per table, and the core's checks on them. |
| `destination.py` | `TableSpec`, a destination as its database describes it, and the checks that a row and its values fit it. |
| `databases/` | `base.py`, the reader and writer protocols and what the dialects share; `mssql.py` and `oracle.py`, each dialect's reads, table descriptions, writes and ledger. |
| `publish.py` | Writing each database in one transaction, in name order, with the rerun guard and ledger rows. |
| `snapshots.py`, `storage.py` | The snapshot's files and `run.json`; the snapshot root through `pyarrow.fs`, with atomic writes. |
| `runs.py` | `run`, `replay`, `resume` and `regress`, which `cli.py` and `api.py` share. |
| `diff.py` | How two runs' changes to a table differ. |
| `ddl.py` | `CREATE TABLE` for a job's tables and for the ledger. |
| `logs.py` | JSON lines, run fields on every record, and the exception record. |
| `testing.py` | `ContractTests`, the import check's `FORBIDDEN`, `check_agent_files`, and the builders `frame` and `inputs`. |
| `new.py`, `sync.py`, `templates/`, `skills/` | `etl new repo`, `etl new job` and `etl sync`: a job repo from the example and the templates, and its agent files. |
| `example/` | The example job, laid out as a job's folder, with its fixture and tests; `etl new` copies it, leaving out its `ruff.toml`. |
| `docs/` | The docs for job authors, which `etl docs` prints. |

## Rules to keep

- Job code does no I/O. `lint-imports` enforces it for the core's pure modules and the example's job; each job's
  contract tests enforce it in job repos.
- Nothing about a table is assumed: a write mode, a `Precision`, a `Zone` and an input's `empty` are always declared.
  A new option gets no default that depends on how jobs usually look.
- A query binds only the interval, as `interval_start` and `interval_end` (Oracle reserves `START`). Params never
  change what is read.
- Nothing is written to a database unless every check passed for every output, and only from `etl run --publish` or
  `etl resume`. Each database's tables and ledger rows commit in one transaction.
- Identifiers in SQL are a row's column names and a table's name, restricted by `rows.IDENTIFIER` and
  `outputs.TABLE_NAME`; every value is bound.
- A conversion at a boundary loses nothing; polars' own casts round and truncate, so `rows.conform` casts back and
  compares.
- A field added to `RunRecord` gets a default, so snapshots from older versions still load.
- After changing the example, the templates or the skills, generate a repo and run its checks (see Checks).

## Changing the framework

- A database dialect is a module in `databases/` with a reader, a writer, its settings in `settings.py`, its types in
  `destination._TYPES` and `ddl._FIXED`, and its ledger columns in `ddl._LEDGER`; add its driver to the import
  contract and to `testing.FORBIDDEN`, and its container to the integration tier and CI.
- A write mode is a frozen dataclass in `outputs.py`, a case in `changes.build`, and a row in the reference's table.
- A command is a handler in `cli.py`; its Python twin, if any, is in `api.py`.

## Checks

```sh
uv run pre-commit run --all-files   # ruff, ty, the lockfile and the import contract
uv run pytest                        # unit tests and the example's own
uv run pytest -m integration         # real SQL Server and Oracle: tests/integration/README.md
```

To check what `etl new` generates:

```sh
uv run etl new repo pricing-etl prices --dir SCRATCH --framework-path .
cd SCRATCH/pricing-etl && uv run etl new job rates
uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run ty check
```
