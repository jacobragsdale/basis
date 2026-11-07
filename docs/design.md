# The basis design

basis is a set of Python packages that give the firm's APIs, batch jobs and portfolio optimizers one shape, so that the
agents that now write nearly all of their code produce the same kind of repo every time, and checks, rather than people
reading diffs, decide whether a change is right. Web UIs are built with
[basis-ui](https://github.com/jacobragsdale/basis-ui), a separate TypeScript project that talks to basis APIs through
the contract in [What a UI can rely on](#what-a-ui-can-rely-on). "basis" and "basis-ui" are placeholder names. The
design was agreed on 2026-11-06 in a brainstorm that built on
[portfolio-optim](https://github.com/jacobragsdale/portfolio-optim) and
[etl-framework](https://github.com/jacobragsdale/etl-framework). This page records the decisions and their reasons; [the
roadmap](roadmap.md) gives the build order, and which steps can run at once.

## The problem

Software developers and quants at a fixed income research firm write many APIs, batch jobs and web UIs, and each team
reinvents the same plumbing: database wiring, Dockerfiles, pipelines, settings and logging. The code is messy and its
standards differ from repo to repo, it is hard to test, it is hard to observe in production, and a change takes too long
to get there. The firm writes its code with AI agents (Claude Code, Cursor, GitHub Copilot, opencode and others), and
people rarely read it. What matters is that every repo has the same shape, that every guarantee is a check an agent can
run, and that everything runs from the command line.

There are more than fifty repos and several developers. People own whole products, and a product's APIs, UIs and jobs
often live in separate repos and deploy separately, to the same AKS cluster. Developers work on Windows 11 Citrix
desktops, both on Windows and in WSL, and run their agents there, interactively; basis assumes no admin rights and no
Docker on those desktops. The firm's data sits in Oracle and SQL Server. The maintainer's team owns deployment, through
Azure DevOps pipelines, and every deployable runs in a container. The repos have no CI yet. A custom sign-in system
protects the UIs and can issue JWTs; the APIs behind them have no authentication today.

The maintainer and the maintainer's team build basis. Repos take a new version when their owners choose to, by relocking
with uv. People review every change, and want strong automatic gates behind that review. Developers should be able to
treat basis as a black box, and nothing about it should make them wonder.

## The idea: the core owns the loop, and the checks are the review

optim and etl-framework showed that a framework that owns the run loop can keep the code it runs pure. The core loads,
the model or job computes, the core checks and saves, and every run replays from its snapshot. basis applies that idea
to every kind of Python the firm writes, and adds one rule for a codebase nobody reads: every rule is a check, and every
check's failure says how to fix it.

A rule that lives only in a doc is a rule an agent eventually breaks. The teams that publish on agent-written code
converge on this. OpenAI's harness-engineering team enforced its architecture with custom linters whose error messages
carry the fix, and kept its agent file to a short map of the repo. Anthropic's Claude Code guidance says a line Claude
keeps missing belongs in a hook, and that the verifier must be nearly perfect, "otherwise Claude will solve the wrong
problem". Cursor's advice is typed languages, linters and tests, because "agents perform best when they have a clear
target to iterate against".

Replaying real work is the strongest of those checks, because a wrong answer rarely passes it. `basis regress` reruns
recent runs and requests with the working tree's code and lists what changed, so a reviewer sees what a change did to
real outputs without reading the diff.

basis is Python only, and a repo holds one toolchain: a basis repo is Python, and a UI is always a repo of its own, on
basis-ui. One language keeps each project's toolchain, checks and docs whole, and the firm already keeps most of its UIs
apart from their APIs.

## What a developer sees

Every basis repo has one command, `basis`, whatever it holds, and the same verbs mean the same thing in an API repo, a
jobs repo and an optimizer repo. A verb that belongs to one kind of part exists only in repos that have that part, and
`basis --help` lists exactly the verbs of the repo it runs in. People move between their product's repos all day, and
one command with one set of verbs lets them do it without thinking about which repo they are in. A basis-ui repo has the
same verbs (`check`, `dev`, `test`, `sync`, `doctor`, `docs`) through Vite+'s own `vp` command.

| Verb | What it does | In repos with |
|---|---|---|
| `basis check` | Lints, type checks, unit and contract tests, and the agent-files and drift checks, with no network and no Docker | Every part |
| `basis check --full` | `check`, plus `regress` where the run store is reachable, and the QA tier where QA settings exist | Every part |
| `basis review` | Writes a pull request's summary: what `regress` changed, the API contract's diff, the versions, and the commit that was checked | Every part |
| `basis replay RUN`, `basis regress` | Rerun one saved run or recorded request with today's code; rerun recent ones and list what changed | Every part |
| `basis new PART NAME` | Adds an API, a job or, provisionally, a model to the repo | Every part |
| `basis sync`, `basis upgrade` | Rewrite the framework's files from the installed version; relock basis, sync, check and regress in one step | Every part |
| `basis doctor`, `basis docs` | Check the desktop's setup and print the fix for each problem; print the docs for the installed version | Every part |
| `basis dev`, `basis serve`, `basis call ROUTE` | Run the API on fakes; run the API; run one route in-process and print its response | An API |
| `basis records export` | Write chosen recorded requests and responses as JSON, for a UI's fixture mode | An API |
| `basis run NAME`, `basis resume RUN`, `basis describe NAME` | Run a job, and with `--publish` write its outputs; finish a publish that failed partway; print its params, inputs and outputs | Jobs, and models (provisional) |
| `basis ddl NAME` | Print a job's `CREATE TABLE` statements | Jobs, and models (provisional) |
| `basis sweep RUN DIR` | Decide a model's saved run once per params file | Models (provisional) |

The rest of the black box is how basis fails. Every file `basis sync` writes opens with a line saying that basis manages
it, and the drift check fails when someone edits one, so the fix for a framework file is a framework release. Every
failure is one line that starts with `ERROR`, gives the reason and ends with the fix. A missing setting names the
variable to set. `basis doctor` checks uv, git, the hooks, the feed and the settings, on Windows and in WSL, and prints
the fix for each problem it finds.

## The packages

| Package | What it is | Imports from basis |
|---|---|---|
| `basis-core` | Internal: database sources for Oracle and SQL Server, settings, JSON logs, storage, the run store, snapshots, publishing with a ledger, the machinery of `replay`, `regress`, `new` and `sync`, contract-test helpers, and the `basis` command | Nothing |
| `basis-job` | Batch jobs: etl-framework's job contract | `basis-core` |
| `basis-api` | HTTP APIs on FastAPI | `basis-core` |
| `optim` | Portfolio optimizers: portfolio-optim's model contract and engine. Provisional, and not yet released | `basis-core` |

Every package but optim is published to the internal PyPI feed; optim stays in this repo, built and tested with the rest,
and is not published until its design is settled. `basis-core` is internal: no repo depends on it by name, no user
code imports it, and no user doc mentions it. It arrives as a dependency of the packages a repo does depend on, and
brings the `basis` command with it. Each kind of part adds its own verbs to that command through entry points, so a job
author's docs, skills and commands are complete without a word about the core underneath.

The packages import as `basis.job` and `basis.api`, with the core as `basis.core`, one namespace that each distribution
adds to. An import contract in this repo's checks keeps the layers apart: `basis.core` imports none of the others, and
`basis.job`, `basis.api` and `optim` import `basis.core` and never each other. optim's side of it is in its own
`pyproject.toml`, so the rest holds without it.

`basis.core` is the one place basis shares code rather than only formats, and the reason is the code itself. Jobs and
APIs both read and write Oracle and SQL Server, read settings, log, and keep what they did; etl-framework carries all of
that, and an API framework would have been a second copy. Copies drift: portfolio-optim carries a third, and while
building etl-framework, a defect was found and fixed in how its SQL Server reads begin a snapshot transaction, which
portfolio-optim's `sources/mssql.py` likely has still. The packages above `basis.core` share nothing with each other, so
a change to jobs cannot break an API or an optimizer.

**Provisional until the maintainer meets the firm's quant team.** optim stays separate in its code: its own package, with its own model repos, generator, skills and docs, because
optimizers are a distinct kind of code with its own workflow (sweeps, trials, research from an IDE) and quants as its
main authors. It gives its plumbing to `basis.core` and keeps its contract, its engine, `optim.cvxpy`, rounding and
acceptance, `reserve` and `order`, `sweep` and `trial`, the shared holdings and securities datasets, and pandas. Its
verbs come under `basis` like every other part's, and `optim` stays as an alias for the same command. Its orders are
written through `basis.core`'s publishing, with a ledger row in the same transaction, so a failed or retried run never
writes an order twice and `resume` finishes one that stopped partway. No repo depends on optim yet, so it moves with
nothing to migrate. Its own design is in [packages/optim/docs/design.md](../packages/optim/docs/design.md), and is
provisional too.

optim's design may change after that meeting, or optim may go, so nothing outside `packages/optim` depends on it:
basis-core, basis-job and basis-api, their release, and the contract a UI relies on stay as they are whatever optim
does. A mechanism in core that only one part uses must be one a job could use as it stands, and core names no part: it
finds each through its `basis.verbs` entry point, relocks each installed part's package, and owns a skill by the marker
in its front matter, not by its name. `scripts/without_optim.py` checks the rest on every pull request: it lists each
line outside `packages/optim` that names optim, but a few that must, then removes `packages/optim`, relocks, fails when
a remaining package's version moves, and runs every hook and unit test. What it cannot see is a mechanism shaped by
optim, which the rule above leaves to review.

## One repo, one version

Every package lives in this repo, a uv workspace, so a change that crosses packages, such as a new resource in
`basis.core` and its use in `basis.api`, is one pull request. This repo's own CI runs on GitHub Actions, with SQL Server
and Oracle service containers for the integration tests, and runs only the jobs whose paths changed.

basis-core, basis-job and basis-api are released together, under one version and one tag, such as `v0.7.0`. Each
package depends on `basis-core` at exactly its own version, so a repo's lock can only hold basis packages from a single
release, and an owner never has to work out which versions go together. optim keeps that version in this repo, and
depends on `basis-core` at exactly it, so it builds and tests with every release, but it is not published until its
design is settled. Some releases change nothing in some packages; their notes say
so. basis stays at 0.x until a real repo runs it in production, as portfolio-optim's own policy was.

A developer starts a repo with `uvx`, which fetches a package from the internal feed and runs its command from a cached
environment, installing nothing into a project: `uvx basis-api new app positions`, `uvx basis-job new repo
nightly-loads`, or, once optim is released, `uvx optim new repo sma income` (the repo and its first model). uv installs per user, with no admin
rights. Each developer's user-level `uv.toml` names the internal feed as the default index, not an extra one: under uv's
default first-index strategy, a name the feed holds is then never looked up on public PyPI, where a package of the same
name can be another's entirely, as public PyPI's `optim` is. The feed proxies public PyPI for everything else.
A generator takes the feed's URL as `--index`, and writes it as the new repo's default index: uv does not record which
index a package came from, so a part installed from one and given no `--index` refuses, with that fix, rather than take
basis from public GitHub.

Repos pull new versions; nothing pushes them. `basis upgrade` relocks basis-core and each installed part's package with
uv, runs `basis sync`,
runs `basis check --full`, and prints what changed, so taking a release is one command whose result says whether the
repo still behaves the same.

## Repos and products

A repo declares its parts in its `pyproject.toml`: an API, jobs and models, in any combination, and basis never infers a
part from which folders exist. The declaration is `[tool.basis] parts = ["api", "job"]`, naming each part the repo
has. A product can span several repos, and its UI is always one of its own.

| Path | Holds, when the repo has that part |
|---|---|
| `api/` | The API, on `basis-api` |
| `jobs/JOB/` | One folder per job, on `basis-job` |
| `models/MODEL/` | One folder per optimizer model, on `optim` (provisional) |
| `openapi.json` | The API's OpenAPI document, committed |
| `fixtures/` | Synthetic data and its manifests, which the tests read |
| `Dockerfile`, `azure-pipelines.yml` | One image for the repo, and the deploy pipeline that calls the maintainer's team's templates |
| `AGENTS.md`, `CLAUDE.md`, `.agents/skills/`, `.claude/skills/` | The agent files; see [Agents](#agents) |
| `.githooks/`, `.gitattributes` | The commit and push hooks, and LF line endings on Windows and in WSL alike |

The image has an entry point per part, under `tini`: `basis serve` runs the API, and `basis run NAME --publish` runs a
job or a model. Whatever schedules a job runs the container with that command, so basis assumes no scheduler.

## What a UI can rely on

A basis API and the basis-ui app that calls it live in different repos and deploy separately, so this contract is what
keeps them working together. `basis-api`'s contract tests check its side, and basis-ui's tests run against a pinned copy
of the `openapi.json` that this repo's example API produces. [The contract page](contract.md) gives it field by field.

- **The OpenAPI document.** Each API commits its `openapi.json`, which `basis sync` writes from its service and `basis
  check` fails on when it is out of date. Each operation's id is its handler's name, so a generated client's functions
  read `getPositions` rather than FastAPI's default `read_positions_positions_get`, and every model is strict. The API's
  breaking-change check compares the document with the one at its last release tag, which the repo declares as a glob of
  its release tags, and fails when a field is removed, renamed or changes type; with no earlier release, it passes and
  says so, and in a clone that may lack its releases, a shallow one or one that fetches no tags, it fails with the fix. Adding a response field is never breaking, because basis-ui's
  schemas drop fields they do not know. Requests are the other way round: an API refuses unknown request fields, so an
  API that accepts a new one deploys before the UI that sends it, and it takes a decimal only as a string, as it sends
  one, and a time only with its offset, as it sends one: a response's time without one is a defect, a 500.
- **Errors.** Every error is an RFC 9457 problem details response, from one exception map. A 422, whether basis or the
  handler refuses the request, names each field at fault in `errors`, by a JSON Pointer into the body or by a query or
  path parameter's name, as RFC 9457's own example does, so a UI puts each error on its field.
- **Identity.** A UI sends the user's JWT as a bearer token, and the API verifies it. A missing token is anonymous; an
  invalid or expired one gets a 401 problem of type `invalid-token`, and a route that needs a user answers an anonymous
  request with a 401 of type `sign-in-required`, each with a `WWW-Authenticate: Bearer` challenge. While the sign-in
  system's keys cannot be fetched, a token whose key the API cannot look up gets a 503, `keys-unavailable`, which a UI
  retries rather than signing the user out. In development, a UI signs its tokens with basis's published development
  key.
- **Datasets as Arrow.** A route that returns a dataset declares it, and returns Arrow when the request asks for
  `application/vnd.apache.arrow.stream`, which is how parallax, basis-ui's grid, receives its data. Its rows are flat,
  one column for each field, so its Arrow and its JSON hold the same values.
- **One origin.** A UI and its API sit behind one host on the AKS cluster, the ingress routing `/api` to the API,
  removing the prefix, and the rest to the UI, so the browser makes no cross-origin calls. The document's paths carry no
  `/api`, and a UI's client adds it. Where that routing is not possible, the API's settings declare the origins it
  allows, per environment; an empty list, the default, means the same origin only.
- **Recorded responses.** `basis records export` writes chosen recorded requests and responses as JSON files in the
  format basis-ui's fixture mode reads. They hold client data, so they go in a folder git ignores, never in a commit.

## APIs: basis.api

FastAPI already owns the request loop: routing, dependency injection, validation and OpenAPI. What each team reinvents
is the app around that loop and the I/O inside it, so those are what `basis.api` owns. A service declares its routes,
its resources and its settings, and the core builds the FastAPI app from them, with the same middleware, lifespan, error
handlers, health endpoints and shutdown in every repo.

A resource is anything a handler reaches outside the process: a database, another HTTP service, a cache, the clock, new
ids. Each is declared by name, and the core opens it in the app's lifespan, puts a deadline on every call that can wait,
traces every call, and provides a fake of it for tests. The clock and new ids never wait, so they have no deadline, and
their spans record each value, so that a recorded request replays them. Handler code reaches the outside only through
its resources, and an import contract, as basis-job's keeps job code pure, keeps handlers away from database drivers,
HTTP clients and environment variables. "Pure" cannot mean "no I/O" for a handler that reads, decides, and reads again; it
means "I/O only through the core", which lets the core bound, observe and record every call.

Request bodies and settings are strict models: an unknown key is refused and no value is coerced, so a misspelled field
fails at the boundary instead of being silently ignored. A decimal in a request body is a JSON string, as it is in a
response, because a JSON number reaches a decimal through a float, and a time in one carries its offset. Query and path
parameters, which arrive as text, are read by each type's grammar. Every route declares its deadline, which caps every
call its request makes, its response model and its errors, and an error it does not declare answers 500. Database writes
in a request commit only after the handler returns and its response validates, and calls to other services that change
state go after that commit. A declared pure compute function runs in a process pool, off the event loop, because
CPU-bound quant code inside an `async def` stalls every request on that worker. An unexpected error is logged once, with
its traceback.

A call that changes another service's state cannot be rolled back if it goes before the request's writes commit, nor
undone if it fails after, so such a call goes through an outbox. In a route that writes, the handler sends it with
`send` on its `api.Client`, with an idempotency key, and basis writes it as a row of the outbox table that the written
database declares, with how long its calls are retried, in the route's one transaction, so the call commits or rolls
back with the writes. After the commit, basis makes the call once, within the route's deadline, with the key as
`Idempotency-Key`, and marks the row done. If that attempt fails, the request still succeeds, since its writes
committed, and its response says nothing of the call still pending. A relay, `basis relay`, a verb of the API's part
that the repo's scheduler runs on a schedule and never in the API's replicas, retries the row with backoff until the
other service accepts it, or until the row ages out: the row is then marked expired and retried no more, and the
relay's run fails, its one ERROR line naming the call and its key, so that the scheduler's alert fires. A 4xx refuses
the call for good, but for a 408, a 409, a 425 or a 429, which are retried as a 5xx is: a 409 under the
Idempotency-Key draft means a call with the same key is still being processed. A refused row is marked refused at
once, whether the request or the relay made the call, and retried no more, and the relay's run fails with the same
ERROR line, naming the call, its key and the status. Delivery is therefore at least once, and the firm's services drop
a duplicate by its `Idempotency-Key`. basis provides the outbox table's DDL, as it does the ledger's, through `basis
ddl`. A route that writes no database makes the call at once, since it has nothing to undo, and a failure there
answers 502.

Identity is optional. A request with a token has it verified (signature, expiry, issuer and audience), and an invalid or
expired token is refused rather than treated as anonymous, so a broken sign-in in a UI fails loudly. A handler sees the
user as `User | None`, so the type checker makes it handle the anonymous case, and the user goes into every log record,
trace and request record. Tests and `basis dev` sign tokens with a development key that the settings refuse anywhere but
a developer's desktop.

A service that verifies tokens declares its user, `api.Service(..., user=User)`: a subclass of `api.User`, which holds
the subject, `sub`, with a field for each other claim the service needs, and a token without one is refused. The claims
basis verifies itself are not a service's to declare, and the user holds nothing else of the token. The settings name
the issuer, the audience, the algorithms accepted, the leeway for clock skew, and the keys: the sign-in system's JWK set
from its URL, fetched again when a token names a key it does not hold, or one public key from a file. Each is set
explicitly, since the sign-in system's details are not yet known, and none is guessed. Only asymmetric algorithms are
accepted, never `none` or HMAC, whose key would let every API that verifies a token make one too, and a key verifies
only an algorithm it fits. The development key is one fixed ES256 key, published, so that tests, `basis dev` and a UI's
development mode all sign with it; every app refuses it unless the machine is declared a desktop, by `desktop = true` in
`basis/desktop.toml` in the user's own configuration folder, which no repo, image or deployment's variables hold, and
refuses it in a pod or a container even then, but for an app built from a test's own settings. Tokens are verified with
PyJWT, which every basis environment already holds.

`basis call ROUTE` runs a handler in-process with its real or fake resources and prints the response, so an agent
exercises an endpoint without starting a server. Every request in production is recorded in the run store: the request,
each resource call and its result, the writes, the response, and the code's and the framework's versions. `basis replay`
reruns one recorded request with today's code against the recorded results, touching nothing live, and `basis regress`
replays a sample of recorded requests and lists the responses that changed. Both run on developers' desktops, which can
read the store. How long records are kept is out of scope for this design.

The contract tests check that the app builds on fakes, that every route declares what it must, that `openapi.json`
matches the code and that a breaking change to it is flagged, that the API survives property-based requests generated
from its own schema (Schemathesis, run on fakes), that handler modules import nothing they may not, and that the agent
files match the installed version.

## Jobs: basis.job

`basis.job` is etl-framework's job contract, with its behaviour unchanged: typed inputs and outputs, a pure transform,
the framework's checks and the job's own before any write, all-or-nothing publishing with a ledger row in the same
transaction, `resume` from the snapshot, and `regress`. [etl-framework's
design](https://github.com/jacobragsdale/etl-framework/blob/main/docs/design.md) holds its decisions and the reasons for
them, and they stand. What changes is that its plumbing moves into `basis.core`, its command becomes `basis` (`etl run`
becomes `basis run`), and its jobs can live in a repo beside an API.

## The run store and fixtures

Every package writes its run data in one format. A run, a request or a decision is a manifest, a JSON file at
`runs/PACKAGE/NAME/RUN_ID.json`, that names the run's datasets by the hash of their content, and each dataset is one
Parquet file at `objects/HASH.parquet`. A reference table that each of a day's hourly runs of a job loads unchanged is
then stored once.
There is one store per environment, a path or URL read through `pyarrow.fs` as optim's and etl-framework's snapshot
roots are, and DuckDB reads the manifests and the Parquet directly when someone wants to query across runs.

A dataset's hash is the SHA-256 of its object, the Parquet file itself, which is written once with fixed settings
(zstd, statistics, the Arrow schema kept, and no dictionaries) and hashed as it is written, so storing a dataset
copies nothing and holds only the writer's buffers. Parquet writes no bytes under a null, and without dictionaries
pyarrow's writer writes the same bytes however a table is chunked or sliced, so equal data gets one hash however it
sits in memory. With dictionaries the bytes follow the chunks, which is why they are off; a test holds the writer to
this on a table of many pages. A column's name, order and type are part of the hash, so two datasets share an object
only when it reads back as each of them, and `sha256sum` checks an object against its name. The hash depends on the
settings and on pyarrow's version, whose name the file records: a change to either may store a dataset once more
after it, which costs space and never reads wrong data. Hashing a separate canonical encoding instead would keep the
hash apart from the settings, at the cost of encoding every dataset twice and, to stay apart from chunking, copying
it whole first: measured on a dataset of 200 MB in four chunks, 236 MB at peak against 36 MB. An object is written
once, and every file in the store goes to a hidden name that is renamed into place, the objects before the manifest
that names them, so a reader never sees half a file or a manifest whose objects are missing.

A manifest with a label, such as a regress set, keeps its objects. Deleting old runs is garbage collection, as in git:
delete the manifests a retention policy no longer keeps, then delete every object that no remaining manifest names.
An object that no manifest names may belong to a run still writing, whose manifest comes last, so the collector
spares every object younger than a grace period longer than any run. Writing that policy is out of scope here.

Fixtures are small, synthetic and in git, as a CSV per dataset with a manifest, so they diff and an agent can read them.
Client data never goes in git. A test that needs real data names a run by id and runs only where the store is reachable,
and basis has no tool for scrubbing client data out of a snapshot, because a generic scrubber is where leaks come from.

`basis.core` hands each package its data as Arrow. `basis.job` works in polars, which keeps nullable integers, decimals
at their scale and zone-aware datetimes exact. optim, provisionally, works in pandas. `basis.api` works in pydantic models, and in
pyarrow for its Arrow responses.

## Agents

| File | Read by |
|---|---|
| `AGENTS.md` at the root, about a hundred lines that map the repo and list its commands | Cursor, GitHub Copilot (VS Code and CLI), Codex and opencode |
| `CLAUDE.md`, containing only `@AGENTS.md` | Claude Code, which reads `AGENTS.md` itself only when no `CLAUDE.md` exists |
| `.agents/skills/` | Cursor, GitHub Copilot, Codex and opencode |
| `.claude/skills/`, a copy | Claude Code, which reads no other skills directory |

`basis sync` writes each part's block of `AGENTS.md` and its skills, prefixed with the part's name, and a contract test
fails when the copies differ from the installed versions. The skills are copies rather than symlinks because Git for
Windows checks a symlink out as a text file by default.

basis configures no MCP servers, because each agent keeps its MCP settings in its own file (`.vscode/mcp.json`,
`.cursor/mcp.json`, `opencode.json`, `.mcp.json`, `.codex/config.toml`) and no two share one. The agent's tool is the
`basis` command instead, which every agent runs with no setup, on Windows and in WSL. Every command prints one line per
failure, starting with `ERROR` and giving the reason and the fix, and never thousands of lines of progress, so a failure
is one grep away; Anthropic's C-compiler experiment found the same.

## Gates without CI

The repos have no CI, so the gates run on the desktop, where the agent works. The commit hook formats and lint-fixes the
staged files with ruff and then runs `basis check`. The push hook runs `basis check --full`, which adds `regress` where
the run store is reachable and the QA tier where QA settings exist. Both hooks are committed under `.githooks/`, and the verbs
a developer runs on a clone (`basis check`, `sync` and `new`, and `dev` and `doctor` once they exist) set its
`core.hooksPath` when it is unset, so a fresh clone has them after its first check, and `basis check` fails, with that
fix, when they are missing. The verbs a container or a scheduler runs, such as `basis run`, leave git alone.

The QA tier runs a repo's parts against the QA databases from the desktop, with the safeguards of optim's and
etl-framework's end-to-end suites: it refuses to run unless every configured source carries the QA marker, and writes
only to an allowlist of QA tables and the QA store. With no Docker on the desktops, it is also the only place a repo's
code meets a real database before production. All of it is declared in the repo's `pyproject.toml`, as
`[tool.basis.qa]`, and nothing is inferred: the one settings file the tier reads, never the process's environment, so a
variable set for production never mixes in; the marker, which every database that file configures must carry, in the one
server or database a SQL Server connection string names, its write block's included, or in an Oracle DSN; the QA stores,
one of which the file's run store must be; and the tables it may write, by database, ledgers included. The checks run
before anything does, for every part at once, and the databases a part gets refuse any other table as they write. Each
part reads its own runs from its own table, `[tool.basis.qa.job]` for jobs, each with what `basis run` takes. QA
settings "exist" where the repo declares the tier and the machine has that file; elsewhere the push hook says it skipped
it.

A reviewer judges what a change does. `basis review` writes the pull request's summary: the outputs `regress` saw
change, the API contract's diff, the versions, and the commit that was checked, so the reviewer reads a summary of
behaviour rather than lines of agent-written code. `basis check --full` records what it found on each commit it checks
with nothing uncommitted, in the git folder the clone's worktrees share, and `basis review` prints that record. An
output `regress` sees change fails no check, since a change may be meant: the summary shows it, and the pull request
says why.

A hook can be skipped with `--no-verify`, and agents do skip it. `AGENTS.md` says never to, and the synced Claude Code
settings deny `--no-verify`, but until CI exists the gates are only as strong as the hooks, and the commit `basis
review` names is how a reviewer sees which commit passed. The generated `azure-pipelines.yml` already runs `basis check
--full`, so the day the firm builds CI, a branch policy on `main` that requires that pipeline turns the same checks into
the gate.

Loop time, from an edit to every check passing, is measured by the eval. If the commit hook passes a budget of a minute,
its tests move to the push hook, and the commit hook runs only the tests a change touches.

## Evals

basis is judged by whether agents succeed in it, so it carries an eval. Twenty to fifty tasks drawn from real tickets
run in a generated repo, on the Harbor setup from the maintainer's harness-evals plan, graded by basis's own checks: the
contract tests, `regress` and the QA tier. Each task runs several times per variant, and the eval reports the pass rate,
how consistently a task passes across runs, the loop time and the tokens spent. It tests basis itself: whether a new
error message, skill or doc page helps agents or, as Cursor found of many of its own changes, makes them worse.

The method follows Anthropic's: tasks from real failures, deterministic graders where possible, the result graded rather
than the path, and repeated trials. A task no run ever passes is usually a broken task. Infrastructure alone moved
Anthropic's scores by six points, so a difference under three points is not a finding until the setups match.

## Versions

Python is 3.14, as optim and etl-framework require, set up with uv only.

## Decided against

| Option | Why not |
|---|---|
| A UI inside basis | One language per project keeps each toolchain, its checks and its docs whole; the UI is basis-ui. |
| Repos that hold an API and its UI | Each repo has one toolchain; the firm already keeps most UIs apart from their APIs. |
| C# services | One backend language keeps the stack small; engineering writes Python APIs too. |
| Rust services | Rust's SQL Server drivers are weak (the original `tiberius` crate lives on in forks, and sqlx dropped its SQL Server driver), its builds slow an agent's loop, and memory is not the cost that matters most. |
| A command per kind of part | People move between their product's repos; one `basis` command with the same verbs everywhere spares them working out which tool a repo uses. |
| `basis-core` as something users depend on or read about | It is plumbing; each part's docs are complete without it. |
| Independent versions per package | A repo with two kinds of part would need versions that agree, and owners pull upgrades themselves; one version cannot disagree with itself. |
| Pushing upgrades to repos | Owners relock when they choose; `basis upgrade` makes that one command. |
| Docker on the desktops | Not available. The QA tier reaches real databases instead. |
| MCP servers | Every agent reads MCP settings from a different file. |
| Shared datasets across packages | They matter to optim, so they stay optim's. |
| Code shared between `basis.job`, `basis.api` and `optim` | Each depends on `basis.core` only, so one cannot break another. |
| Core code shaped by one part | A part's design can change, or the part can go, and core's must not change with it; a mechanism only one part uses is one a job could use as it stands. |
| Lint and format rules in prose | An agent eventually breaks a rule nobody checks. |
| DVC or Git LFS for run data | Each adds a tool and storage credentials to every desktop, and the packages already own their storage. |
| A tool that scrubs client data from snapshots | A generic scrubber is where leaks come from; fixtures are generated instead. |
| Copier or cookiecutter templates | Fifty repos of copied templates drift; `basis sync` rewrites the framework's files from the installed version. |

## Facts behind the decisions

Each fact was checked on 2026-11-06 against the source named, unless it says secondhand.

| Fact | Source |
|---|---|
| A SQL Server snapshot-isolation defect fixed in etl-framework is likely still in portfolio-optim's `sources/mssql.py` | etl-framework's design page |
| Claude Code reads `.claude/skills/` only, and `AGENTS.md` only when no `CLAUDE.md` exists; Cursor, Copilot, Codex and opencode read `.agents/skills/` | each tool's docs |
| No two of Cursor, VS Code's Copilot, the Copilot CLI, opencode, Claude Code and Codex read the same MCP configuration file | each tool's docs |
| On SWE-Sharp-Bench, agents resolved about 40% of C# tasks, against about 70% of Python tasks on SWE-bench Verified, on different task sets | arXiv 2511.02352 |
| OpenAI's harness-engineering team kept `AGENTS.md` to about 100 lines and enforced its architecture with linters whose messages carry the fix | a third-party copy of OpenAI's post, since openai.com refuses automated fetches |
| Anthropic measured a six-point spread on Terminal-Bench 2.0 from infrastructure alone, and advises skepticism of differences under three points | anthropic.com/engineering |
| Every basis environment already holds PyJWT with its crypto extra, through mssql-python, azure-identity and msal (checked 2026-11-07) | `uv tree --invert --package pyjwt` |

## Not yet verified

Each item but the last two is a spike in [Phase 0 of the roadmap](roadmap.md#phase-0-spikes), and the last two are
**(work)** questions for the firm; each needs a written yes or no before the step that depends on it:

- that uv installs on a Citrix desktop without admin rights, on Windows and in WSL, and reaches the Azure Artifacts feed
  from both;
- that Python namespace packages (`basis.core` across distributions) work with uv workspaces, ty and grimp;
- that the AKS cluster's ingress can route `/api` on a UI's host to the API's service;
- which agents the Harbor setup can drive;
- which algorithm the sign-in system signs its tokens with, how it distributes its keys, its issuer and audience, and
  the names of its claims. HMAC stays refused whatever the answer;
- whether the firm's services read `Idempotency-Key` as the bare token basis sends, or as a structured-field string
  in quotes, as the IETF draft has it. A service drops a duplicate either way, since every attempt at a call carries
  the same key.
