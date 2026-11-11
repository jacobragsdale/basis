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
| `basis check --full` | `check`, plus `regress` where the run store is reachable, the QA tier where QA settings exist, and a mutation report in WSL or Linux | Every part |
| `basis review` | Writes a pull request's summary: what `regress` changed, the API contract's diff, the mutation report, the versions, and the commit that was checked | Every part |
| `basis smoke --env ENV` | Checks a deployed environment: the API's contract and readiness, each route's read-only example, and a dry run of a job | An API or jobs |
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
| `basis-core` | Internal: database sources for Oracle and SQL Server, the resources jobs and APIs share, settings and environments, JSON logs, storage, the run store, snapshots, publishing with a ledger, the machinery of `replay`, `regress`, `new` and `sync`, contract-test helpers, and the `basis` command | Nothing |
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
  breaking-change check compares the document with `openapi.released.json`, the copy its last release committed beside
  it, which `basis release-document VERSION` writes at a release (decided on 2026-11-09, step 7.12), so it reads no
  tag and no history, and fails when a field is removed, renamed or changes type, or an error's type answers another
  status; with no released copy committed yet, it passes and says so. Adding a response field is never breaking, because basis-ui's
  schemas drop fields they do not know. Requests are the other way round: an API refuses unknown request fields, so an
  API that accepts a new one deploys before the UI that sends it, and it takes a time only with its offset, as it sends
  one: a route whose request or response may hold a time without its offset is refused when it is declared.
- **Numbers.** Every number is a JSON number, never a string, and NaN and ±inf are null, in JSON and Arrow alike. The
  UI rounds, once, at display, as [Numbers](#numbers) says.
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
- **Display formats.** A dataset's number column may declare how it is shown, on its float field, as
  `Annotated[float, api.format.money("USD")]` or `api.format.pct(2)`, so pages never choose decimals. basis-api writes
  it as a JSON object: in Arrow, as the field metadata key `basis.format`, whose value is the object as a JSON string,
  and in the document as `x-basis-format` on each numeric property's schema, the columns declared for an Arrow response
  among them. parallax and @basis/ui's number components format each column by it, and a column without it gets the
  formatter's default for its type. The kinds, agreed with basis-ui on 2026-11-08, are `number`, `money`, `pct`, `bps`,
  `price` and `price32`, and [the contract page](contract.md#display-formats) gives each one's fields. Rates and
  spreads are fractions: 0.0525 is 5.25%, or 525 bp.
- **One origin.** A UI and its API sit behind one host on the AKS cluster, the ingress routing `/api` to the API,
  removing the prefix, and the rest to the UI, so the browser makes no cross-origin calls. The document's paths carry no
  `/api`, and a UI's client adds it. Where that routing is not possible, the API's settings declare the origins it
  allows, per environment; an empty list, the default, means the same origin only. basis-api can also serve a built UI
  folder at `/` from the same app, with its routes under `/api`, which gives one origin with no ingress split; ctzd's
  apps do this (see [ctzd](#ctzd-a-consumer)). The document's paths stay unprefixed either way.
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
their spans record each value, so that a recorded request replays them. An API's code reaches the outside only through
its resources, and an import contract, as basis-job's keeps job code pure, keeps it away from database drivers, HTTP
clients and environment variables. "Pure" cannot mean "no I/O" for a handler that reads, decides, and reads again; it
means "I/O only through the core", which lets the core bound, observe and record every call. Another service, the
clock, ids and tracing are `basis.core`'s, which jobs declare too, and `basis.api` builds on them: see [What jobs and
APIs share](#what-jobs-and-apis-share). A request's transaction, its outbox, the cache, the compute pool and identity
are the API's own.

An API's code sits in layers, each a module that basis knows by its name, at any depth, so `bonds/handlers.py` is a
handlers module too:

| Module | Holds |
|---|---|
| `service.py` | The controller: its routes |
| `handlers.py` | One function per route, and the only place resources arrive |
| `repository.py` | All the SQL and every call to another service, each function taking the resource it needs and returning domain types |
| `domain.py` | Pure types and rules, with no I/O, no FastAPI and no `basis.api` |

Beside them, two modules declare: `resources.py` the resources and settings, and `errors.py` the errors. `basis check`
holds the layers with grimp, which core already uses, and no configuration file to keep: imports go down the table
only; no layer imports a database driver, an HTTP client or storage, as basis-core's purity check lists them, nor reads
the environment, the clock or new ids itself, in every API repo and not only in this workspace's example; a domain
module stays pure, as a compute module is today; and a handler passes a resource on and never calls its methods
itself, so the SQL stays in repositories. A test's module and a job's are no layer. Every API repo then has the same shape, and the checks keep it. The
declarations sit between the repository and the domain: every layer imports them, and they import only domain modules,
as `errors.py` imports the exceptions it gives a meaning and `resources.py` a cache's domain type. An import counts
directly or through a module of the repo's that is no layer, such as a helper. Pure means what it means for a compute
module: nothing of basis, no web framework, server or settings, no database driver, HTTP client or storage, and none of
the standard library's sockets, processes or databases. A handler's parameter that `api.given` gives may be passed as
an argument, read, or called itself, as a compute function is, but no method of it is called; the check reads the
handler's code, each failure one `ERROR` line with its fix.

A domain's errors are plain Python exceptions, defined in its domain module, because the same domain code runs in jobs,
which have no HTTP. The API gives each its HTTP meaning once, in `errors.py`, as
`MATURED = api.error(domain.Matured, status=422, title="The bond has matured")`, and its routes list those. A route answers
the most specific meaning it declares for the exception's class, its problem's type the exception's name in kebab case
and its detail the exception's message; a domain exception a route does not declare is a 500, as any other is.

Request bodies and settings are strict models: an unknown key is refused and no value is coerced, so a misspelled field
fails at the boundary instead of being silently ignored. A number in a request body is a JSON number, as it is in a
response (see [Numbers](#numbers)), and a time in one carries its offset. Query and path parameters, which arrive as
text, are read by each type's grammar. Every route declares its deadline, which caps every
call its request makes, its response model and its errors, and an error it does not declare answers 500. Database writes
in a request commit only after the handler returns and its response validates, and calls to other services that change
state go after that commit. A commit that fails or passes its deadline once it has begun may have committed or not, so
it answers its own problem, `outcome-unknown`, which a UI does not retry as it may a 503, and its log says so with why.
A path to another service is a template whose values basis encodes, each as one segment, as
`pricing.get("/prices/{security}", CleanPrice, segments={"security": security})`, so a request's value never reaches
another of its endpoints, and a path that holds a query, a fragment or a dot segment is refused. A declared pure compute function runs in a process pool, off the event loop, because
CPU-bound quant code inside an `async def` stalls every request on that worker. An unexpected error is logged once, with
its traceback.

A dataset route may return a pandas frame or an Arrow table, declared as `response=api.Table[Row]`, decided on
2026-11-08. basis checks it once against the row model's columns and types (names, types, nulls and time zones), never
row by row, and a mismatch is a defect, a 500, whose request's writes roll back. Since no row is checked by itself, a
`Table` row model may not declare value constraints, and one that does is refused when it is declared.
`Reader.fetch_table(Row, SQL, ...)` reads Arrow straight from the driver, and the route's JSON is rendered column-wise
from the Arrow, as [the contract](contract.md#datasets-as-arrow) has it. This is the path ctzd's apps use.

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
answers as its kind of service error does (see [What jobs and APIs share](#what-jobs-and-apis-share)).

Identity is optional. A request with a token has it verified (signature, expiry, issuer and audience), and an invalid or
expired token is refused rather than treated as anonymous, so a broken sign-in in a UI fails loudly. A handler sees the
user as `User | None`, so the type checker makes it handle the anonymous case, and the user goes into every log record,
trace and request record. A route that is not a GET changes what the service holds, so its handler asks for the user,
or the route declares `anonymous=True`, that anyone may make it, decided on 2026-11-09 (step 7.10); the example's write
asks for a signed-in user. Tests and `basis dev` sign tokens with a development key that the settings refuse anywhere
but a developer's desktop, and that no app serves on a socket other machines reach. A refused token's 401, and a 503
while the keys cannot be fetched, name nothing of the API's sign-in, its issuer, audience or keys' URL, which the log
line names instead.

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
refuses it in a pod or a container even then, but for an app built from a test's own settings, and for an app on its
fakes alone in `basis dev`, on 127.0.0.1, and `basis call --fake`, in its process, which verify with it, for the issuer
`basis-dev` and the service's name, where their settings name no identity, as `basis check`'s does: no one else can
reach one, and its data is the fakes'. `api.serve` refuses the development key on any host but this machine's loopback,
whatever settings it is given. Tokens are verified with PyJWT, which every basis environment already holds.

`basis call ROUTE` runs a handler in-process with its real or fake resources and prints the response, so an agent
exercises an endpoint without starting a server, and `call` and `dev` are library functions too. Where a deployment
turns recording on, every request is recorded in the run store: the request, each resource call and its result, the
writes, the response, and the code's and the framework's versions. Recording is off otherwise, which ctzd needs (see
[ctzd](#ctzd-a-consumer)), in place of recording every request in production. `basis replay`
reruns one recorded request with today's code against the recorded results, touching nothing live, and `basis regress`
replays a sample of recorded requests and lists the responses that changed. Both run on developers' desktops, which can
read the store. How long records are kept is out of scope for this design.

The contract tests check that the app builds on fakes, that every route declares what it must, that `openapi.json`
matches the code and that a breaking change to it is flagged, that the API survives property-based requests generated
from its own schema (Schemathesis, run on fakes), that the layers hold, and that the agent files match the installed
version. `basis check` builds the app on the fakes its service declares, from its settings' defaults alone, never the
environment, and on the development key where it verifies tokens; Schemathesis then sends it, in the same process
through `from_asgi`, 50 requests its document allows and 50 it refuses for each operation, the same on every run, so
none is flaky. A 500, a status, content type or body the document does not list, or a refused request it accepts
fails the check, with the `basis call` that makes the request again. Schemathesis is each API repo's development
dependency, never basis-api's, so no image holds it.

## Jobs: basis.job

`basis.job` is etl-framework's job contract, with its behaviour unchanged: typed inputs and outputs, a pure transform,
the framework's checks and the job's own before any write, all-or-nothing publishing with a ledger row in the same
transaction, `resume` from the snapshot, and `regress`. [etl-framework's
design](https://github.com/jacobragsdale/etl-framework/blob/main/docs/design.md) holds its decisions and the reasons for
them, and they stand, but for polars. What changes is that its plumbing moves into `basis.core`, its command becomes
`basis` (`etl run` becomes `basis run`), its jobs can live in a repo beside an API, and its frames are pandas.

A job keeps its declarative shape, which has an API's layers already: its `Inputs` and `Outputs`, with their SQL in
`sql/` files, are its repository, and its `transform` and `check` its pure domain. A repo's `domain.py` can serve its
API's handlers and its jobs' transforms alike, under the same purity check.

`basis.core` and `basis.job` hand job code its frames as pandas DataFrames, decided on 2026-11-07 in place of polars.
Every number in them is a float64, as [Numbers](#numbers) has it, and strings, dates and times take pandas' nullable
and Arrow-backed dtypes from the job's declared row type, so a time keeps its zone. polars leaves the lock, and
pyarrow stays the I/O layer, for Parquet, the run store, Arrow responses and the lake. The firm's team, its Excel
habits, optim and the code to port are all pandas, and polars' gains matter little here: frame work was about 5% of a
measured optim run, and bulk movement goes through pyarrow. A job with a huge in-memory transform can convert to
polars itself.

## What jobs and APIs share

Jobs and APIs share one set of tools. Today basis-api alone has the HTTP client, the clock, ids and OpenTelemetry
tracing, and jobs get only databases and logs, so [phase 5 of the roadmap](roadmap.md#phase-5-resources) moves them
into `basis.core` and adds the rest of this section. The maintainer decided all of it on 2026-11-07, and asked that it
stay simple.

**Resources.** basis-api's resource model moves into `basis.core`: a resource is declared by name, every call that can
wait has a deadline, every call has a trace span, and every resource has a fake. The HTTP client (httpx2, with a proxy
and a CA bundle), the clock, ids and tracing move with it, so jobs get them too, and basis-api builds on core's. A job
declares them as it declares its datasets, so its transform stays pure and every run replays from its snapshot: a
`Fetch` is a GET that binds the interval and answers a JSON list of rows, which the snapshot saves, and is the one way
a job reaches another service; a `Clock` or `Ids` gives the transform values that the run's record keeps, so that a
replay is given them again. basis-api keeps what is a request's own, a route's deadline, which caps its calls, its
transaction, the outbox, the compute pool and identity, and the cache.

**Service errors.** Every resource and its fake raise one hierarchy of `basis.core`'s, so a failure means the same
whatever was called. `ServiceError` carries the resource, the status and the detail, and each kind has its rule for
retries and the response a route gives when nothing handles it:

| Error | Raised for | Retried by basis | Unhandled, a route answers |
|---|---|---|---|
| `Timeout` | The deadline passing, or a 504 | Never | 504 |
| `Unavailable` | A connection refused or reset, or a 502, 503 or 429 | On a GET only, within the deadline | 503 |
| `NotFound`, `Conflict`, `Rejected`, `Denied`, `BadReply`, `Failed` | A 404; a 409; a 400 or 422; a 401 or 403; a reply that does not match its declared model; a 500 and anything else | No | 502 |

A repository turns the ones that mean something into domain results, and anything else becomes the route's response,
so a handler needs no `try`. They replace `CallFailedError` and `DeadlineExceededError`. A fake fails on demand, as
`pricing.fails("/prices/X", Unavailable)`, so every error path is tested without mocks, and basis ships a contract
suite per resource, which its CI runs against the fake and the real resource, so a fake stays faithful. The set
follows Google's AIP-193 and AIP-194, Uber's YARPC codes and Meta's fbthrift, which all use a small, gRPC-like set and
retry only unavailable, idempotent calls.

**Traces and logs.** Traces go to Datadog. The OpenTelemetry SDK makes them, and basis posts them over OTLP/HTTP to the
Datadog Agent on each AKS node, at the full URL `BASIS_TRACES__ENDPOINT` names, so basis needs no Datadog library: in a
pod, the Agent's node-local Service (`internalTrafficPolicy: Local`), a fixed URL a deployment's settings can hold, or
`http://$(HOST_IP):4318/v1/traces` where the deployment sets the variable itself, since Kubernetes expands `$(HOST_IP)`
only in a container's own `env`, never in a ConfigMap ([the berth spike](spikes/berth.md), answer 7). It posts them with
httpx2 itself, rather than through the SDK's OTLP exporter, which reads `OTEL_*` variables whatever it is given and
sends through `HTTP_PROXY`: basis's settings are the only configuration, with one exception, decided on 2026-11-07.
basis reads `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES`, and only those two (`basis.core.telemetry`): where a
deploy platform sets them, as berth does per container, a process's spans carry the platform's service name and
attributes in place of basis's, and every log record that names its service names the platform's. They say what a
process is, never where its spans go, and they are the one source of a service's name per container: berth tags a
pod's metrics and its Datadog tags `<app>-<component>`, which basis cannot know, so without them logs and traces would
sit under one name in Datadog and metrics under another. An attribute that names another environment than the one the
process acts in is refused. Each run and each app has its own tracer
provider, and nothing sets OpenTelemetry's global one. Every run and every request is a trace, and every SQL statement
and HTTP call is a span; with no endpoint set, as on a desktop, spans are made and go nowhere, and nothing fails. Logs
stay JSON on stderr, as they are now, never files on NFS, and each record logged in a trace carries its `trace_id` and
`span_id` in OpenTelemetry's hexadecimal, which Datadog reads as they are ([the spike](spikes/datadog.md)), and a
`service` field, so Datadog links a log to its trace and its service. There are no custom metrics: Datadog's APM
derives request counts, errors and latency from the traces.

**Calendars.** The firm has no official business-day calendar, only several implementations, mostly of SIFMA's. basis
ships two, `sifma-us`, with its full and early closes, and `target2`, as data in `basis.core` built from SIFMA's and the
ECB's published schedules, with no pandas. A calendar says whether a date is a business day, adds business days, and
gives the previous business day and a day's early-close time. Each states the last date it covers, and asking about a
later one is an error, never a silent business day. A job names its calendar explicitly, as its module's `CALENDAR`.

**Relative intervals.** A scheduler runs the same command every time: berth's tasks have static arguments, and its
Control-M jobs one fixed time a day. So `basis run --interval` also takes an interval relative to the moment the run
starts, such as `previous-business-day`, `previous-month` or `today-7d..today`, decided on 2026-11-07. It resolves in
the zone and business days of the job's `CALENDAR`, which it requires, to dates, and the run's manifest records those
dates, so `replay` and `regress` rerun exactly the interval the run had. The Python API and the QA tier take dates.

**Messages.** Slack messages go through channel webhooks, as the firm already posts to its channels, and email through
the firm's SMTP relay. Both have fakes, and nothing is sent in tests, `replay`, `regress` or the QA tier. Their numbers
are written by a Python port of parallax's number formatter, decided on 2026-11-08, which reads the same display
formats as the UI (see [What a UI can rely on](#what-a-ui-can-rely-on)), so a number reads in a message as it does in
the grid. It is tested against the output parallax's Rust writes, every case of
`packages/parallax/crates/parallax-engine/tests/fixtures/format.json` in basis-ui. A job's message is an output, a
Slack channel's text or an email, which only `basis run --publish` sends, after every database has committed, and the
ledger of a database the message names records it as a publish is, with its text's hash, so a resume never sends
one twice and `--again` sends it again, decided on 2026-11-08 by the orchestrator on the maintainer's behalf. Alerts
that a job failed stay with Airflow and Datadog, not basis.

**Folders and Excel.** Portfolio managers live in Excel, and the files sit on NFS. A folder is a named path, which each
machine's settings give: `\\server\share` on Windows, and a mount path in WSL and in a pod. basis reads and writes Excel
with pandas' `read_excel` (calamine) and `to_excel` (xlsxwriter), in an optional extra, `basis-core[excel]`.
Every write goes to a temporary file that is then renamed into place, so a portfolio manager never opens a half-written
file.

**Mongo and SQL Server JSON.** The firm uses Mongo, and is moving that data to SQL Server's native `json` columns, so a
job that reads Mongo and publishes to SQL Server is the migration. basis reads Mongo, and writes to it as upserts by a
declared key, so a rerun is safe; it is an optional extra, `basis-core[mongo]`, with no Mongo transactions and no
ledger in Mongo. SQL Server's `json` columns work in reads, publishing and `basis ddl`.

**Azure Data Lake.** Many of the firm's jobs archive data from databases to Azure Data Lake (ADLS Gen2), so a job can
publish an output as Parquet to a declared lake path, partitioned by its run's interval, such as
`CONTAINER/TABLE/date=2026-11-07/`. It goes through `pyarrow.fs`, which the run store already uses, so it adds no heavy
dependency. The environment's settings declare how it signs in: an account key or a SAS token, as a secret, or Entra ID,
through workload identity in a pod and `az login` on a desktop. Each partition is written to a temporary path and
renamed into place, which the hierarchical namespace does atomically, and the archive counts as done only when its files
read back with the row count written. The job's database ledger records the archive like any publish, with the
interval, the path, the row count and the files' hashes, so a retry never archives twice, and `resume` and `--again`
work as they do on a table. Deleting the archived rows from the source database is a later step, and an explicit one.

**Environments.** Every command that touches the firm's systems needs `--env dev|qa|prod`, or `BASIS_ENV`, and has no
default, so no command reaches an environment nobody named. Each user keeps one file per environment, `basis/ENV.env` in
their own configuration folder, beside `desktop.toml`, holding its hosts, paths and secrets, and every repo they work in
shares it. A pod gets its settings from its deployment. A repo's `.env.example` lists the names it needs. basis-job's
`run(..., env="qa")` names an environment, as `--env` does, and the QA tier reads the user's `qa.env`, still alone.
Database logins stay as the settings have them: a SQL Server connection string, which may carry a password or Kerberos,
and Oracle's user and password. Most people use passwords, and some Kerberos.

Prod is read-only unless the deployment sets one flag: without it, a run whose plan publishes or writes anything is
refused at the start, with one `ERROR` line. That stops accidents, not malice, since anyone can set a flag; the real
control is a read-only prod login for desktops, and the docs say so. Desktops write in dev and QA. `basis doctor --env
ENV` checks that each login works and each folder is reachable.

None of this brings Docker to the desktops: cross-platform still means the image in AKS, Windows on the Citrix desktops,
and WSL.

## Numbers

Every number basis handles is a float64, from the database to the browser, decided on 2026-11-08. One simple
primitive type then runs the whole way, and the browser's Number is a float64 too. The UI rounds once, at display, by
Excel's rule: 15 significant digits, half away from zero, then the decimals shown, which parallax's formatter already
implements. This replaces the rules that kept integers and decimals exact, in frames and as JSON strings.

- **Frames.** Every number in a frame is a plain numpy float64 column, with NaN for missing: not Arrow-backed, not
  decimal, and not int64. Identifiers, such as ids and account numbers, are text, and a query selects them as text.
  Strings and dates keep their dtypes, as [Jobs](#jobs-basisjob) says.
- **Reads.** The drivers cast every numeric source type to float64 at the Arrow level: on Oracle, NUMBER in every form,
  and BINARY_DOUBLE; on SQL Server, decimal, numeric, money, smallmoney, float, int, bigint, smallint and tinyint.
  Oracle's BINARY_FLOAT and SQL Server's real widen through their shortest decimal, so 0.1 stays 0.1. A whole-number
  column that holds a value beyond 2^53 fails the query, naming the column, with the fix: select it as text if it names
  something, or `CAST AS FLOAT` if it is an amount.
- **Row models** take `float` for numbers, and an `int` or `Decimal` field is refused, with the same fix.
- **Writes.** A float published into an existing DECIMAL column binds as its shortest decimal text, its `repr`, so the
  column's scale rounds it: 2.675 becomes 2.68, not 2.67. Into an INT column, a whole float binds as integer text, and
  anything else is refused. NaN publishes as NULL, and ±inf is refused. `basis ddl` emits SQL Server `float` and
  Oracle `BINARY_DOUBLE`, and column kinds lose int and decimal.
- **`regress`** compares numbers at 15 significant digits, so a sum done in another order is not a change.
- **APIs.** Numbers are JSON numbers, never strings, and NaN and ±inf are null, in JSON and Arrow alike. Dataset rows
  take `float` for every number, and refuse `int` and `Decimal` fields, with the fix; an enum or `Literal` in one is
  text, and one of integers is refused, with the fix to use a `str` enum or a `Literal` of strings. Outside datasets,
  route models refuse `Decimal`, and `int` stays allowed, as a JSON number.
- **Rounding.** No gate refuses rounding. The UI rounds a number once, as it shows it, so a service gives numbers
  whole; business rounding, such as lot and tick sizes, stays in named, tested functions. A gate on `round()` refused
  `frame["traded_at"].dt.round("1min")`, had no exemption, and let `math.floor(x * 100 + 0.5) / 100` through, so it
  went (step 8.7).

## ctzd, a consumer

ctzd is the firm's citizen-developer platform, and since 2026-11-08 a consumer of basis. Its only UI is a React app on
@basis/ui and @basis/parallax, with a basis-api backend: one repo and one container per app. Citizen code never
imports basis directly; ctzd-core wraps it. ctzd never runs the `basis` command or `basis sync`, because ctzd owns
every file in its apps, so a ctzd app is not a basis repo, and the rule that a basis repo holds one toolchain does not
reach it.

The surface ctzd uses is basis-api's `Service`, dataset routes and `basis.api.build(service, settings=..., fakes=...)`;
the drivers; and later the calendars, messages and the lake. A change to that surface needs a note to ctzd. basis gives
ctzd:

- **A UI at `/`.** basis-api can serve a built UI folder at `/` from the same app, with the routes under `/api`, so one
  container serves the UI and its API on one origin.
- **Library functions.** `call` and `dev` are callable as functions, not only as verbs.
- **Settings in code.** `build(...)` with `Settings` made in code stays a supported seam. ctzd builds Settings from its
  own variables, so a ctzd app has no `BASIS_*` variables.
- **No recording by default.** Request recording to the run store is off unless a deployment turns it on.
- **Nothing that touches its files.** In a repo whose `pyproject.toml` has no `[tool.basis]` table, `basis` refuses
  every verb that reads or writes the repo's files, `sync`, `check`, `new`, `run`, `upgrade`, `review` and the rest,
  because ctzd's concern is an agent writing over its files. It allows `--version`, `doctor`, which checks the desktop
  only, as it does today, and `docs`, which prints the packaged docs, since those three write nothing. `sync`, `check`,
  `new` and `run` already refused there on 2026-11-08, and step 2.11 adds a test that holds every verb to it.
- **A number formatter.** The Python port of parallax's formatter, for Slack and email text (see
  [Messages](#what-jobs-and-apis-share)), which ctzd moves onto.

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

pyarrow is the I/O layer, for Parquet, the run store, Arrow responses and the lake. Job code works in pandas, as
[Jobs](#jobs-basisjob) says, and optim, provisionally, in pandas too. `basis.api` works in pydantic models, and in
pyarrow for its Arrow responses and its `api.Table` datasets.

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

The gates are only as strong as the settings they run with, so basis owns them (the review's step 8.1): `basis sync`
writes each repo's `ruff.toml`, `ty.toml` and `pytest.ini` from basis's settings and each part's share, as framework
files, and `basis check` refuses a `[tool.ruff]`, `[tool.ty]` or `[tool.pytest]` table in `pyproject.toml`, which would
sit beside them; a repo that ignored whole rule families, turned ty off and lowered its floor to 10 passed every check
before. The tools arrive pinned through each part's `dev` extra, as `basis-job[dev]`, so `basis upgrade` moves them with
basis. A repo takes an exception only as one rule at one path with its reason, `[tool.basis] exceptions`, and a coverage
floor under 90 only as such an exception too; `basis review` lists every one. A part whose design is provisional, as
optim's, gives no share, and a repo of it keeps its own settings until it does.

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
it. From phase 5, that file is the user's `qa.env`, which every repo shares, and the repo no longer names it (see
[What jobs and APIs share](#what-jobs-and-apis-share)).

`basis check` holds every generated repo's tests to the same gates:

- **Coverage.** Unit tests must cover a floor of the branches, `[tool.basis] coverage = 90` in the repo's
  `pyproject.toml`. Generators write 90, and a repo may change it, where its diff shows it; a repo without it fails
  `basis check`, with that fix. The floor counts unit tests alone, and leaves out repository modules, which the
  integration tests cover. It is not 100%: Google calls 90% exemplary, and finds that chasing 100% has diminishing
  returns. pytest-cov, a development dependency of every generated repo, measures it as basis configures it, never
  the repo's own `[tool.coverage]`: coverage.py's branch measure of every module under the repo's folder, one no test
  imports among them, but tests and repository modules, leaving out no line but `if TYPE_CHECKING:`, a script's
  `if __name__ == "__main__":` and what `# pragma: no cover` marks, its data in a temporary folder.
- **No mocks.** `unittest.mock` and pytest-mock are refused in a repo's tests, and fakes are the only test double. A
  file may opt out with a one-line comment that gives its reason, `# basis: allow-mocks -- WHY`; one that gives no
  reason is refused.
- **No network.** Unit tests cannot open sockets, so none can go flaky by reaching QA. basis-core's pytest plugin,
  which every repo's pytest loads, fails a test not marked `integration` that connects, binds or sends on an IPv4 or
  IPv6 socket, from any thread, whatever the code does with the error, Windows' default event loop's included, which
  connects through its proactor in C rather than the socket's methods. The socket pair an event loop makes to wake
  itself, which Windows makes over 127.0.0.1, and Unix sockets stay open, and FastAPI's `TestClient`, which calls the
  app in the process, needs nothing more. A driver that opens its sockets in C, as mssql-python's does, and a DNS
  lookup are out of its sight.
- **Mutation testing, as a report.** `basis check --full`, which the push hook runs, runs mutmut on the domain
  functions a change touches, and `basis review` reports what it found. It never blocks, and it runs in WSL or on
  Linux only, since mutmut needs `fork`. Coverage alone cannot tell an agent's empty test from a real one: Meta's ACH
  found that 49% of the tests that caught mutants added no coverage. The change is what the commit holds against where
  it branched, the merge base with the remote's default branch, `origin/HEAD`, or else the commit's parent, and every
  domain function in a first commit; a domain module is one named `domain`, at any depth. mutmut runs from the repo's
  development dependencies in a copy of its folder, so the repo gains no `mutants/`, and the record lists each mutant
  that survived with what it changed.

After each deploy, and from a desktop, `basis smoke --env ENV` checks the environment: the served `openapi.json` equals
the committed one; the API is ready; each route that declares a read-only example, `smoke=`, answers it, and its
response validates; and a job dry-runs on that environment's inputs for a recent interval, running its transform and
checks and publishing nothing. A failure blocks promotion to prod. That is all the end-to-end testing basis does for
now, with no journeys across services, test tenancy, Pact, chaos testing or canary analysis: Uber's end-to-end setup,
which works, needed tenancy in every service and still passed about 90% of attempts, and Google measured 14% of its
large tests as flaky.

A reviewer judges what a change does. `basis review` writes the pull request's summary: the outputs `regress` saw
change, the API contract's diff, the mutation report, the versions, and the commit that was checked, so the reviewer
reads a summary of behaviour rather than lines of agent-written code. `basis check --full` records what it found on
each commit it checks with nothing uncommitted, in the git folder the clone's worktrees share, and `basis review` prints
that record. An output `regress` sees change fails no check, since a change may be meant: the summary shows it, and the
pull request says why.

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
| mutmut 3.8 runs on Python 3.14, forking a process for each mutant, reads its settings from `[tool.mutmut]` and writes `mutants/` in the folder it runs in (checked 2026-11-07) | mutmut's `configuration.py`, and a run on the example |
| Schemathesis 4.33 calls an ASGI app in its own process through `from_asgi`, starting the app's lifespan and keeping it until the process ends unless it is shut down (checked 2026-11-07) | `schemathesis/python/asgi.py` |

## Not yet verified

Each item but the last two is a spike in [Phase 0 of the roadmap](roadmap.md#phase-0-spikes), and the last two are
**(work)** questions for the firm; each needs a written yes or no before the step that depends on it, but for the four
about Datadog, SQL Server's `json` type, Kerberos and the lake (spikes 0.5 to 0.8), whose steps build to their desk
research's best guesses, which the pilot then confirms at the firm:

- that uv installs on a Citrix desktop without admin rights, on Windows and in WSL, and reaches the Azure Artifacts feed
  from both;
- that Python namespace packages (`basis.core` across distributions) work with uv workspaces, ty and grimp;
- that the AKS cluster's ingress can route `/api` on a UI's host to the API's service;
- which agents the Harbor setup can drive;
- that the Datadog Agent on the AKS cluster accepts OTLP, on which host and port, that it collects containers' logs,
  and which format of trace id links a log record to its trace;
- that mssql-python handles SQL Server's native `json` type;
- that Kerberos logins work with mssql-python, in WSL and in a pod, and with oracledb, whose thin mode may not support
  Kerberos;
- that `pyarrow.fs.AzureFileSystem` signs in to the firm's storage account with workload identity in a pod and with the
  Azure CLI on a desktop, and that the account has the hierarchical namespace, so a rename is atomic;
- which algorithm the sign-in system signs its tokens with, how it distributes its keys, its issuer and audience, and
  the names of its claims. HMAC stays refused whatever the answer;
- whether the firm's services read `Idempotency-Key` as the bare token basis sends, or as a structured-field string
  in quotes, as the IETF draft has it. A service drops a duplicate either way, since every attempt at a call carries
  the same key.
