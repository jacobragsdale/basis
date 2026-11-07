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
| `basis new PART NAME` | Adds an API, a job or a model to the repo | Every part |
| `basis sync`, `basis upgrade` | Rewrite the framework's files from the installed version; relock basis, sync, check and regress in one step | Every part |
| `basis doctor`, `basis docs` | Check the desktop's setup and print the fix for each problem; print the docs for the installed version | Every part |
| `basis dev`, `basis serve`, `basis call ROUTE` | Run the API on fakes; run the API; run one route in-process and print its response | An API |
| `basis records export` | Write chosen recorded requests and responses as JSON, for a UI's fixture mode | An API |
| `basis run NAME`, `basis resume RUN`, `basis describe NAME` | Run a job or a model, and with `--publish` write its outputs or orders; finish a publish that failed partway; print its params, inputs and outputs | Jobs or models |
| `basis ddl NAME`, `basis sweep RUN DIR` | Print a job's `CREATE TABLE` statements; decide a model's saved run once per params file | Jobs; models |

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
| `optim` | Portfolio optimizers: portfolio-optim's model contract and engine | `basis-core` |

Every package is published to the internal PyPI feed. `basis-core` is internal: no repo depends on it by name, no user
code imports it, and no user doc mentions it. It arrives as a dependency of the packages a repo does depend on, and
brings the `basis` command with it. Each kind of part adds its own verbs to that command through entry points, so a job
author's docs, skills and commands are complete without a word about the core underneath.

The packages import as `basis.job` and `basis.api`, with the core as `basis.core`, one namespace that each distribution
adds to. An import contract in this repo's checks keeps the layers apart: `basis.core` imports none of the others, and
`basis.job`, `basis.api` and `optim` import `basis.core` and never each other.

`basis.core` is the one place basis shares code rather than only formats, and the reason is the code itself. optim and
etl-framework each carry their own Oracle and SQL Server sources, settings, logs, storage and snapshots, and the copies
have already drifted. While building etl-framework, a defect was found and fixed in how its SQL Server reads begin a
snapshot transaction; portfolio-optim's `sources/mssql.py` reads the same way, and likely has the defect still. An API
framework would have been a third copy. The packages above `basis.core` share nothing with each other, so a change to
jobs cannot break an optimizer.

optim stays separate in its code: its own package, with its own model repos, generator, skills and docs, because
optimizers are a distinct kind of code with its own workflow (sweeps, trials, research from an IDE) and quants as its
main authors. It gives its plumbing to `basis.core` and keeps its contract, its engine, `optim.cvxpy`, rounding and
acceptance, `reserve` and `order`, `sweep` and `trial`, the shared holdings and securities datasets, and pandas. Its
verbs come under `basis` like every other part's, and `optim` stays as an alias for the same command. Its orders are
written through `basis.core`'s publishing, with a ledger row in the same transaction, so a failed or retried run never
writes an order twice and `resume` finishes one that stopped partway. No repo depends on optim yet, so it moves with
nothing to migrate.

## One repo, one version

Every package lives in this repo, a uv workspace, so a change that crosses packages, such as a new resource in
`basis.core` and its use in `basis.api`, is one pull request. This repo's own CI runs on GitHub Actions, with SQL Server
and Oracle service containers for the integration tests, and runs only the jobs whose paths changed.

Every package is released together, under one version and one tag, such as `v0.7.0`. Each package depends on
`basis-core` at exactly its own version, so a repo's lock can only hold basis packages from a single release, and an
owner never has to work out which versions go together. Some releases change nothing in some packages; their notes say
so. basis stays at 0.x until a real repo runs it in production, as portfolio-optim's own policy was.

A developer starts a repo with `uvx`, which fetches a package from the internal feed and runs its command from a cached
environment, installing nothing into a project: `uvx basis-api new app positions`, `uvx basis-job new repo
nightly-loads`, or `uvx optim new repo sma income` (the repo and its first model). uv installs per user, with no admin
rights. Each developer's user-level `uv.toml` names the internal feed as the default index, not an extra one: under uv's
default first-index strategy, a name the feed holds is then never looked up on public PyPI, where an unrelated `optim`
exists. The feed proxies public PyPI for everything else.

Repos pull new versions; nothing pushes them. `basis upgrade` relocks the basis packages with uv, runs `basis sync`,
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
| `models/MODEL/` | One folder per optimizer model, on `optim` |
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
of the `openapi.json` that this repo's example API produces.

- **The OpenAPI document.** Each API commits its `openapi.json`. Each operation's id is its handler's name, so a
  generated client's functions read `getPositions` rather than FastAPI's default `read_positions_positions_get`, and
  every model is strict. The API's breaking-change check compares the document with the one at its last release tag, and
  fails when a field is removed, renamed or changes type. Adding a response field is never breaking, because basis-ui's
  schemas drop fields they do not know. Requests are the other way round: an API refuses unknown request fields, so an
  API that accepts a new one deploys before the UI that sends it.
- **Errors.** Every error is an RFC 9457 problem details response, from one exception map.
- **Identity.** A UI sends the user's JWT as a bearer token, and the API verifies it. A missing token is anonymous; an
  invalid or expired one gets a 401.
- **Datasets as Arrow.** A route that returns a dataset returns Arrow when the request asks for
  `application/vnd.apache.arrow.stream`, which is how parallax, basis-ui's grid, receives its data.
- **One origin.** A UI and its API sit behind one host on the AKS cluster, the ingress routing `/api` to the API and the
  rest to the UI, so the browser makes no cross-origin calls. Where that routing is not possible, the API declares the
  origins it allows, per environment; an empty list means the same origin only.
- **Recorded responses.** `basis records export` writes chosen recorded requests and responses as JSON files in the
  format basis-ui's fixture mode reads. They hold client data, so they go in a folder git ignores, never in a commit.

## APIs: basis.api

FastAPI already owns the request loop: routing, dependency injection, validation and OpenAPI. What each team reinvents
is the app around that loop and the I/O inside it, so those are what `basis.api` owns. A service declares its routes,
its resources and its settings, and the core builds the FastAPI app from them, with the same middleware, lifespan, error
handlers, health endpoints and shutdown in every repo.

A resource is anything a handler reaches outside the process: a database, another HTTP service, a cache, the clock, new
ids. Each is declared by name, and the core opens it in the app's lifespan, puts a deadline on every call, traces the
call, and provides a fake of it for tests. Handler code reaches the outside only through its resources, and the import
contract that keeps optim's models pure keeps handlers away from database drivers, HTTP clients and environment
variables. "Pure" cannot mean "no I/O" for a handler that reads, decides, and reads again; it means "I/O only through
the core", which lets the core bound, observe and record every call.

Request bodies and settings are strict models: an unknown key is refused and no value is coerced, so a misspelled field
fails at the boundary instead of being silently ignored. Every route declares its deadline, its response model and its
errors. Database writes in a request commit only after the handler returns and its response validates, and calls to
other services that change state go after that commit. A declared pure compute function runs in a process pool, off the
event loop, because CPU-bound quant code inside an `async def` stalls every request on that worker. An unexpected error
is logged once, with its traceback.

Identity is optional. A request with a token has it verified (signature, expiry, issuer and audience), and an invalid or
expired token is refused rather than treated as anonymous, so a broken sign-in in a UI fails loudly. A handler sees the
user as `User | None`, so the type checker makes it handle the anonymous case, and the user goes into every log record,
trace and request record. Tests and `basis dev` sign tokens with a development key that the settings refuse anywhere but
a developer's desktop.

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
Parquet file at `objects/HASH.parquet`. The morning's holdings, loaded by twenty optimizer runs, is then stored once.
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
at their scale and zone-aware datetimes exact. optim works in pandas. `basis.api` works in pydantic models, and in
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
the run store is reachable and the QA tier where QA settings exist. Both hooks are committed under `.githooks/`, and any
`basis` command sets the clone's `core.hooksPath` when it is unset, so a fresh clone has them after its first `basis`
command, and `basis check` fails, with that fix, when they are missing.

The QA tier runs a repo's parts against the QA databases from the desktop, with the safeguards of optim's and
etl-framework's end-to-end suites: it refuses to run unless every configured source carries the QA marker, and writes
only to an allowlist of QA tables and the QA store. With no Docker on the desktops, it is also the only place a repo's
code meets a real database before production.

A reviewer judges what a change does. `basis review` writes the pull request's summary: the outputs `regress` saw
change, the API contract's diff, the versions, and the commit that was checked, so the reviewer reads a summary of
behaviour rather than lines of agent-written code.

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

## Not yet verified

Each item is a spike in [Phase 0 of the roadmap](roadmap.md#phase-0-spikes), and each needs a written yes or no before
the step that depends on it:

- that uv installs on a Citrix desktop without admin rights, on Windows and in WSL, and reaches the Azure Artifacts feed
  from both;
- that Python namespace packages (`basis.core` across distributions) work with uv workspaces, ty and grimp;
- that the AKS cluster's ingress can route `/api` on a UI's host to the API's service;
- which agents the Harbor setup can drive.
