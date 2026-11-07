# The basis design

basis is a set of packages that give the firm's APIs, batch jobs, portfolio optimizers and web UIs one shape, so that
the agents that now write nearly all of their code produce the same kind of repo every time, and checks, rather than
people reading diffs, decide whether a change is right. "basis" is a placeholder name. The design was agreed on
2026-11-06 in a brainstorm that built on [portfolio-optim](https://github.com/jacobragsdale/portfolio-optim) and
[etl-framework](https://github.com/jacobragsdale/etl-framework). This page records the decisions and their reasons; [the
roadmap](roadmap.md) gives the build order, and which steps can run at once.

## The problem

Software developers and quants at a fixed income research firm write many APIs, batch jobs and web UIs, and each team
reinvents the same plumbing: database wiring, Dockerfiles, pipelines, settings and logging. The code is messy and its
standards differ from repo to repo, it is hard to test, it is hard to observe in production, and a change takes too long
to get there. The firm writes its code with AI agents (Claude Code, Cursor, GitHub Copilot, opencode and others), and
people rarely read it. What matters is that every repo has the same shape, that every guarantee is a check an agent can
run, and that everything runs from the command line.

Developers work on Windows 11 Citrix desktops with no GPU, and basis assumes they have no admin rights. The firm's data
sits in Oracle and SQL Server. The maintainer's team owns deployment, through Azure DevOps pipelines, and every
deployable runs in a container. A custom sign-in system protects the UIs and can issue JWTs; the APIs behind them have
no authentication today.

## The idea: the core owns the loop, and the checks are the review

optim and etl-framework showed that a framework that owns the run loop can keep the code it runs pure. The core loads,
the model or job computes, the core checks and saves, and every run replays from its snapshot. basis applies that idea
to every kind of code the firm writes, and adds one rule for a codebase nobody reads: every rule is a check, and every
check's failure says how to fix it.

A rule that lives only in a doc is a rule an agent eventually breaks. The teams that publish on agent-written code
converge on this. OpenAI's harness-engineering team enforced its architecture with custom linters whose error messages
carry the fix, and kept its agent file to a short map of the repo. Anthropic's Claude Code guidance says a line Claude
keeps missing belongs in a hook, and that the verifier must be nearly perfect, "otherwise Claude will solve the wrong
problem". Cursor's advice is typed languages, linters and tests, because "agents perform best when they have a clear
target to iterate against".

Replaying real work is the strongest of those checks, because a wrong answer rarely passes it. `basis regress` reruns
recent production runs and requests with the working tree's code and lists what changed, so a reviewer, human or agent,
sees what a change did to real outputs without reading the diff.

## The packages

| Package | Published to | What it is | Imports from basis |
|---|---|---|---|
| `basis-core` | Internal PyPI | Database sources for Oracle and SQL Server, settings, JSON logs, storage, the run store, snapshots, publishing with a ledger, `replay` and `regress`, `sync`, the docs plumbing, contract-test helpers, and the `basis` command | Nothing |
| `basis-job` | Internal PyPI | Batch jobs: etl-framework's job contract | `basis-core` |
| `basis-api` | Internal PyPI | HTTP APIs on FastAPI | `basis-core` |
| `optim` | Internal PyPI | Portfolio optimizers: portfolio-optim's model contract and engine, with its own `optim` command | `basis-core` |
| `@basis/ui` | Internal npm | React components, the app shell, the toolchain presets and fixture mode | Nothing |
| `@basis/parallax` | Internal npm | The grid, pivot table and charts, running in the browser | Nothing |

The Python packages import as `basis.core`, `basis.job` and `basis.api`, one namespace that each distribution adds to.
An import contract in this repo's checks keeps the layers apart: `basis.core` imports none of the others, and
`basis.job`, `basis.api` and `optim` import `basis.core` and never each other.

`basis.core` is the one place basis shares code rather than only formats, and the reason is the code itself. optim and
etl-framework each carry their own Oracle and SQL Server sources, settings, logs, storage and snapshots, and the copies
have already drifted. While building etl-framework, a defect was found and fixed in how its SQL Server reads begin a
snapshot transaction; portfolio-optim's `sources/mssql.py` reads the same way, and likely has the defect still. An API
framework would have been a third copy. The packages above `basis.core` share nothing with each other, so a change to
jobs cannot break an optimizer.

optim stays its own package and command, with its own model repos, generator, skills and docs, because optimizers are a
distinct kind of code with its own workflow (sweeps, trials, research from an IDE) and quants as its main authors. It
gives its plumbing to `basis.core` and keeps its contract, its engine, `optim.cvxpy`, rounding and acceptance, `reserve`
and `order`, `sweep` and `trial`, the shared holdings and securities datasets, and pandas. Its orders are written
through `basis.core`'s publishing, with a ledger row in the same transaction, so a failed or retried run never writes an
order twice and `resume` finishes one that stopped partway. No repo depends on optim yet, so it moves with nothing to
migrate.

## One repo, packages released on their own

Every package lives in this repo, a uv workspace for the Python packages and a pnpm workspace for the npm ones, so a
change that crosses packages, such as a new resource in `basis.core` and its use in `basis.api`, is one pull request
that one CI run checks. CI runs only the jobs whose paths changed, so a UI change never waits on parallax's Rust build.

Each package is released on its own tag, such as `core-v0.4.0` or `api-v0.3.1`. Nothing is republished unchanged, and an
optim model repo sees no new version for a change to the UI. `basis-core` follows semver strictly, and the other
packages depend on it by range. CI tests each package against the workspace's `basis-core`, and again against the oldest
`basis-core` its range allows, with `uv sync --resolution lowest-direct`, so a range that claims too much fails here
rather than in someone's repo. Each package stays at 0.x until a real repo runs it in production, as portfolio-optim's
own policy was.

A developer starts a repo with `uvx`, which fetches a package from the internal feed and runs its command from a cached
environment, installing nothing into a project: `uvx basis-api new app positions --ui`, `uvx basis-job new repo
nightly-loads`, or `uvx optim new repo sma income` (the repo and its first model). uv installs per user, with no admin
rights. Each developer's user-level `uv.toml` names the internal feed as the default index, not an extra one: under uv's
default first-index strategy, a name the feed holds is then never looked up on public PyPI, where an unrelated `optim`
exists. The feed proxies public PyPI for everything else.

Inside a repo, one command does the daily work: `basis`, from `basis-core`, to which each installed package adds its own
subcommands through entry points. `basis-core` provides `check`, `sync`, `replay`, `regress` and `docs`; `basis-job`
adds `run`, `resume` and `ddl`; `basis-api` adds `serve`, `call`, `dev` and `shot`. An agent learns one command whatever
parts a repo has. optim keeps its own `optim` command, and uses `basis.core`'s `sync` and storage underneath.

## An app repo

An app repo holds one app: an API, optionally its UI, and any number of jobs. Its parts are declared in its
`pyproject.toml`, never inferred from which folders exist, and any combination is valid except a UI without an API,
since a UI has nothing else to call. A repo of jobs alone is an app repo too.

| Path | Holds |
|---|---|
| `api/` | The API, on `basis-api` |
| `ui/` | The React app, on `@basis/ui` |
| `jobs/JOB/` | One folder per job, on `basis-job` |
| `openapi.json` | The API's OpenAPI document, committed. The API writes it and the UI's client is generated from it, and a contract test fails when either is stale |
| `fixtures/` | Synthetic data and its manifests, which the tests, the jobs and the UI's fixture mode read |
| `Dockerfile`, `azure-pipelines.yml` | One image for the repo, and a pipeline that calls the platform team's templates. `basis sync` keeps both identical across repos |
| `AGENTS.md`, `CLAUDE.md`, `.agents/skills/`, `.claude/skills/` | The agent files; see [Agents](#agents) |
| `.githooks/` | The commit hook; see [Hooks and gates](#hooks-and-gates) |

The image has two entry points under `tini`: `basis serve` runs the API, and `basis run JOB --publish` runs a job.
Whatever schedules a job runs the container with that command, so basis assumes no scheduler. Every file that `basis
sync` writes is the framework's, and a contract test fails when a repo has edited one, so the fix for a framework file
is a framework release.

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
event loop, because CPU-bound quant code inside an `async def` stalls every request on that worker. Errors are RFC 9457
problem details from one exception map, and an unexpected error is logged once, with its traceback.

Identity is optional. A request without a token runs anonymously; a request with one has it verified (signature, expiry,
issuer and audience), and an invalid or expired token is refused with a 401 rather than treated as anonymous, so a
broken sign-in in the UI fails loudly. A handler sees the user as `User | None`, so the type checker makes it handle the
anonymous case, and the user goes into every log record, trace and request record. Tests and `basis dev` sign tokens
with a development key that the settings refuse anywhere but a laptop.

The API's OpenAPI document is committed as `openapi.json`. The core sets each operation's id from its handler's name, so
the generated client's functions are named `getPositions` rather than FastAPI's default `read_positions_positions_get`,
and a strict model publishes `additionalProperties: false`, so the client's schemas are strict too. A route that returns
a dataset can also return it as Arrow when asked, which is how parallax receives its data.

`basis call ROUTE` runs a handler in-process with its real or fake resources and prints the response, so an agent
exercises an endpoint without starting a server. Every request in production is recorded in the run store: the request,
each resource call and its result, the writes, the response, and the code's and the framework's versions. `basis replay`
reruns one recorded request with today's code against the recorded results, touching nothing live, and `basis regress`
replays a sample of production records and lists the responses that changed. How long records are kept is out of scope
for this design.

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
becomes `basis run`), and its jobs live in app repos next to an API when one exists.

## The run store and fixtures

Every package writes its run data in one format. A run, a request or a decision is a manifest, a JSON file at
`runs/PACKAGE/NAME/RUN_ID.json`, that names the run's datasets by the hash of their content, and each dataset is one
Parquet file at `objects/HASH.parquet`. The morning's holdings, loaded by twenty optimizer runs, is then stored once.
There is one store per environment, a path or URL read through `pyarrow.fs` as optim's and etl-framework's snapshot
roots are, and DuckDB reads the manifests and the Parquet directly when someone wants to query across runs.

A manifest with a label, such as a regress set, keeps its objects. Deleting old runs is garbage collection, as in git:
delete the manifests a retention policy no longer keeps, then delete every object that no remaining manifest names.
Writing that policy is out of scope here.

Fixtures are small, synthetic and in git, as a CSV per dataset with a manifest, so they diff and an agent can read them.
Client data never goes in git. A test that needs real data names a run by id and runs only where the store is reachable,
and basis has no tool for scrubbing client data out of a snapshot, because a generic scrubber is where leaks come from.

`basis.core` hands each package its data as Arrow. `basis.job` works in polars, which keeps nullable integers, decimals
at their scale and zone-aware datetimes exact. optim works in pandas. `basis.api` works in pydantic models, and in
pyarrow for its Arrow responses.

## UI: @basis/ui

The UI is React 19.3 with the React Compiler 1.0, ported from the firm's Angular library. Its toolchain is Vite+,
VoidZero's MIT-licensed `vp` command, which bundles Vite 8 with Rolldown, Vitest 5, Oxlint, Oxfmt, tsdown and a task
runner, configured in one `vite.config.ts`. `vp check` runs formatting, linting and type checking together, and is the
check the agent runs most.

basis uses what Vite+ ships, at its strictest settings, and no lint plugins of its own:

- TypeScript 7, the native compiler, with `strict` and every further check: `exactOptionalPropertyTypes`,
  `noUncheckedIndexedAccess`, `noPropertyAccessFromIndexSignature`, `noImplicitOverride`, `noImplicitReturns`,
  `noFallthroughCasesInSwitch`, `noUnusedLocals`, `noUnusedParameters`, `allowUnreachableCode: false`,
  `allowUnusedLabels: false`, `verbatimModuleSyntax`, `isolatedModules`, `erasableSyntaxOnly` and
  `noUncheckedSideEffectImports`.
- Oxlint with type-aware linting and type checking on, its built-in typescript, react, jsx-a11y, unicorn, import,
  promise, vitest and oxc plugins (the spike settles the exact list), and its correctness, suspicious, pedantic, perf,
  style and restriction categories at error. Its experimental nursery category stays off. Where two rules conflict, one
  is turned off, with a one-line reason beside it. A warning fails the check.
- Oxfmt with its defaults.

Dropping lint plugins costs two guarantees. Components the React Compiler cannot optimize used to show up as lint
errors; the compiler's `panicThreshold: "all_errors"` makes them build errors instead. The design-system rules (no raw
colours, no arbitrary values) have no built-in replacement; a Tailwind theme that defines only the firm's colours
removes off-palette classes from the CSS, which fails silently, so the screenshot tests are what catch it.

The rest of the stack is TanStack Router, whose search parameters are typed and validated, for apps that keep their
filters and layouts in the URL; TanStack Query for server state; Tailwind 4 and Base UI, which shadcn/ui made its
default in July 2026; and Zod 4 at every boundary. The client is generated by orval from `openapi.json`: the types, a
strict Zod schema per model, a TanStack Query hook per operation, and MSW handlers for fixture mode. Generated types are
not validation, so every response is parsed by its schema before a component sees it.

The app shell owns startup, sign-in (it attaches the user's JWT to every request through orval's custom fetch), routing,
the generated client, telemetry and how errors are shown. Feature code reaches the backend only through the generated
client, which Oxlint's built-in `no-restricted-imports` and `no-restricted-globals` (for `fetch`) enforce.

Agents need to see what they build, so the UI runs without a backend. Fixture mode serves the generated MSW handlers,
and recorded API responses read from a reachable run store, which are never committed; `basis dev` starts the app on
fixtures on a free port per git worktree, and `basis shot ROUTE` writes a screenshot, the page's accessibility tree and
its console errors. Agents drive the browser with Playwright CLI, which Microsoft built for coding agents. Visual
baselines are made and compared only in one pinned Linux image, because Playwright's docs warn that rendering varies by
operating system; on a Citrix desktop an agent looks at screenshots but does not assert against baselines.

## Parallax: @basis/parallax

Parallax runs only in the browser. Its Rust engine, compiled to WebAssembly, runs in a Web Worker so that pivots never
block the page on a shared Citrix CPU, and it loads Arrow that the app fetches from its own API through the generated
client. Its server and connectors are deleted: the WebSocket server, the dataset registry and its hot reload, the expose
rules and the SQL registry, the SQL Server, Oracle, Mongo, Parquet and HTTP connectors, the NFS cache, ODBC and Oracle
Instant Client, and CI's three database containers. About 3,000 lines of Rust go, and about 15,000 stay: the engine, the
session layer, the protocol between the page and its worker, and the wasm wrapper. Excel export moves into the wasm
build.

The reason is the data's size. The firm's tables are not millions of rows, so a server that holds data the browser
cannot is cost without benefit, and every app would have needed one deployed beside it. A documented row limit, measured
on a Citrix desktop, replaces it: a larger load fails with a message saying so. React components replace the 44 Angular
files of its web layer; the canvas renderers, the formatter, the charts and the technical-analysis tools stay, as do the
DuckDB and Perspective comparisons that test the engine's numbers. App developers install one npm package with the wasm
built into it, and Rust stays inside this package's CI. Parallax keeps its Apache-2.0 license.

## Agents

| File | Read by |
|---|---|
| `AGENTS.md` at the root, about a hundred lines that map the repo and list its commands | Cursor, GitHub Copilot (VS Code and CLI), Codex and opencode |
| `CLAUDE.md`, containing only `@AGENTS.md` | Claude Code, which reads `AGENTS.md` itself only when no `CLAUDE.md` exists |
| `.agents/skills/` | Cursor, GitHub Copilot, Codex and opencode |
| `.claude/skills/`, a copy | Claude Code, which reads no other skills directory |

`basis sync` writes each installed package's block of `AGENTS.md` and its skills, prefixed with the package's name, and
a contract test fails when the copies differ from the installed versions. The skills are copies rather than symlinks
because Git for Windows checks a symlink out as a text file by default.

basis configures no MCP servers, because each agent keeps its MCP settings in its own file (`.vscode/mcp.json`,
`.cursor/mcp.json`, `opencode.json`, `.mcp.json`, `.codex/config.toml`) and no two share one. The agent's tools are
commands instead, which every agent runs with no setup: `basis`, `vp` and Playwright CLI. Every command prints one line
per failure, starting with `ERROR` and giving the reason, and never thousands of lines of progress, so a failure is one
grep away; Anthropic's C-compiler experiment found the same.

## Hooks and gates

Every repo has a commit hook that runs `basis check`, which runs every lint, type check and test suite the repo has.
Vite+ ships hooks of its own (`vp config`, `vp staged`), but they belong to the Vite+ toolchain, which needs Node 22 or
24, and a repo of jobs or an optimizer repo has neither. So `basis-core` owns the hook: `.githooks/pre-commit` is
committed, and any `basis` command sets the clone's `core.hooksPath` to it when it is unset, so a fresh clone has the
hook after its first `basis` command and `basis check` fails, with that fix, when the hook is missing. In a repo with a
UI, the hook runs `vp staged` first, which formats and lint-fixes the staged files and stages the result.

A commit that runs every test stays cheap only while the suites are fast. Loop time, from an edit to every check
passing, is measured by the eval below, and if the hook passes a budget of a minute, the full suites move to a pre-push
hook and the commit hook runs only the tests a change touches.

A hook can be skipped, with `git commit --no-verify`, and agents do skip it. So the hook is fast feedback, not the gate.
`AGENTS.md` says never to skip it, and the synced Claude Code settings deny `--no-verify`, but the gate is the pipeline:
it runs the same `basis check`, and an Azure DevOps branch policy on `main` requires that build to pass before any
merge.

## Evals

basis is judged by whether agents succeed in it, so it carries an eval. Twenty to fifty tasks drawn from real tickets
run in a generated app repo, on the Harbor setup from the maintainer's harness-evals plan, graded by basis's own checks:
the contract tests, `regress` and the end-to-end suite. Each task runs several times per variant, and the eval reports
the pass rate, how consistently a task passes across runs, the loop time and the tokens spent. It settles stack
questions with numbers rather than opinion, and it tests basis itself: whether a new error message, skill or doc page
helps agents or, as Cursor found of many of its own changes, makes them worse.

The method follows Anthropic's: tasks from real failures, deterministic graders where possible, the result graded rather
than the path, and repeated trials. A task no run ever passes is usually a broken task. Infrastructure alone moved
Anthropic's scores by six points, so a difference under three points is not a finding until the setups match.

## Versions

TypeScript 7.0.2 is the stable release and 7.1 is in beta. The 7.1 plan schedules its release candidate for 2026-11-10
and its stable release for 2026-11-24, but its beta shipped two weeks behind that plan. Work starts on the 7.1 beta,
since tsdown recommends 7.1, and releases pin 7.1 stable if it has shipped by then, or 7.0.2 if not. Python is 3.14, as
optim and etl-framework require. Node is the current LTS, managed by `vp env`.

## Decided against

| Option | Why not |
|---|---|
| C# services | One backend language keeps the stack small; engineering writes Python APIs too. |
| Rust services | Rust's SQL Server drivers are weak (the original `tiberius` crate lives on in forks, and sqlx dropped its SQL Server driver), its builds slow an agent's loop, and memory is not the cost that matters most. Rust stays where it earns its place, in parallax's engine. |
| Staying on Angular | The maintainer's call. The measured evidence is thin either way: in DesignBench, Claude 3.7's Angular compiled less often than its React when writing a page from scratch (0.69 against 0.95), and almost as often when editing one (0.91 against 1.00). |
| typescript-eslint | It does not support TypeScript 7, which ships no compiler API; its maintainers closed the request as not planned until TypeScript 7.1's API lands. |
| Oxlint JS plugins | Alpha. basis uses the rules Vite+ ships. |
| Prettier | Oxfmt ships with Vite+. |
| Bun | Vite+ already installs packages (`vp install`) and manages Node (`vp env`); `bun test` has no browser mode; Vitest under Bun has open worker failures. |
| MCP servers | Every agent reads MCP settings from a different file. |
| Parallax's server | The firm does not need million-row tables in the browser, and every app would have deployed one. |
| Shared datasets across packages | They matter to optim, so they stay optim's. |
| One version for every package | Packages are published and adopted separately; independent tags release only what changed. |
| Code shared between `basis.job`, `basis.api` and `optim` | Each depends on `basis.core` only, so one cannot break another. |
| Lint and format rules in prose | An agent eventually breaks a rule nobody checks. |
| Storybook visual tests, Argos, Lost Pixel | Storybook's visual tests send stories to Chromatic's cloud, Argos's maintainers do not support self-hosting, and Lost Pixel was archived in April 2026. Playwright and Vitest compare screenshots locally. |
| DVC or Git LFS for run data | Each adds a tool and storage credentials to every laptop and pipeline, and the packages already own their storage. |
| A tool that scrubs client data from snapshots | A generic scrubber is where leaks come from; fixtures are generated instead. |
| Copier or cookiecutter templates | Hundreds of copied templates drift; `basis sync` rewrites the framework's files from the installed version. |

## Facts behind the decisions

Each fact was checked on 2026-11-06 against the source named, unless it says secondhand.

| Fact | Source |
|---|---|
| typescript-eslint 8.72 peers on `typescript <6.1.0`; its maintainers closed TypeScript 7 support as not planned (issue #12518) while TypeScript 7.1's API is in progress (issue #10940) | npm registry; GitHub issues |
| TypeScript 7.0.2 is npm's `latest` (2026-07-08); 7.1.0-beta shipped 2026-10-20; the 7.1 plan dates the RC 2026-11-10 and stable 2026-11-24 | npm registry; microsoft/TypeScript#63703 |
| Oxlint's type-aware mode needs TypeScript 7 and covers 59 of typescript-eslint's 61 type-aware rules; its JS plugins are alpha and cannot use type information | oxc.rs docs |
| Vite+ is MIT-licensed and bundles Vite, Rolldown, Vitest, Oxlint, Oxfmt, tsdown and a task runner; it ships commit hooks through `vp config` and `vp staged`, which need Node | viteplus.dev; GitHub release v1.5.0 |
| Cloudflare announced on 2026-06-04 that it was acquiring VoidZero, which says its tools remain open source and MIT-licensed | voidzero.dev; the acquisition date is secondhand |
| React Compiler 1.0 was released 2025-10-07 | react.dev |
| orval 8.46 generates TanStack Query hooks, Zod schemas and MSW mocks, and depends on no `typescript` package; @hey-api/openapi-ts 0.99, which FastAPI's own full-stack template uses, lists `typescript` as a peer | npm registry; fastapi/full-stack-fastapi-template |
| Claude Code reads `.claude/skills/` only, and `AGENTS.md` only when no `CLAUDE.md` exists; Cursor, Copilot, Codex and opencode read `.agents/skills/` | each tool's docs |
| Bun has no browser mode in `bun test`; Anthropic acquired Bun on 2025-12-03 | bun.com; anthropic.com |
| A SQL Server snapshot-isolation defect fixed in etl-framework is likely still in portfolio-optim's `sources/mssql.py` | etl-framework's design page |
| On SWE-Sharp-Bench, agents resolved about 40% of C# tasks, against about 70% of Python tasks on SWE-bench Verified, on different task sets | arXiv 2511.02352 |
| OpenAI's harness-engineering team kept `AGENTS.md` to about 100 lines and enforced its architecture with linters whose messages carry the fix | a third-party copy of OpenAI's post, since openai.com refuses automated fetches |

## Not yet verified

Each item is a spike in [Phase 0 of the roadmap](roadmap.md#phase-0-spikes), and each needs a written yes or no before
the step that depends on it:

- that `vp` installs on a Citrix desktop without admin rights, and that uv reaches the Azure Artifacts feed from one;
- which of Oxlint's rules conflict at the strictest settings, and that type-aware Oxlint and tsdown work on TypeScript
  7.1 beta;
- that the React Compiler's `panicThreshold` still exists and fails the build;
- that orval emits Zod 4 strict objects, TanStack Query v5 hooks and MSW 3 handlers, and handles an Arrow response;
- that Oxlint has `no-restricted-imports` and `no-restricted-globals` built in;
- that Python namespace packages (`basis.core` across distributions) work with uv workspaces, ty and grimp;
- that rust_xlsxwriter builds for wasm, and how many rows parallax holds on a Citrix desktop;
- which agents the Harbor setup can drive.
