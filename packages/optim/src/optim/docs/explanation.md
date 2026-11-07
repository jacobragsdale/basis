# How optim is put together

`optim` owns the run loop that every portfolio model shares, so that a model holds only what is particular to it. The
loop has the same stages for every model: load, prepare, decide, round, check, save, and, in production, write orders.
Loading, saving and writing orders do I/O and happen once, in the parent process. Preparing, deciding and checking are
pure computation on data already in memory, and happen per portfolio, in worker processes. Most of the design follows
from keeping those two kinds of work apart, and from owning the loop rather than offering a library a model calls.

## Why the core owns the loop

Quants at a fixed income firm write optimizers for many small separately managed portfolios and for fewer, larger
institutional ones, and there are hundreds of them. They share one shape: load holdings and other data, apply each
portfolio's constraints and rules, optimize, and save the orders. When each repeats that shape in its own code, I/O
ends up anywhere, nothing can be replayed, logs differ, and none of them scales or tests the same way.

A library of helpers would not fix that, because the guarantees come from the loop, not from the helpers. Because the
core loads the data, model code does no I/O. Because the core saves every dataset as it loaded it, every run can be
replayed. Because the core runs every decision through the same rounding and checks, a solver's optimum is never
mistaken for permission to trade. The same properties let an agent drive research: it can decide a saved run again
with a hundred variants of its params without touching production data.

`optim` is one package that each model repo depends on, rather than a template copied into each repo, because hundreds
of copies of an engine drift apart. Everything that must stay the same across repos ships in the package and upgrades
with it: the loop, the shared datasets, the contract tests and the import check, the e2e safeguards, the example the
generator copies, and the agent guidance `optim sync` writes. A repo depends on a version range, and every `run.json`
and every run's first log line records the version it ran, so production snapshots show which model runs which
version. The package stays at 0.x until real models run on it in production, since a minor release may still break
the contract, as 0.2 broke 0.1's.

## One folder per model, one place per change

An analyst opens a model repo in an editor, not a terminal, so the repo has to say where things are by how it is laid
out. Each model is one folder in `models/`, with everything about it inside: its code, its params, its tests and its
fixture. Adding a dataset or a rule then touches one folder, not three trees. The folders that grow by adding files are
folders from the start, `datasets/`, `rules/`, `optimizer/`, `params/` and `tests/`, and the rest are single files that
become folders of the same name when they outgrow one file.

Two files carry two jobs. `model.py` is the model, and reads as its table of contents: the params, the data, the
numbers it reports, and hooks of a few lines that call `prep.py`, the rules and the optimizer. `main.py` is what you
run, with every value it runs written out: the params, the settings file, the portfolios. Nothing is inferred from
where a file sits, because inferences are invisible to the person debugging them. Workers import `model.py` and what it
imports, never `main.py`, which is why `main.py` may reach every source and `model.py` may not.

Nothing in a repo is specific to one editor or one agent vendor. There is no `.vscode/` or `.idea/`, no params schema
wiring, and the agent files are `AGENTS.md` and `.agents/skills/`, the conventions every agent reads. The layout, a
short `README.md` in each folder that grows, and a docstring opening every file are what make a repo legible in any
editor, with or without an agent beside it.

## A model, in hooks

A model declares its `Params`, its `Data`, and a pure `decide(portfolio, market, params)`. The hooks around `decide`
exist to put work where it runs best. `prepare_market` runs once per run, in the parent, for what every portfolio
shares, such as joining market data. `prepare_portfolio` runs once per portfolio, always in parallel: it builds the
portfolio from its slice of the data and applies the rules, one per line, in order. It is a separate step from
`decide` so that rules still run in parallel when decisions cannot, as with `reserve`. `check` adds a desk's own
reasons to reject, and `reserve` and `order` let portfolios compete for scarce supply.

`Params` is strict: an unknown key is refused and no value is coerced. A misspelled key in a variant an agent wrote, or
`"0.05"` where a number belongs, fails before any data loads instead of being ignored, which is the failure that would
otherwise surface as a quietly wrong sweep. Everything else the core validates is lax, so that a driver's `Decimal` or
Oracle's midnight `datetime` passes as the float or date it means.

`Data` declares each dataset as a class attribute, which is a descriptor: on the class it is the `Dataset`, and on an
instance it is the frame. So `portfolio.data.holdings` is a `DataFrame` that ty checks and editors complete, where a
string key would be a typo away from a `KeyError` in the morning run. Every model reads the shared holdings and
securities, because the core's checks need them, so `optim.Datasets` declares both.

The market and the portfolio are the model's own classes. `optim` reads nothing in them, so a model shapes them as its
problem needs, and their names say what they hold, where `shared` and `prepared` once said only when they were made.

## Rules share one signature

Every rule takes the portfolio, the market and the params, and returns the portfolio. One signature lets a rule do
anything short of solving: drop rows of the universe, add columns, set its flags, and change the limits the optimizer
reads from the portfolio. A rule that took only the pieces it touched would read a little tighter, but every rule would
have a different shape, and a rule that later needed one more piece would change its signature and its callers. With one
shape, `prepare_portfolio` reads as a list, each rule is tested the same way on a few rows, and adding one is one line.

A held security a rule drops stays held, and is not traded: the portfolio's value is taken from its holdings before
any rule, so a dropped row still counts.

## Constraints are cvxpy

A limit arrives from the params, the same for every portfolio and varied in sweeps; from a per-portfolio dataset, such
as a client's mandate; from code, such as cash never being overdrawn and trades coming in the sizes the market trades;
or from prep, computed each run, such as a band around a benchmark's weights. Whatever its origin, the constraint that
holds the problem to it is cvxpy, in the model's `optimizer/`: a function that takes the trades and returns cvxpy
constraints, which `build` adds to the problem. The limit's number is data, a param or a field a rule sets on the
portfolio, so a sweep varies it without a change of code.

`optim` defines no constraint of its own. Which constraints a model has, and how limits from different origins combine,
whether the tighter binds or a mandate overrides, is the model's logic, in its rules and its optimizer; the core
prescribes nothing.

Trade sizing, the integer program for whole increments, minimum pieces and minimum remainings, is model code too,
visible and changeable, rather than hidden in the core. Quants know their solver code and change it; a core that hid it
would be one they could not fix.

## Why workers do no I/O

Workers hold no connections. The parent ships the run's data and the market to each worker process once, through the
pool initializer, and each task is a portfolio id. Three things follow. Connection counts stay bounded however many
processes run. Every portfolio in a run sees the same moment of the data, so decisions are consistent with each other
and reproducible from the snapshot. And moving from one machine to a cluster would change one module, `engine.py`,
which would ship the state with `ray.put` or Dask's `scatter` and submit the same ids.

The rule is enforced, not only followed. In this package, `lint-imports` fails if a pure module, such as the contract,
`optim.decide` or the example's `model.py`, imports the loader, a source, the cache, storage or outputs. In a model
repo, each model's `tests/test_contract.py` asks grimp for any import chain from its `model.py` to a source, the loader,
the cache, outputs, the runs, a database driver or an HTTP client, and fails with the chain it found. A model repo
carries no import-linter configuration of its own that could drift. The contract tests are a plain test class the
model's test file subclasses, so they appear and run in an editor's test panel like any other test.

The cost is that the parent is the bottleneck for loading, and that every worker holds the whole run's data when a
model defines no `prepare_market`. Loading is I/O-bound and batched, so the parent is the right place for it.

## One path from main.py and the command line

`main.py` calls `optim.run`, and Airflow calls `optim run`; both reach the same code, from loading to saving, so a
laptop run behaves as production does. They differ only where an analyst's machine and production differ. A call from
Python takes its paths from the model's folder, because an editor runs a file from wherever it likes. It names its
settings file, `.env`, `.env.qa` or `.env.prod`, and sets that file's variables for the call alone, so a notebook that
reads QA and then production does not mix them. It saves its snapshot in the repo's `snapshots/`, where the analyst
finds it, whichever settings file it read, so research runs never land in production's store. And it never writes
orders: only `optim run` does, where production's environment configures the orders table. A laptop with read-only
production credentials can then decide real portfolios, or replay production's runs, and write nothing to a shared
system.

An error raises from Python, with its traceback, so a debugger stops where it surfaced. A hook's error still rejects
only its portfolio, in both, since one bad portfolio must not stop the rest of a production run; its reason names the
line of model code that raised it, and the log holds the traceback.

## Snapshots, replay and why params never change a query

A snapshot holds the run's request and params, each dataset as it was loaded, and every decision. Nothing a model
derived from the data is stored: the hooks are pure, so a replay derives it again with the code of the day, and a
change to a rule shows on replay as a change to the trades. Storing what each portfolio saw instead would make
snapshots grow with the model's intermediate frames, and the parent would hold them all until the snapshot is written.

Every query takes only the as-of date and the portfolio ids, and params never change what is loaded. A choice that
depends on params, such as which benchmark to track, selects rows after loading. That rule is what makes sweeps
possible: if a variant's params could change a query, the snapshot would not hold the data that variant needs, and the
sweep would have to touch production. A run's id is a UUIDv7 the core creates, so ids sort by when their runs started,
and `run.json` is the record of the run; there is no runs table.

The snapshot root is a path or URL read and written through `pyarrow.fs`: the repo's `snapshots/` for a run from
Python, and `OPTIM_SNAPSHOT_ROOT`, shared storage, in production, so quants and agents can replay production runs by
id. Moving to Azure Data Lake changes the URL alone. A snapshot is written under a hidden name and renamed whole, so a
reader never sees half of one, which needs a filesystem that renames: local disk, SMB and NFS mounts, and Data Lake with
a hierarchical namespace do. Snapshots never go in git, because they hold client holdings, and nothing prunes a repo's
`snapshots/`: deleting it is the analyst's choice, and safe at any time.

## The portfolios a model manages

Most runs decide every portfolio a model manages, and a model knows which those are better than its scheduler does. So
a model may declare a `portfolios` dataset, whose query takes only the as-of date and lists them, with any columns the
model wants, such as a client's own rating floor. A run given no portfolios decides every one it lists, which keeps
hundreds of ids out of Airflow's command lines and out of `main.py`, and the snapshot records the list as it does any
run's portfolios. The dataset loads first, whole, since the other per-portfolio queries need its ids, and each
portfolio's hooks then see its own row, as with any per-portfolio dataset. A portfolio given by id that the dataset
does not list is rejected rather than decided, since an id the model does not manage is usually a typo.

## Loading at the pace of each source

In production a query can take minutes, so a run asks for everything it needs at the start and lets each source set
the pace. Datasets read whole load concurrently with every batch of per-portfolio datasets, a thousand portfolios a
batch. Each source has a limit, `OPTIM_<SOURCE>__MAX_CONCURRENT`, which the loader holds as a semaphore around the
source call alone, so cache hits and validation never wait for a slot. A run then takes about as long as its slowest
source, not the sum of its queries.

Reads that depend on each other share a read-only transaction. A portfolio's holdings and its controls are only
consistent if they come from one moment: a trade booked between the two reads would count a sale but not the control
that came with it. So each batch's per-portfolio datasets from one source are read in one read-only transaction
(snapshot isolation on SQL Server, `SET TRANSACTION READ ONLY` on Oracle), which holds one of the source's slots
throughout and is rolled back at the end. Consistency matters within a portfolio, not across portfolios, so batches
still load concurrently.

Every fetch is bounded: rows arrive ten thousand at a time, and a query that returns more than
`OPTIM_<SOURCE>__MAX_ROWS` fails as soon as it passes the limit, so a query that lost its filter fails fast instead of
filling the run's memory. The first failed load stops the rest. Batched queries bind a list of ids as one JSON array,
which the SQL unpacks with `OPENJSON` or `JSON_TABLE`: one bind per batch, no `IN`-list limits, and no SQL built from
strings. A source the settings do not configure fails its first load, naming the variables to set; no run ever falls
back from a source to a fixture, because a real-money run must never decide on test data without saying so.

Rows are validated column by column, one pydantic `TypeAdapter(list[T])` per field, which is about five times faster
than a model per row. The constraint is that field rules must live in the annotation, so a row model with a
`@field_validator` is refused rather than half-validated. A `Literal` field becomes an ordered category, which is why a
rule can write `rating <= "A-"`.

## The file cache

A dataset that may be cached declares how long an entry stays fresh. The cache is a Parquet file per entry under
`OPTIM_CACHE_ROOT`, written through `pyarrow.fs` as snapshots are, with the time its source was read in the file. A
laptop points the root at a local folder and production at shared storage, so every run shares the entries, and
`optim warm` fills them before the day's first run. Without the setting, nothing is cached. Redis, which the engine
used before, would be infrastructure in every environment, bounded by memory, and awkward on Windows laptops; the
cache sits behind a small interface, so another backend can be added if files prove too slow for some dataset.

A run never fails because of the cache, since the cache only saves time. An entry it cannot read is a miss, and the
live read replaces it. A write goes to a temporary file that is renamed into place, so a concurrent run never reads
half an entry. Runs never cache an empty result, which is usually a feed that has not landed yet.

## Floats, and rounding to increments

Every value is a float, booked values included. With exact `Decimal` columns, a quant's `holdings.quantity * 0.5`
raises `TypeError`, and arithmetic on object columns is slow. What makes trades exact is rounding: the core rounds each
quantity to the nearest whole number of its security's trading increment, so a solver's 2.9999999 lots are 3, and
sells a holding whole when a sale comes within half an increment of it, so an odd lot sells as booked.

## Acceptance: when a decision may trade

A solver that reports optimal has solved the problem it was given, within its tolerances. That is not permission to
trade: a time limit may have left a poor answer, rounding may have moved a trade, and a formulation can count cash
differently from the books. So after rounding, the core rebuilds the portfolio from the holdings and prices alone and
rejects a decision that was not solved, trades a security that is not loaded, has a quantity that is not positive,
sells more than it holds, leaves cash below zero, or that rounding moved. One tolerance applies, in weights of the
portfolio's value, $1 per $1mm.

The last check replaces checking each constraint again. The solver proved the decision meets every constraint of the
model's problem before rounding; if rounding changed nothing material, it still does. A model that decides whole
increments, as the example's sizing does, always passes, since rounding then removes only the solver's noise. The
alternative, writing every limit a second time in pandas to check the rounded trades, kept two copies of each
constraint that nothing held in step. `check` stays, for anything a
desk wants beyond the problem.

A rejected portfolio writes no orders, its decision and reasons stay in the snapshot, and `optim run` exits non-zero.
`optim` never submits orders: they are released elsewhere.

## Solving through optim.cvxpy

The core does not call a model's solve: `decide` is one function for every kind of model, whether it solves one problem,
several or none. Instead, `optim.cvxpy` standardizes solving. `solve` passes the solver its time limit and gap under the
solver's own option names, and `warm_start=False`, since cvxpy's default warm start made HiGHS re-solves about six times
slower. `read` takes a problem the model solved itself. Both read the solver's own status, because cvxpy's loses what
acceptance needs: it reports COPT stopped by its time limit as `optimal_inaccurate` with a solution and
`infeasible_inaccurate` without one. A decision is solved only when the solver proved it optimal within the gap asked;
one that stopped at its time limit keeps its trades, for the snapshot, and is rejected.

## Portfolios that compete for supply

Each decision covers one portfolio; there are no joint solves across portfolios. When portfolios compete for a limited
buy universe, such as a new issue or a scarce bond, a model defines `reserve`, and its portfolios decide one at a time,
each seeing the market the earlier ones left. Only an accepted decision reserves, so a rejected portfolio leaves its
supply to the next one. Who gets a scarce bond is a client-fairness decision, so the order is always explicit: the
model's `order`, or the run's order, recorded in `run.json`. Replaying one portfolio of such a run decides the
portfolios before it again, since what they reserved is what it saw.

Allocating the supply up front in `prepare_market`, pro rata or by rotation, is the other way to handle competition.
It keeps `decide` parallel, and is the better choice when the allocation rule does not depend on what each portfolio
decides.

## One start method, and Windows laptops

Worker processes start with `spawn` on every platform. `forkserver` does not exist on Windows, where most quants'
laptops are, and one start method means a laptop behaves as production does. Each worker imports numpy, pandas and
cvxpy itself, which adds a second or two to a run that starts a pool; a run with one worker decides in its own process
and starts none, which is also where a breakpoint stops. A spawned worker imports the file that started the run, which
is why `main.py` keeps its call under `if __name__ == "__main__":`. Workers share the cores, so each gets a one-thread
BLAS.

Settings come only from `OPTIM_*` variables, the same for every model, so no model repo holds a connection string in
its code and moving storage is one deployment change. A laptop keeps them in its settings files, which git ignores.

## Logs are written for agents

Every record from the command line is one JSON object per line on stderr, because the main reader is an agent, and log
platforms parse the same lines; a run from Python logs text, for the person at the editor, unless `OPTIM_LOG_FORMAT`
says otherwise. Models log with `logging.getLogger(__name__)`, and the core's handler adds the run id, the model and the
portfolio id to every record, worker processes' records included, so one filter on `run_id` finds the whole run, even
one that died before writing its snapshot. Records stay small: bulk data goes in the snapshot. An exception is logged
once, where its traceback is whole, with the notes added on the way up, its root cause, and the line where it surfaced.

## Known limits

- **Holdings are working holdings.** The shared holdings dataset is the start of the day plus what has traded since; a
  model that needs start-of-day holdings declares its own dataset with the same row model.
- **Cash is cash holdings.** The core's cash check counts the holdings of cash securities; a cash flow not yet in the
  holdings is the model's to count, in its problem or its `check`.
- **One currency per portfolio.** There are no FX rates.
- **Increments are the core's alone.** The core rounds to the increment and checks that rounding moved nothing;
  minimum pieces and minimum remainings are a model's to decide, as the example's `optimizer/constraints.py` does.
- **Snapshots write every dataset every run.** Store each once per content hash if storage grows too fast; until then,
  delete a repo's `snapshots/` by hand.
- **Consistency is per batch and per source.** A batch's per-portfolio datasets from one source are read at one moment;
  the datasets read whole, and other sources, are each their own moment.
