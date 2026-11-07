# Agent guide

This package is basis-job, which ETL job repos depend on; it was etl-framework until basis's step 1.3. A job is a
folder in a repo's `jobs/`, whose `job.py` exports `Params`, `Inputs`, `Outputs`, `transform` and optionally `check`;
the framework loads its inputs, runs its transform, checks its changes, saves a snapshot, and with `basis run
--publish` writes each database in one transaction with a ledger row per table. The job contract is this package's,
and the plumbing it runs on is basis-core's, in `packages/core`, which imports nothing from here.
[docs/design.md](docs/design.md) is the design and its decisions; the docs for job authors are in
`src/basis/job/docs/` and ship with the package.

## Where things are

A run flows top to bottom through this table. Paths are under `src/basis/job/`, and those marked core under
`../core/src/basis/core/`.

| Path | Holds |
|---|---|
| `__init__.py` | The job contract's names; `etl.run` and `etl.replay` load `api.py` on first use. |
| `api.py` | `run` and `replay`, as a job's `main.py` calls them: paths from the job's folder, a settings file set for the call alone, snapshots in the repo's `snapshots/`, never a publish. |
| `cli.py` | basis-job's part of the `basis` command: `part()`, which its `basis.verbs` entry point names, with its verbs (`run`, `resume`, `describe`, `ddl`) and what core's verbs do with its jobs; and `scaffold()`, the `basis-job` command that makes a repo. One handler per verb, its arguments as a frozen dataclass. |
| core `settings.py` | `BASIS_*` variables: the snapshot root and each named database, with an optional `WRITE` block. |
| `job.py` | Finding a job by entry point or module path, and reading its contract. |
| `rows.py` | `Row`, its column types and annotations, and `conform`, the boundary every frame passes through. |
| core `tables.py` | What a run writes, in the terms every part shares: `Column`, `IDENTIFIER`, `Changes`, `Delete`, `Range`, and the `Target` protocol that `outputs.Target` meets. |
| `params.py`, core `model.py` | `Params` (strict), and the lax base model for records. |
| `inputs.py`, `outputs.py` | `Query`, `File` and `Inputs`; `Table`, the write modes, `Target` and `Outputs`. |
| core `interval.py` | `Interval`: a run's span, its parsing and its binds. |
| `loader.py` | Loading a job's inputs, each database in one read-only transaction, or from a folder of files. |
| `changes.py` | From outputs to conformed `Changes` per table, and the core's checks on them. |
| core `destination.py` | `TableSpec`, a destination as its database describes it, and the checks that a row and its values fit it. |
| core `databases/` | `base.py`, the reader and writer protocols and what the dialects share; `mssql.py` and `oracle.py`, each dialect's reads, table descriptions, writes and ledger. |
| core `publish.py` | Writing each database in one transaction, in name order, with the rerun guard and ledger rows. |
| core `snapshots.py`, `storage.py` | A run's manifest and its datasets; the run store, its objects named by `digest`, through `pyarrow.fs` with atomic writes. |
| `runs.py` | `run`, `replay`, `resume` and `regress`, which `cli.py` and `api.py` share. |
| core `runs.py` | The halves of `replay`, `resume` and `regress` every part shares: finding and opening a saved run, finishing its publish, and choosing the runs to regress. |
| `diff.py` | How two runs' changes to a table differ. |
| `ddl.py` | `CREATE TABLE` for a job's tables and for the ledger, from basis-core's `basis.core.ddl`, which optim's orders table shares. |
| core `logs.py` | JSON lines, run fields on every record, and the exception record. |
| `testing.py` | `ContractTests`, the import check's `FORBIDDEN`, and the builders `frame` and `inputs`. |
| core `testing.py` | What every part's contract tests share: the import chains, with basis's portions named. |
| core `errors.py` | `BasisError`, the failures basis raises on purpose, which print as one line, and `deliberate`, for a call that raises a builtin error on purpose. |
| `new.py`, `sync.py`, `templates/`, `skills/` | `basis-job new repo`, `basis new job` and `basis sync`: a job repo from the example and the templates, declaring the job part, and what the part writes in it: its block of `AGENTS.md`, its skills, named `job-*`, and its line of the Dockerfile. |
| core `new.py`, `sync.py`, `hooks.py`, `hygiene.py`, `templates/` | What `new` and `sync` share across parts: the shared templates, the sources for a repo's basis packages, the files basis owns in a repo, which `sync` writes and removes, with the framework files in `templates/sync/`, the git setting that runs a repo's hooks, and the checks of staged files the commit hook runs. |
| core `verbs.py`, `command.py`, `check.py` | The interface a part gives the `basis` command; the command, which finds the repo's declared parts and runs core's verbs and theirs, printing every failure as one `ERROR` line; and `basis check`'s steps. |
| `example/` | The example job, laid out as a job's folder, with its fixture and tests; `basis new job` copies it, leaving out its `ruff.toml`. |
| `docs/` | The docs for job authors, which `basis docs` prints. |

## Rules to keep

- Job code does no I/O. `lint-imports` enforces it for the job contract's modules, the pure core modules they use, and
  the example's job; each job's contract tests enforce it in job repos.
- basis-core imports nothing from basis-job; `lint-imports` enforces it. What core needs of a job's declarations, it
  takes through `basis.core.tables`.
- Nothing about a table is assumed: a write mode, a `Precision`, a `Zone` and an input's `empty` are always declared.
  A new option gets no default that depends on how jobs usually look.
- A query binds only the interval, as `interval_start` and `interval_end` (Oracle reserves `START`). Params never
  change what is read.
- Nothing is written to a database unless every check passed for every output, and only from `basis run --publish` or
  `basis resume`. Each database's tables and ledger rows commit in one transaction.
- Identifiers in SQL are a row's column names and a table's name, restricted by `basis.core.tables.IDENTIFIER` and
  `outputs.TABLE_NAME`; every value is bound.
- A conversion at a boundary loses nothing; polars' own casts round and truncate, so `rows.conform` casts back and
  compares.
- A field added to `RunRecord` gets a default, so snapshots from older versions still load.
- After changing the example, the templates or the skills, generate a repo and run its checks (see Checks).

## Changing the framework

- A database dialect is a module in core's `databases/` with a reader, a writer, its settings in core's
  `settings.py`, its types in core's `destination._TYPES` and `ddl._FIXED`, and its ledger columns in core's
  `ddl._LEDGER`; add its driver to the import contract and to core's `testing.FORBIDDEN`, and its container to the
  integration tier and CI.
- A write mode is a frozen dataclass in `outputs.py`, a case in `changes.build`, and a row in the reference's table.
- A verb is a `Verb` in `cli.part()` with its handler in `cli.py`; its Python twin, if any, is in `api.py`. A verb that
  every kind of part has, such as `replay`, is core's, in core's `command.py`, and reaches the job part through a
  field of its `Part`.
- Every failure a verb reports is one line, `ERROR: REASON: FIX`: return `fail(reason, fix)`, or raise a `BasisError`
  (such as `JobError` or `SettingsError`) whose message ends with the fix. Any other exception is a defect, and prints
  its traceback first; wrap a call that raises a builtin error on purpose in `deliberate`, as `cli.py` does around
  finding a run.
- A file basis-job wrote before it carried the managed-by marker, and no longer writes, goes in `sync.RETIRED`, so
  `basis sync` removes it from repos.

## Checks

From the workspace's root:

```sh
uv run pre-commit run --all-files   # ruff, ty, the lockfile and the import contracts
uv run pytest                        # every package's unit tests, and the example's own
uv run pytest -m integration         # real SQL Server and Oracle: tests/integration/README.md
```

To check what `basis-job new repo` generates, from the root, whose checkout `--framework-path` names:

```sh
uv run basis-job new repo pricing-etl --dir SCRATCH --framework-path .   # its first job is positions
cd SCRATCH/pricing-etl && uv run basis new job rates
uv run basis check
git add --all && git commit -m "First commit"   # the commit hook runs basis check
```

A repo whose image builds takes basis-job from git, as the root's AGENTS.md says.
