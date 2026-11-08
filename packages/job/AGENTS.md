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
| core `settings.py` | `BASIS_*` variables: the snapshot root, each named database, with an optional `WRITE` block, each other service a fetch names (`http`), where spans go (`traces`), and the image's commit (`git_sha`). |
| `job.py` | Finding a job by entry point or module path, and reading its contract. |
| `rows.py` | `Row`, its column types and annotations, and `conform`, the boundary every frame passes through. |
| core `frames.py` | The one table of each kind of column's type in a pandas frame, every one Arrow-backed (`pd.ArrowDtype`); `convert`, a column's values to it without loss, which `conform` uses; and a frame to Arrow and back, as the databases and the run store take it. The one module of basis's pure code that imports pyarrow, which the import check discounts (core `testing.TYPES`). |
| core `tables.py` | What a run writes, in the terms every part shares: `Column`, `IDENTIFIER`, `Changes`, `Delete`, `Range`, and the `Target` protocol that `outputs.Target` meets. |
| `params.py`, core `model.py` | `Params` (strict), and the lax base model for records. |
| `inputs.py`, `outputs.py` | `Query`, `Fetch`, `File`, `Clock`, `Ids` and `Inputs`, with the `Handles` a run gives a transform; `Table`, the write modes, `Target` and `Outputs`. |
| core `resources.py` | The resources every part declares alike: `http`, `clock` and `ids`, and their protocols. It opens nothing, so `inputs.py` imports it. |
| core `errors.py` | `BasisError`, and `ServiceError` with its kinds, `Timeout`, `Unavailable`, `NotFound`, `Conflict`, `Rejected`, `Denied`, `BadReply` and `Failed`, which every resource raises, real or fake: a fetch's GET and a database's statements among them, so a run's `error` names the kind. |
| core `interval.py` | `Interval`: a run's span, its parsing and its binds. |
| core `calendars.py` | `calendar(NAME)`, `sifma-us` and `target2`: business days and early closes, as rules and dated exceptions, each covering only the days it states. |
| `loader.py` | Loading a job's inputs, each database in one read-only transaction, each fetch's GET, or from a folder of files, each thread in a copy of the run's context, so its spans are the run's; `json_rows`, the one place a fetch's rows become a frame, whose refusal of a reply is a `BadReply`. |
| core `opened.py`, `fakes.py`, `deadlines.py` | basis-core's resources as a run opens them: another service's client from the settings alone, with each call's span and deadline, the system's clock and new ids; their fakes; and each call's deadline. |
| `changes.py` | From outputs to conformed `Changes` per table, and the core's checks on them. |
| core `destination.py` | `TableSpec`, a destination as its database describes it, and the checks that a row and its values fit it. |
| core `databases/` | `base.py`, the reader and writer protocols and what the dialects share, a statement's span among it; `mssql.py` and `oracle.py`, each dialect's reads, table descriptions, writes and ledger, each statement a span. |
| core `publish.py` | Writing each database in one transaction, in name order, with the rerun guard and ledger rows. |
| core `snapshots.py`, `storage.py` | A run's manifest and its datasets; the run store, its objects named by `digest`, through `pyarrow.fs` with atomic writes. |
| `runs.py` | `run`, `replay`, `resume` and `regress`, which `cli.py` and `api.py` share, each run, replay and resume one trace, and the fakes a test gives by name. |
| `handles.py` | What a run gives a transform for each clock and ids: basis-core's, keeping every value for the run's record, and a replay's, which gives them again. |
| core `traces.py` | Each run's own tracer provider, never the global one, the settings' exporter, `trace`, a run's one trace, and `tracer`, the current run's, which core's SQL statements are spans on. |
| core `runs.py` | The halves of `replay`, `resume` and `regress` every part shares: finding and opening a saved run, finishing its publish, and choosing the runs to regress. |
| `diff.py` | How two runs' changes to a table differ. |
| `ddl.py` | `CREATE TABLE` for a job's tables and for the ledger, from basis-core's `basis.core.ddl`, which every part that publishes shares. |
| core `logs.py` | JSON lines, run fields on every record, the trace's and span's ids on each logged in a trace (`trace_fields`), and the exception record. |
| `testing.py` | `ContractTests`, the import check's `FORBIDDEN`, and the builders `frame` and `inputs`. |
| core `testing.py` | What every part's contract tests share: the import chains, with basis's portions named. |
| core `errors.py` | `BasisError`, the failures basis raises on purpose, which print as one line, and `deliberate`, for a call that raises a builtin error on purpose. |
| `new.py`, `sync.py`, `templates/`, `skills/` | `basis-job new repo`, `basis new job` and `basis sync`: a job repo from the example and the templates, declaring the job part, and what the part writes in it: its block of `AGENTS.md`, its skills, named `job-*`, and its line of the Dockerfile. |
| core `new.py`, `sync.py`, `hooks.py`, `hygiene.py`, `templates/` | What `new` and `sync` share across parts: the shared templates, the sources for a repo's basis packages, the files basis owns in a repo, which `sync` writes and removes, with the framework files in `templates/sync/`, the git setting that runs a repo's hooks, and the checks of staged files the commit hook runs. |
| core `verbs.py`, `command.py`, `check.py`, `gates.py`, `pytest_plugin.py` | The interface a part gives the `basis` command; the command, which finds the repo's declared parts and runs core's verbs and theirs, printing every failure as one `ERROR` line; `basis check`'s steps; and the gates it holds a repo's tests to, a coverage floor, no mocks and no sockets in unit tests, the last through basis's pytest plugin. |
| core `full.py`, `qa.py`, `mutation.py`, `review.py`, `clone.py` | `basis check --full`: regress, the QA tier with its marker, allowlist and guarded databases, mutation testing of the domain functions a change touches, and the record of what it found, which `basis review` prints; and what basis reads of a repo's git clone. |
| `qa.py` | basis-job's QA runs: each job `[tool.basis.qa.job]` declares, checked against the allowlist, then published to QA. |
| core `doctor.py`, `upgrade.py` | `basis doctor` and `basis upgrade`. |
| `example/` | The example job, laid out as a job's folder, with its fixture and tests; `basis new job` copies it, leaving out its `ruff.toml`. |
| `docs/` | The docs for job authors, which `basis docs` prints. |

## Rules to keep

- Job code does no I/O. `lint-imports` enforces it for the job contract's modules, the pure core modules they use, and
  the example's job; each job's contract tests enforce it in job repos. A job reaches another service only through a
  `Fetch`, a GET whose rows its snapshot saves, and its transform gets the time and new ids only through a `Clock` or
  `Ids`, whose values the run's record keeps, so a replay is given them again and reaches nothing.
- Every run, replay and resume is one trace, with a span for each SQL statement and GET under its root, on a provider
  of its own; nothing sets OpenTelemetry's global one, no `OTEL_*` variable changes where spans go, and every record
  it logs carries the job's name as `service`.
- basis-core imports nothing from basis-job; `lint-imports` enforces it. What core needs of a job's declarations, it
  takes through `basis.core.tables`.
- Nothing about a table is assumed: a write mode, a `Precision`, a `Zone` and an input's `empty` are always declared.
  A new option gets no default that depends on how jobs usually look.
- A query binds only the interval, as `interval_start` and `interval_end` (Oracle reserves `START`), and a fetch's
  path only the interval's ends, as `{start}` and `{end}`. Params never change what is read.
- Nothing is written to a database unless every check passed for every output, and only from `basis run --publish` or
  `basis resume`. Each database's tables and ledger rows commit in one transaction.
- Identifiers in SQL are a row's column names and a table's name, restricted by `basis.core.tables.IDENTIFIER` and
  `outputs.TABLE_NAME`; every value is bound.
- Every frame a job gets or returns is pandas, each column Arrow-backed, of the type `basis.core.frames` gives its
  row's kind, so no column is `object`. A frame becomes Arrow through `frames.to_arrow` alone: the columns' own types,
  no index and no pandas metadata, so the same rows are always the same object in the run store.
- A conversion at a boundary loses nothing: `frames.convert` casts with pyarrow's safe casts, which refuse a value
  that would round or truncate, and checks what they allow, a time of day dropped from a date and an integer other
  than 0 or 1 made a boolean.
- Job code never imports pyarrow, which the contract tests catch; it names a dtype through its row, `Row.dtypes()`.
- A pandas frame changes in place, so a run keeps each input as the Arrow table it loaded, which the snapshot saves,
  and gives the transform and the check each frames of their own, made from those tables (`runs.fresh`).
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
