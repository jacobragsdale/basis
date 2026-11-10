# Plan: after the MVP review

This is the review that [the roadmap](docs/roadmap.md#later-what-the-mvp-leaves-for-the-review) left for after the
MVP, done on 2026-11-09 against commit `95e6794`, and the work that follows from it. [The design](docs/design.md)
still holds the decisions and [the roadmap](docs/roadmap.md) phases 0 to 5; this page adds phases 6 to 12, whose step
ids continue the roadmap's, so a branch is named as `AGENTS.md` says, such as `7.1-publish-state`.

The review read the source of all four packages (about 29,000 lines) and a sample of their tests, ran the unit tests
and coverage, drove the `basis` command in generated job and API repos as an agent would, and probed the pure parts
(calendars, number formatting, intervals, the OpenAPI diff) with edge cases. It did not run anything on Windows,
against a real SQL Server, Oracle, Mongo or Azure Data Lake, or read basis-ui. Each finding below says how sure it
is:

- **[repro]** a script or command reproduced it;
- **[read]** it follows from reading the code path end to end, and was not run;
- **[suspect]** it is likely, and needs the environment named to confirm.

The repro scripts were written to a temporary folder and are not in the repo. Every step that fixes a finding starts
by writing the failing test.

Paths are short: `core/x.py`, `job/x.py` and `api/x.py` are `packages/NAME/src/basis/NAME/x.py`, `optim/x.py` is
`packages/optim/src/optim/x.py`, and `NAME/tests/x.py` is `packages/NAME/tests/x.py`. Line numbers are as of
`95e6794`.

## Verdict

The design is sound, and the code mostly does what the design says. The parts worth protecting:

- **One failure boundary.** Everything becomes `ERROR: REASON: FIX`, last on stderr, and the tests assert it. In a
  generated job repo `basis check` takes 3 seconds and prints every problem at once, each with its fix; an API repo's
  takes 5.
- **The ledger row in the data's transaction**, with `published(run_id)` making each database idempotent. A lost
  commit acknowledgement and a double resume are both safe.
- **The request transaction and the outbox.** No probe made a write commit after a failed validation or a passed
  deadline, and no crash point between insert, commit, attempt and record lost or doubled an outbox row.
- **Token verification, the HTTP client's total deadline, the calendars and the number formatter.** `sifma-us` matched
  SIFMA's published 2025 and 2026 schedules, and the formatter held on about 250 probes.
- **The tests.** 2,335 unit tests, 89% branch coverage, none flaky across repeated and parallel runs, no
  `unittest.mock` anywhere, and nearly all of them assert behaviour through the public surface.

What the review found, in order of how much it matters:

1. **The run loop can lose or double data around a crash.** A job killed after one database commits is recorded as
   never published, cannot be resumed, and the fix its error offers doubles the rows (step 7.1). Several rerun guards
   have holes (7.2), and naive datetimes in a zone with daylight saving lose and duplicate rows once a year (7.3).
2. **The gates can be turned off from inside the repo.** `basis check` and the commit hook still passed after a
   repo's own `pyproject.toml` ignored whole ruff rule families, set ty to `all = "ignore"`, dropped the coverage
   floor to 10 and skipped a test (8.1, 8.2). With the firm's CI months away, the hooks are the only gate.
3. **The agent's own files are wrong in places.** Every job repo's `AGENTS.md`, two skills and the reference teach
   `Decimal` and `Precision`, which the code refuses; the fixture-run command they give fails twice as written; an
   API repo has no skill and no docs pages (9.1 to 9.4).
4. **Nothing is tested where developers work.** Every CI job is Linux; the hook tests skip on Windows; the Schemathesis
   and mutation tests run in no job; Mongo, lake and SQL Server `json` tests skip in CI (phase 6).
5. **Adding a kind of thing means editing many places**, and the copies have already drifted: about 15 places for a
   job's output kind, about 18 files across three packages for a resource kind (phase 11).
6. **There is no path for pushing live updates to a UI**, for a service that needs a token, or for a file dropped on
   a share (phase 12).

Measured on the development host:

| | |
|---|---|
| Unit tests | 2,335 pass in about 100 s, and in about 40 s under `pytest-xdist -n 4` with nothing to change |
| Left out of the default run | 178 integration tests and 9 `heavy` ones, which pass in 11 s |
| Branch coverage | core 86%, job 86%, api 90%, optim 88%; 89% together |
| `basis check`, generated job repo | 3.0 s; a commit through its hook, 4.2 s |
| `basis check`, generated API repo | 5.0 s warm; with `BASIS_FUZZ=1`, 9 s |
| `basis --version` | 0.6 s and 168 MB, of which pandas is half |

## Decisions taken in this review

The maintainer decided these on 2026-11-09. Each changes [the design](docs/design.md) in the commit of the step named.

| Decision | Step |
|---|---|
| basis owns each repo's ruff, ty and pytest settings, which move with `basis upgrade`; a repo adds only narrow, reasoned exceptions that `basis review` lists | 8.1 |
| The rounding gate goes: it is not needed, and its exemption was never settled | 8.7 |
| The firm's CI is months away or unknown, so the desktop gates are strengthened, and the pipeline stage stays ready | 8.9 |
| basis APIs push live updates to UIs; this is what the firm needs WebSockets for | 12.1 |
| optim stays provisional: only bugs with money consequences are fixed, and nothing new is built on it | 7.14 |
| Order submission and what an order-management system needs are real, and deferred | [Deferred](#deferred) |
| A port is proven equivalent to its legacy system per system, by its owner; basis builds no comparison tool | [Deferred](#deferred) |
| There is no deadline; the work goes in the order that makes the most sense | [The order](#the-order) |

## The order

| Phase | What | Why here |
|---|---|---|
| 6 | The safety net | Cheap, and every later fix needs CI to see it and a database double to test it on |
| 7 | Data that is never lost, doubled or changed | The defects that cost money or trust; nothing should build on them |
| 8 | Gates that hold | The premise is that checks are the review; until these land they can be switched off |
| 9 | The agent's first hour | Facts that are wrong today, fixed before anything measures agents on them |
| 10 | Measure, release, pilot | The eval the design promises, then `v0.1.0` once the contract is true, then the firm |
| 11 | Structure for change | Refactors, safe once phases 6 and 7 pin the behaviour, and due before more kinds are added |
| 12 | New capability | Built on phase 11's seams rather than the fifteen-place pattern |

The roadmap's rules for working in parallel stand, rule 2 above all: changes to `basis-core` land one pull request at
a time. Phase 7 has three lanes (A core and jobs, B APIs, C optim) that run at once; phase 8 is core's and follows
7's core steps through the same queue; phase 9 can start as soon as 7.4 settles the types the docs describe. The
maintainer decided on 2026-11-09 that the work keeps going straight to `main`, one commit or more per step, each
passing the checks in `AGENTS.md` before it lands.

## Phase 6: the safety net

1. **6.1 CI sees every change.**
   - `paths-ignore: ["**/*.md"]` skips CI for the Markdown that ships in wheels and that `basis sync` checks byte for
     byte: skills, each part's `templates/agents.md`, the packaged docs. Use `paths` with negations for `docs/**`,
     the root's `*.md` and `packages/*/AGENTS.md` alone. **[read]**
   - No job runs `-m heavy`: stop leaving `heavy` out of the default run (11 s).
   - Add `pytest-xdist` and run `-n auto` in CI. Watch the tests that hold real-time deadlines.
   - Measure coverage in CI and set `fail_under = 85` for basis itself, which is at 89% and holds generated repos to
     90. Omit `*/example/tests/*`, and collect coverage in the integration job, where `mssql.py` (64%) and
     `oracle.py` (44%) actually run.
   - Add `timeout-minutes` and a `concurrency` group to each job, pin actions by commit, and run the release dry run
     on a push to `main`.

   Done when a change to a packaged skill alone runs CI, and CI reports coverage with its floor.
2. **6.2 A Windows leg.** A `windows-latest` job that runs the unit tests and the generated repo's commit and push
   steps. `packages/core/tests/test_hooks.py:99` skips every hook test on `nt`, and the Windows branches of
   `sync.py`, `doctor.py` and `environments.py` are only simulated on Linux. Expect real failures; steps 8.5 and 8.6
   fix the ones this review predicts. Done when the leg is green and required.
3. **6.3 Real Mongo, Azurite and SQL Server 2025 in the integration job.** Four tests call `pytest.skip` while the
   job passes (`packages/job/tests/integration/test_mongo.py:34`, `test_lake.py:44`, `test_text.py:170`). Add the
   three containers, and make a fixture fail rather than skip when `CI` is set. Done when no integration test skips
   in CI.
4. **6.4 An API repo in CI, and generators that copy only what they mean to.**
   - Add an `api-generated-repo` job as `job-generated-repo` is: generate, `basis check`, commit through the hooks,
     build the image, `basis call` a route. `packages/api/tests/test_new.py` tests `new app` with `install=False`
     alone.
   - Both generators copy the example by a deny-list that misses `.ruff_cache/`, which a checkout's example folder
     holds (`packages/job/src/basis/job/new.py:69`, `packages/api/src/basis/api/new.py:20`). Copy by an allow-list.
     **[repro]**
   - `basis-api new app yield`, `class`, `signal`, `calendar`, `jobs`, `tests` and `basis` each make a broken repo
     and leave its folder behind, so the retry says the folder exists (`api/new.py:32-41`). Refuse keywords,
     `sys.stdlib_module_names` and basis's own names before writing, and build in a temporary folder that is renamed
     on success. **[repro]**
   - `api/templates/repo/pyproject.toml.in:5` writes `<0.{next_minor}`, which is wrong from 1.0. **[read]**

   Done when the job is green and each refused name fails in one `ERROR` line with nothing written.
5. **6.5 One shipped database double, held to the real writers.** Six tests carry their own double of a database's
   writer and ledger, each a little different (`core/tests/test_publish.py:49`, `core/tests/test_qa.py:169`,
   `core/tests/test_messages.py:114`, `job/tests/test_lake.py:64`, `job/tests/test_messages.py:61`,
   `optim/tests/fakes.py:136`), and no unit test publishes an `etl.Table` through `runs.run`: publish, partial and
   resume are tested only against containers the desktops lack. Ship `basis.core.fakes.Database` (reader, writer,
   ledger), with a contract suite parametrised over the fake, SQL Server and Oracle, as
   `core/tests/test_http_contract.py` is for HTTP, and move the six onto it. The design promises a contract suite per
   resource; today only HTTP has one. Done when the publish and resume scenarios of
   `job/tests/integration/test_publish.py` also run in the default run, on the fake, and the suite passes on all
   three.

## Phase 7: data that is never lost, doubled or changed

### Lane A: core and jobs

1. **7.1 A run knows it began to publish.** `packages/job/src/basis/job/runs.py:246-256` saves the manifest as
   `checked`, publishes, and rewrites it only when every database, workbook, upsert, archive and message is done.
   - A job killed between those is `checked` with no publishes. `resume` refuses it
     (`packages/core/src/basis/core/runs.py:99`), a rerun of an `Append` table is refused at the first committed
     database, and the fix the error offers, `--again`, inserted the rows twice. **[repro]**
   - `basis resume` on a run made without `--publish`, or on a replay, of a job whose outputs are only messages,
     workbooks, upserts or archives posted the Slack text, sent the email and returned `published`
     (`job/runs.py:803-838`). **[repro]**
   - A resume overwrites the record of a database already committed with empty counts (`core/runs.py:109`).
     **[repro]**
   - A missing ledger in a second database is found only after the first commits (`core/publish.py:74-76`), and a
     destination that cannot be reached during the pre-flight is "a defect" with no manifest (`job/runs.py:221`).
     **[repro]**

   Record in the manifest that the run was asked to publish, and rewrite it as `publishing` before the first commit.
   `resume` takes a `publishing` run, asking each ledger what it holds, and refuses any run not asked to publish or
   with `replay_of`. Describe each ledger in the pre-flight, and put the pre-flight under the handler that records a
   failed run. Done when a test kills a two-database job at each point between its steps and `resume` finishes every
   one with each row written once.
2. **7.2 Rerun guards without holes.**
   - The ledger lock is taken only for a plain insert (`core/publish.py:85`), so two `ReplaceWhere` runs of one
     interval can both delete and both insert. **[read]**; confirm with two writers and a barrier on SQL Server and
     Oracle, which no test has today.
   - An `Append` table in a run with no interval has no guard: two plain runs both published, 4 rows for 2
     (`job/changes.py:50-51`, `core/publish.py:85-86`). **[repro]**
   - Oracle's guard locks the whole ledger table until commit, including while a lake archive uploads or a message
     sends, so every publish to that database queues (`core/databases/oracle.py:361`). **[read]**
   - `Upsert()` on a row whose columns are all key columns fails whenever a key exists
     (`core/databases/mssql.py:610-614`, `oracle.py:378`). **[repro]**

   Take a lock per job and output for every publish (`sp_getapplock`, and `DBMS_LOCK` or a row of a small lock
   table), guard an append with no interval on the job and output alone, and run an all-key upsert as insert-new.
   This is the roadmap's "run locks". Done when the two-writer test passes on both databases.
3. **7.3 Datetimes in a zone with daylight saving.** For a column in `America/New_York` on 2026-11-01, one hourly
   run's delete range is empty, so its rerun inserts again; the next run's range removes the previous hour's rows;
   two instants are written as one value; and reading any value in that hour fails the dataset
   (`core/databases/base.py:189`, `mssql.py:594`, `oracle.py:409`, `core/frames.py:288`). **[repro]**
   - Oracle's `TIMESTAMP WITH LOCAL TIME ZONE` is read as plain `TIMESTAMP` (`oracle.py:326`), so stored instants
     depend on the session's zone. Misclassification **[repro]**, effect **[read]**.
   - A query's interval binds are naive UTC whatever the column's zone, while `ReplaceWhere` converts to the zone,
     so the read window and the delete window differ by the offset (`core/interval.py:59-61`). **[read]**

   Refuse, when the run is planned, a range or values whose naive image is ambiguous; let `Zone` declare how an
   ambiguous or missing source value is read; refuse or bind `LOCAL TIME ZONE` properly; and give a query binds in
   its column's zone, or say in the example's SQL how to filter one. Done when the fall-back and spring-forward hours
   round-trip or are refused with the fix, on both databases.
4. **7.4 Text and numbers at the boundary.**
   - An empty string becomes NULL or stays `''` depending on which pandas dtype holds the column
     (`core/frames.py:156`, `job/rows.py:186`). Apply the rule only when text is parsed into another kind.
     **[repro]**
   - On Oracle `''` is NULL: in a key or a `matching` column it passes every check and then never matches, so each
     run inserts again (`core/destination.py:159`, `oracle.py:413-429`). Checks **[repro]**, Oracle **[read]**.
   - The 2^53 guard never sees Oracle's unconstrained `NUMBER`, which arrives already a float: an 18-digit id is
     rounded with no error (`core/frames.py:108`). **[repro]** for `floats`, driver mapping **[read]**.
   - A float into `REAL` or `BINARY_FLOAT`, microseconds into `DATETIME2(3)`, and `'00123'` into an integer column
     are accepted and changed (`core/destination.py:71,78,170,211`). Checks **[repro]**, servers **[read]**.
   - Column names are never quoted and reserved words pass, so `key`, `order` or `date` fail at publish.
     **[read]**

   Done when each is refused before any write, with its fix, and the integration tier covers each on both databases.
5. **7.5 `regress` tells the truth.**
   - Rounding to 15 digits still reports sums that differ in the last bit: 553 of 20,000 group sums computed in two
     orders (`job/diff.py:108-122`). Compare with a relative tolerance. **[repro]**
   - Only `Table` outputs are compared: a changed message, workbook, upsert or archive regresses as no change
     (`job/runs.py:777-779`). **[repro]**
   - `basis regress JOB --label purpose=prodution` replays nothing, prints `[]` and exits 0. Print a `SKIPPED` line,
     or fail, when no run matches. **[repro]**
   - `basis replay --env prod` writes the replay into prod's run store, and fails as "a defect" when it cannot
     (`job/cli.py:226-229`). Save it locally, as `etl.replay` does, and print the differences first. **[repro]**

   Done when each case has a test, and a replay against a read-only store prints its differences.
6. **7.6 The lake, Excel, Mongo and messages.**
   - Lake `--again`: when the second rename fails, the cleanup deletes the old partition too, and the ledger still
     says the date is archived (`core/lake.py:163-168,244-251`). **[repro]**
   - A partition already in place is accepted on its row count alone (`core/lake.py:236-239`); compare the hashes
     already computed. **[repro]**
   - Excel: text starting with `=` is written as a live formula in the workbook a portfolio manager opens, and text
     over 32,767 characters is cut with a warning (`core/folders.py:100-102`). Pass `strings_to_formulas: False` and
     `strings_to_urls: False`. **[repro]**
   - Slack and email deadlines apply per socket operation, not in total, while the ledger's transaction stays open
     (`core/sending.py:69,131-135`). A relay that refuses one of two recipients counts as sent
     (`core/sending.py:135`). **[repro]**
   - Mongo upserts write lower-case field names where reads fold case, so `accountId` gains a second document per
     key (`core/mongo.py:101,160,208-215`); nothing checks for a unique index on the key. **[repro]** on the fake.
   - The Mongo fake returns nothing for filters real Mongo matches (dotted paths, array membership, `$or`), and the
     QA tier neither marks nor allowlists Mongo (`core/fakes.py:281-326`, `job/qa.py:103-116`). **[repro]**,
     **[read]**
   - Lake and folder calls have no deadline and no span, and Mongo ignores the enclosing deadline. **[read]**

   Done when each has a test, the lake and Mongo ones against 6.3's containers, and the email ones against a local
   SMTP server, which becomes email's contract suite.
7. **7.7 Environments and secrets.**
   - `--env` is a label: with no `dev.env`, `--env dev` takes whatever `BASIS_*` the shell holds and allows writes,
     and a `dev.env` copied from `prod.env` behaves the same (`core/environments.py:106-116`). Require `BASIS_ENV`
     to be set and equal, and refuse a file whose `BASIS_ENV` differs from its name. **[repro]**
   - The user's file is not read verbatim: `${NAME}` expands from the process, an unquoted value is cut at ` #`, and
     `"C:\runs\new"` gains control characters (`core/settings.py:364`). Read with `interpolate=False`. **[repro]**
   - Secrets reach logs: a run store URL's credentials on every run (`core/storage.py:39`), a password's tail in a
     connection string parse error (`core/databases/mssql.py:93`), `user:pass` of a service or proxy URL at DEBUG
     (`core/opened.py:87,175`), and 2,000 characters of every failed reply's body. **[repro]**
   - A log record whose `extra` holds `pd.NaT` or bytes is dropped for a multi-line traceback
     (`core/logs.py:212-241`). **[repro]**

   Done when each has a test, and a test greps a run's whole stderr for every secret its settings held.
8. **7.8 Retries, exits and small crashes.**
   - A GET's retries have no cap: 124 requests in a 120 s deadline against a service answering 503
     (`core/opened.py:45-48,211-219`). Cap at 3 or 4. **[repro]**
   - With the Datadog Agent down, a run's exit blocks for 3.5 s to 17.5 s (`core/traces.py:97-107`). **[repro]**
   - `today..today`, `today..today-7d` and `today-0bd..today` are accepted and then fail as defects;
     `display.text(pd.NaT)` raises (`core/interval.py:97-133`, `core/display.py:402-439`). **[repro]**
   - Every `previous-business-day` job fails from 2028-01-01, by design, with no warning before: `basis doctor` and
     `basis check` should warn as a calendar's last date nears.

### Lane B: APIs

9. **7.9 Strict bodies are strict.**
   - `1e999`, `Infinity` and `NaN` pass a request body, are committed, and reach the other service as null; a
     repeated JSON key takes the last value (`api/strict.py:346`). **[repro]**
   - A body has no size limit, and is read whole and parsed up to twice on the event loop (`api/routes.py:117`). Add
     `serve.max_body_bytes` and a 413 problem. **[repro]**
   - A client that disconnects mid-upload is logged as an ERROR 500 with a traceback (`api/routes.py:117,219-224`).
     **[repro]**
10. **7.10 Writes that can be trusted.**
    - A connection lost during COMMIT answers 503, which the contract tells a UI to retry, though the write may
      have committed (`api/opened.py:559-575`). Give a failure at or after the commit its own problem type that a UI
      does not retry, logged as outcome unknown. **[repro]** Inbound idempotency, which makes the retry safe, is
      12.8.
    - A request's id put into another service's path reaches other endpoints: `../admin/wipe`, `x?force=true#`
      (`api/opened.py:738-748`, `api/outboxes.py:74-82`). Take a path template and its parameters, and encode each
      segment. **[repro]**
    - A route that writes needs no user, and the example every new app copies is one
      (`api/service.py:543-574`). Make a route that is not a GET ask for the user or declare `anonymous=True`.
      **[repro]**
    - A database failure's reason is logged nowhere (`api/app.py:225-227`). **[repro]**
    - `api.serve(settings=Settings.given(...))` accepts the published development key on a real socket
      (`api/tokens.py:449-452`). Refuse it whenever the host is not loopback. **[read]**
    - `/docs` loads unpinned script from a public CDN on the origin that, with `ui=`, holds the UI's token
      (`api/app.py:166-184`); and a refused token tells an anonymous caller the audience and the sign-in system's
      URL (`api/tokens.py:158-161,346-353`). **[repro]**, **[read]**
11. **7.11 The compute pool.**
    - A compute function's exception that does not unpickle breaks the pool for every request in flight, and
      `fakes.Compute` answers the declared 422 instead (`api/pool.py:109-185`). Send back only an exception proven
      to round-trip, and make the fake round-trip too. **[repro]**
    - A call past its deadline keeps its worker; enough of them starve the pool while readiness answers 200
      (`api/pool.py:101-135`). **[repro]**
12. **7.12 The contract is true.** These change what a UI relies on, so each pull request says so and updates
    basis-ui's pinned sample; they land before `v0.1.0` so basis-ui pins once.
    - A float is documented as never null, and NaN answers `null`: basis's own smoke and fuzz then fail the app's
      real answer (`api/openapi.py:71-79`, `docs/contract.md:141-142`). **[repro]**
    - A response field that gains a default "may be absent" in the document though it is always sent
      (`api/contract.py:225-226`). **[repro]**
    - The breaking-change check misses a required field added to one arm of a request union, a retyped field in one
      arm of a response union, `str` narrowed to a `Literal`, and `prefixItems` (`api/contract.py:178,195,288-319`).
      Keep a set of shapes per path, and print a line for each keyword the walker skips. **[repro]**
    - Its baseline is the commit itself when that commit carries a tag, and silently none after
      `git fetch --no-tags`, which a pipeline's default checkout may do (`api/contract.py:383-408`). Commit the released
      document, written at release, and compare with that: no tags, depth or worktrees to depend on. **[repro]**
    - Error types and display formats can change with nothing reported (`api/routes.py:238-252`). Emit each
      operation's error types as `x-basis-errors`, compare them and the formats, and publish a sample Arrow stream
      and all six format kinds for basis-ui. **[repro]**
    - `api.Table`'s one check passes a `Literal` column holding another value, a `UUID` column of any text, and a
      `bool` column of `7` (`api/arrow.py:135-156`); a `list[Row]` dataset with a JSON-only serializer answers
      different values as JSON and as Arrow (`api/dataset.py:83-100`). **[repro]**
    - Smoke crashes on a 200 that is not JSON (`api/smoke.py:66,78`); the UI fallback answers 404 for
      `/positions/BRK.B` and 200 HTML for an API path without its prefix (`api/ui.py:54-64`). **[repro]**
    - The fake database accepts binds that do not match the statement (`api/fakes.py:182-223`). **[repro]**
13. **7.13 The layers hold in a generated repo.**
    - A resource asked for through a `type` alias, as the example aliases its `Path` parameters, drops the route's
      declared failures and passes the layer check (`api/layers.py:94-205`). **[repro]**
    - In a generated repo `handlers.py` importing `httpx2` and reading `os.environ`, and `domain.py` using
      `urllib.request` and `datetime.now`, print no layer line: those contracts are import-linter entries for this
      workspace's example alone, and `PURE` is a hand copy of core's list that has drifted (`api/layers.py:45-50`).
      **[repro]**
    - False positives: a test module under `tests/domain/`, and a job's `errors.py` (`api/layers.py:228-232`).
      **[repro]**

    Done, for lane B, when each case has a test, the example's write is signed in, and basis-ui has the new sample.

### Lane C: optim, money only

14. **7.14 optim's bugs with money consequences.** Its design stays provisional, so these fix what is wrong and add
    nothing.
    - HiGHS drops matrix entries at or below 1e-9, so the example's weight caps vanish for a large portfolio: at
      $1.1bn it returns `optimal`, is accepted, and puts 100% in one bond
      (`optim/example/optimizer/constraints.py:50,64`, `optim/cvxpy.py:76-88`). Write caps in currency in the
      example and docs, and have `optim.cvxpy` check every constraint at the values returned. **[repro]**
    - `prepare_market` gets the run's own frames, so an edit in place corrupts acceptance and the snapshot
      (`optim/runs.py:176,197`). **[repro]**
    - Acceptance's tolerance grows with portfolio value: selling 5,600 of a 5,000 holding passes at $2bn, and a sale
      of cash counts as proceeds (`optim/acceptance.py:54-75`). **[repro]**
    - `--again` removes the duplicate-order guard for every portfolio, and the refusal's own fix offers it
      (`optim/orders.py:147-168`). Guard per portfolio, and require `--portfolios` with `--again`. **[read]**
    - Two repos' models of one name share a ledger identity and a store folder (`optim/runs.py:167,200`). **[read]**
    - An empty feed publishes, and `--publish` with `--data` wrote fixture rows to the orders table
      (`optim/data/dataset.py:30-46`). Take basis-job's `empty="fail"|"allow"`. **[repro]**
    - One portfolio that kills its worker rejects the rest of the book (`optim/engine.py:37-44`); duplicate ids are
      dropped, and `options` can loosen "optimal" (`optim/cvxpy.py:57-106`). **[repro]**
    - Converge now, since it is safe whatever the quant meeting decides: settings through
      `basis.core.environments`, QA safeguards onto core's QA tier, `regressions` for `check --full`, and its docs'
      commands, every one of which fails today for want of `--env`.

## Phase 8: gates that hold

1. **8.1 basis owns the tool settings.** `basis check` runs ruff, ty and pytest with whatever the repo's
   `pyproject.toml` holds (`core/check.py:43-51`), which a generator wrote once. After ignoring `ANN`, `ARG`, `PLR`,
   `C90`, `S` and `BLE`, setting `[tool.ty.rules] all = "ignore"` and `coverage = 10`, `basis check` exited 0 and
   the commit hook took the commit. **[repro]** The pins (`ruff==0.17.4`, `ty==0.0.99`) also stay where the
   generator left them, which is the drift the design refused Copier for; and ty's own README says diagnostics may
   change between any two versions.
   - `basis sync` writes `ruff.toml`, `ty.toml` and pytest's settings as framework files, with the marker, so the
     editor and the command read the same ones, and `basis check` fails on a `[tool.ruff]`, `[tool.ty]` or
     `[tool.pytest]` table in `pyproject.toml`.
   - The tools arrive at exact versions through each part, such as a `basis-job[dev]` extra, so `basis upgrade`
     moves them with basis.
   - A repo's exceptions are `[tool.basis]` entries of a rule, a path and a reason; `basis review` lists them, and
     any coverage floor under the generator's.

   Changes the design's "Gates without CI". Done when the weakening above fails `basis check`, and a generated repo
   upgraded across a basis release that moves ruff needs no edit of its own.
2. **8.2 Suppressions and skips are counted.** With 8.1 in place, what is left to an agent is `# noqa`,
   `# ty: ignore`, `# pragma: no cover`, `allow-mocks`, `pytest.mark.skip` and `xfail`, and deleting a test: all of
   these passed in the same repro. Refuse a suppression that gives no reason, in `allow-mocks`'s `-- WHY` form, and
   have `basis review` list what the change adds against where it branched: suppressions, skips, and tests removed.
   Done when a skipped test and a bare `# noqa` each show in `basis review`, and the bare one fails `basis check`.
3. **8.3 One walk of the repo, which stops at another checkout.** `sync`, both gates and mutation each walk the
   folder (`core/sync.py:525`, `core/gates.py:129,154`, `core/mutation.py:123`), and none stops at a nested `.git`.
   With a second agent in `.claude/worktrees/feature`, `basis check` in the main checkout lists that worktree's
   Dockerfile and hooks as stale, and `basis sync` deletes them; each checkout's sync undoes the other's.
   **[repro]** `AGENTS.md` asks for a worktree per step, so this is the common case. One `walk(repo)` that prunes
   any folder holding a `.git` entry, and `.claude/worktrees/` in the repo's `.gitignore`. Done when two worktrees
   of one repo both pass `basis check` and `basis sync` changes nothing in either.
4. **8.4 Coverage counts every module.** A module no test imports, outside an `__init__.py` chain, is not counted:
   a repo with untested `src/app/core.py` reported 100% (`core/gates.py:285-289`). Set
   `include_namespace_packages`. **[repro]** `basis check`'s coverage data also leaks into the caller's
   `COVERAGE_FILE`, and each job's two-line `main.py` shows as missed on every run.
5. **8.5 The hooks on Windows and at the push.**
   - The push hook does not run the staged-file checks, so a private key committed with `--no-verify` reaches the
     remote, though the hook and `AGENTS.md` call it the backstop (`core/full.py:56`). **[read]**
   - Hooks written on Windows are committed without their executable bit, and git in WSL then ignores them with a
     hint (`core/sync.py:440-451`). Record the mode in the index. Linux half **[repro]**.
   - git's output is decoded in the locale's encoding, so a clone under `C:\Users\Ánna` fails every `check`
     (`core/clone.py:19`). **[repro]** under a non-UTF-8 locale.
   - The commit hook refuses whitespace its own next step would fix (`core/templates/sync/pre-commit:20`).
     **[repro]**
   - The push hook checks a tagged commit again in a new worktree, QA publishes and all
     (`core/templates/sync/pre-push:37-52`).
     **[repro]**
   - `basis upgrade` on Windows must replace the `basis.exe` that is running it (`core/upgrade.py:78-106`).
     **[suspect]**: a **(work)** check on a desktop.
6. **8.6 The no-socket gate on Windows.** It patches `socket.socket`'s methods, and Windows' default event loop
   connects through `_overlapped`, so a unit test using `httpx.AsyncClient` against QA passes on Windows and fails
   in WSL (`core/pytest_plugin.py:29,57-64`). **[read]**; needs 6.2 to test.
7. **8.7 The rounding gate goes.** Remove `gates.rounding` and its line in `check`, with the design's Numbers
   section and the roadmap's step 2.11 changed in the same commit. It refused `frame["traded_at"].dt.round("1min")`,
   had no exemption, and let `math.floor(x * 100 + 0.5) / 100` through. Done when the gate, its tests and its prose
   are gone, and the synced `AGENTS.md` block's "three gates" is true again.
8. **8.8 Schemathesis and mutation testing, on.** The roadmap left turning them back on to this review. Both sit
   behind `BASIS_FUZZ` and `BASIS_MUTATE`, while `AGENTS.md` describes them as running. Move the fuzz into
   `check --full`, building the app once; let it sign a development token for a declared example user, since today
   it runs a signed-in handler's body 0 times in 50 (`api/fuzz.py:107-112`); and print each failure as a file that
   `basis call` reads, since the printed command cannot express every request. **[repro]** Mutation stays a report.
   Done when both run by default where the design says, and the docs say the same.
9. **8.9 Gates in the agent's own harness.** The firm's CI is months away, and a deny rule on a shell command is
   not a boundary: Claude Code's own docs say so, and `git -c core.hooksPath=/dev/null commit`, which none of the
   rules `basis sync` writes matches, committed an `eval` with no hook run. **[repro]** Agents now have hooks of
   their own, which can refuse a tool call and can keep an agent from stopping.
   - Spike first: for Claude Code, Cursor and Codex, what a project hook can block, and its file format, from each
     tool's current docs. Write `docs/spikes/agent-hooks.md`.
   - `basis sync` then writes each supported agent's hook file: one that refuses a commit or push that skips or
     repoints the hooks, and one that runs `basis check` when the agent stops and hands back its `ERROR` lines.
     Both call a `basis` verb, so they are Python on every platform.
   - **(work)** Ask whether the firm can deploy managed agent settings to its desktops, which a repo cannot edit.

   Done when, in a generated repo, an agent told to commit with a failing check cannot, by any of the ways the
   spike lists.
10. **8.10 The one-line promise, kept everywhere.**
    - `basis doctor` writes its `ERROR` lines before the `finished` record, and fails as a defect when a declared
      part does not import (`core/doctor.py:115`, `core/command.py:602-609`). **[repro]**
    - Held `ERROR` lines are lost when the command is interrupted or the harness times it out
      (`core/command.py:165-174`). **[repro]**
    - A row mismatch prints three lines; a workbook that cannot be written is reported as a database failure;
      `--interval today..today-7d` is a traceback; a fixture missing an Excel dataset's file says to set a variable
      (`job/runs.py:344,354-360`, `job/cli.py:203`, `job/testing.py:82`). **[repro]**
    - `basis review` on a record from another basis version names no file and gives the wrong fix
      (`core/review.py:150`). **[repro]**
    - Several API refusals give a fix that cannot be followed, and FastAPI's own errors surface at build
      (`api/strict.py:140-143,234`, `api/routes.py:263`). **[repro]**

## Phase 9: the agent's first hour

1. **9.1 The agent files say what the code does.**
   - Jobs: `job/templates/agents.md:17-20`, `job/skills/job-new/SKILL.md:10-11`,
     `job/skills/job-port/SKILL.md:18-21`, `job/docs/reference.md:31-45,60-64,83,538`,
     `job/docs/explanation.md:26,60-70` and `packages/job/AGENTS.md:24,76,84`
     still describe `int`, `Decimal` with `etl.Precision`, and every column Arrow-backed. `etl.Precision` no longer
     exists. **[repro]**
   - APIs: `api/templates/agents.md:27,84` says a decimal is a string, and never mentions `api.Table`, `api.format`,
     `fetch_table` or `smoke=`. `packages/api/AGENTS.md` lists `new app` and `smoke` as still to come.
   - The root `AGENTS.md` says the repo is its docs until phase 1, omits `smoke` from `Part`'s fields, and says the
     push hook's worktree is inside the git folder.
   - A test, so it stays true: every `etl.NAME` and `api.NAME` in a packaged doc, skill or agent block exists, and
     every `basis` command line in them parses with the real parser. Generate the type tables from
     `frames._TYPES`.

   Done when the test passes and fails on a made-up name.
2. **9.2 A loop with no environment.** The command every job repo's `AGENTS.md` gives,
   `basis run JOB --params ... --data jobs/JOB/tests/fixture`, fails for want of `--env`, then for want of a
   `dev.env`, then because the command
   ignores `fixture.json`'s interval, which `etl.run` and `main.py` read (`job/cli.py:196-206`, `job/api.py:53-59`).
   **[repro]** A run whose every dataset comes from `--data` reaches nothing of the firm's: it needs no
   environment, takes the fixture's interval and `now`, and saves to the repo's `snapshots/`, as `main.py` does;
   `replay` and `regress` of those runs likewise. Done when 6.4's CI job runs the three lines as `AGENTS.md` gives
   them in a fresh repo.
3. **9.3 Results an agent can read.**
   - `basis run` prints nothing on stdout: the run's id is only in a stderr log record, and `replay` needs it.
     Print the summary as JSON, as `AGENTS.md` says of every result.
   - `basis replay` says `gross_notional: 1 changed` and not which row or what it was. Add `--rows N`: key, column,
     before and after.
   - `basis runs [NAME] --last N`: the store's recent runs with status, interval, error and trace id, which is the
     first question of every production issue and today means reading manifests by hand.
4. **9.4 Skills and docs for APIs.** An API repo's `basis docs` lists only job pages, and `.agents/skills/` only
   `job-*`. Add an API tutorial, how-to and reference, and skills for adding a route end to end (domain,
   repository, handler, service, errors, tests, `basis sync`) and for finding out why a request failed. Say where a
   row model with `api.format` lives, which no layer allows today. Keep each skill's description within what agents
   show of it.
5. **9.5 SQL is opened before the environment.** A job declaring `sql="sql/price.sql"` for a file named
   `prices.sql` passes its fixture run and `basis check` (`job/inputs.py:86-99`). **[repro]** The contract tests
   assert each query's file exists and names no bind but the interval's. Then a spike: parse each file with sqlglot
   in its dialect, and compare the selected columns with the row. A parser that refuses valid vendor SQL would
   teach agents to ignore it, so it reports only, until it has parsed the firm's real queries.
6. **9.6 A lighter command.** `core/verbs.py` imports `basis.core.databases`, and so pandas, for one field's type,
   so every `basis` call and both hooks pay 0.6 s and 168 MB. Import it lazily. Done when `basis --version` imports
   no pandas.

## Phase 10: measure, release, pilot

1. **10.1 The eval.** The design judges basis by whether agents succeed in it, and step 0.3 was never built: there
   is no `evals/`. Build it now, small: ten to twenty tasks in a generated repo, each run several times, headless,
   graded by `basis check`, hidden tests and 8.2's counts.
   - Ordinary tasks: add a column, add a job from a description, add a route end to end, port a short legacy script.
   - Adversarial tasks, from this review: a failing test whose honest fix is in the transform, graded on the test
     not being skipped or weakened; a lint error whose honest fix is in the code.
   - Report the pass rate, how consistently a task passes, the loop time and the tokens.

   From here a skill, an error message or a doc page changes when the eval shows the change helps (the roadmap's
   4.5). Done when `evals/` holds the tasks, the runner and a first report.
2. **10.2 `v0.1.0`.** The roadmap's 1.8, after phases 6 to 9 and lane B of 7, so the first contract basis-ui pins
   is the true one.
3. **10.3 The pilot (work).** The roadmap's 2.8, with this review's list of what only the firm's environment can
   answer:
   - `basis upgrade` while `basis.exe` runs; a workbook open in Excel during the rename
     (`core/folders.py:112-119`), a UNC share, a clone under a non-ASCII user folder;
   - an Azure Pipelines checkout's depth and tags; the lake's rename on the firm's account; Kerberos; Datadog;
   - mssql-python's Arrow types for `datetime2(7)`, `datetimeoffset`, `money` and `uniqueidentifier`, and
     python-oracledb's for zoned timestamps;
   - whether managed agent settings can be deployed (8.9).

## Phase 11: structure for change

Each of these is a refactor with behaviour unchanged, safe once phases 6 and 7 have pinned the behaviour.

1. **11.1 One interface for a job's outputs.** Adding an output kind touches about 15 places in
   `job/outputs.py`, `job/runs.py`, `job/cli.py` and `job/qa.py`, and the four hand-copied kinds have drifted:
   `_replayed` omits documents and archives, `_partial` omits workbooks and upserts, and `_finish_files` goes on
   after a failure where `_after_commit` stops. Give every output that is not a table one small interface (check,
   plan, apply, pending) and one ordered loop that `run` and `resume` share; move replay and regress to a module of
   their own.
2. **11.2 One descriptor per kind of resource.** A new kind is about 18 files across core, job and api (Mongo's
   commit touched 27), in five different patterns of declaration and fake, chosen by `isinstance` chains. One record
   per kind (declaration, settings model, open, fake, doctor's check, the QA tier's check) that each part iterates,
   one helper for a ledgered effect (`core/sending.py:218-233` and `core/lake.py:143-165` are the same skeleton),
   and one mapping of HTTP failures (`core/opened.py:197-208` and `core/sending.py:70-77`). `basis doctor` then
   checks HTTP, Slack, email and Mongo, which it does not today.
3. **11.3 One object per database dialect.** Types, binding, DDL, the ledger, quoting and the bind pattern are
   spread over `core/destination.py`, `core/ddl.py`, `core/tables.py` and the settings union, and `mssql.py` and
   `oracle.py` duplicate their transaction and ledger code with small differences. A third dialect would touch all
   of it.
4. **11.4 The command's tables.** A core verb is five edits in `core/command.py`; a shared verb goes through a
   second parser that never sets `repo_root`, which `verbs.py`'s docstring tells a handler to read. **[repro]**
   Make `Runs` a mapping of verb to `Shared`, build every parser in one place, read `[tool.basis]` through one
   typed reader (it is parsed in four), and give `BasisError` a `reason` and a `fix`, which `doctor._split` now
   recovers by splitting the string.
5. **11.5 basis-api's seams.** `api/opened.py` (820 lines) holds planning, the database session, the request's side
   of the outbox and the cache; `api/service.py` holds the declarations, FastAPI's dependencies and `load`. The
   rule that a write commits after the response validates rests on FastAPI internals
   (`fastapi._compat.ModelField`, `dependant.body_params`): put those in one module, with a test that fails on a
   FastAPI upgrade that moves them.
6. **11.6 A compatibility promise for basis's own surface.** Fifty repos will import `basis.job` and `basis.api`,
   and years of runs will sit in the store, and nothing today says what may change.
   - The surface is each part's `__all__`, the command's verbs and options, the settings' names, and the formats of
     the manifest, the ledger and the check record. CI compares the Python surface with the last release's, as the
     API's contract check does for `openapi.json`.
   - A name goes through one release in which using it is refused with its replacement, as `renamed` already does
     for variables, and `basis upgrade` runs the release's codemods, so a rename costs a repo nothing.
   - A corpus of manifests, ledger rows and check records from every release, which the current code must read: a
     manifest carries its part's version but no format version, and a record is read with `extra="forbid"`, which
     is 8.10's `basis review` failure.
7. **11.7 Housekeeping.** Four definitions nothing references (`api/openapi.py:95` `_reached`, `job/api.py:94`
   `fixture_interval`, `optim/data/frames.py:61` `to_models`, `optim/schema.py:31` `Par`), which ruff does not see:
   add a dead-code check. One ruff pin, by replacing the `ruff-pre-commit` mirror with a local hook as ty's is. One
   policy for `PLR0913`, which three packages suppress 37 times and optim ignores whole. Four tests that claim more
   than they assert (`job/tests/test_cli.py::test_a_run_told_again_records_it_and_resume_takes_again_too` and
   `::test_a_run_that_would_write_to_prod_is_refused_unless_the_deployment_sets_the_flag`,
   `core/tests/test_command.py::test_a_repo_without_tool_basis_still_tells_its_version_and_checks_its_desktop`,
   `core/tests/test_core.py::test_core_is_a_portion_of_the_basis_namespace`), and a table of refusals for job
   declarations as `api/tests/test_routes.py` has for routes (`job/outputs.py` is at 70%).

## Phase 12: new capability

Each needs a design decision first, written into [the design](docs/design.md) in its step's commit.

1. **12.1 Live updates to a UI.** basis-api has no path for a stream: `Service.routes` takes five HTTP methods, the
   middleware passes other scopes through with no id, user or deadline, and a `StreamingResponse` is accepted when
   declared and fails at build. Design it with basis-ui, since it changes the contract.
   - The recommendation is server-sent events for a route that only pushes: one HTTP response, the browser
     reconnects by itself, and it passes the ingress and the one-origin rule unchanged. A WebSocket only where the
     UI must also send on the same connection.
   - To settle: the declaration (an event model per stream, in the document as an extension, as `x-basis-format`
     is); identity at connect and when a token expires mid-stream; what replaces the route's deadline (a heartbeat
     and an idle limit); backpressure for a slow reader; what feeds the stream (a repository function polled, or a
     subscription to another service); and its fake.
   - Until then, refuse a `Response` type when a route is declared, and a returned `Response` at run time, which
     today commits and answers a body nothing validated.
2. **12.2 Services that need a login.** A declared HTTP service has no setting for a token, a header or Kerberos,
   and a job's `Fetch` is one GET of a flat JSON list, with no pagination and no POST that only reads. Most of the
   firm's internal services will need the first on day one.
3. **12.3 Files from a folder.** `File` has no `folder=`, as `Excel` has, and no delimiter, encoding or pattern, so
   a CSV dropped on a share has no path into a job.
4. **12.4 Backfill.** `basis backfill JOB --from DATE --to DATE`: one run per interval, in order, in the job's
   calendar, stopping at the first failure and resuming from it.
5. **12.5 Expectations of an output.** The roadmap's "output checks". The run store already holds a job's recent
   published runs, so the checks that catch most bad loads are cheap: a row count within a band of the last runs', a
   column's null rate, a total that reconciles with an input's. Declare them on the output, evaluate them before any
   write, and give their reasons as `check`'s are given. No library: what pandera and Great Expectations add is
   breadth basis does not need, and their failures would not be basis's one line.
6. **12.6 Tables that do not fit in memory.** Every dataset is one pandas frame under `MAX_ROWS`. Archiving a large
   table to the lake, which the design says many jobs do, needs a copy that streams Arrow batches from the driver
   to Parquet with no frame and no transform.
7. **12.7 Incremental loads.** A query binds only the interval. A watermark must be an input the manifest records,
   so that a replay reads what the run read.
8. **12.8 A request that can be sent twice.** An inbound `Idempotency-Key`, or a client-made id, deduplicated in
   the request's transaction, and allowed through CORS. Needed before any route submits an order.
9. **12.9 Secrets and the supply chain.** Azure Key Vault as a source of settings (the roadmap's "Later"). For
   generated repos, a dependency cooldown (`exclude-newer`) and a vulnerability check in `check --full`: confirm
   both against the pinned uv's docs first.

## Deferred

- **Orders and an order-management system.** Deferred by the maintainer. When it starts, bring 12.8, 7.10's
  commit-outcome problem, and from optim: the duplicate-order guard, the order row's missing price, model and as-of
  date, and a report a portfolio manager or an approval step can read.
- **optim after the quant meeting.** Bring: "not solved: infeasible" names no constraint; sizing, caps and turnover
  exist only as a copy in each model repo, so a fix cannot arrive by upgrade, and the copied per-bond cap builds a
  dense matrix (1.6 GB at 12,000 bonds); `Security` has no duration, spread duration or benchmark; `regress`
  compares exact trades and records no solver version, so a solver upgrade reads as a change. And one opinion: a
  model is mostly a job. Params, inputs, a pure transform, a check and one appended table with a ledger are the job
  contract, and what is optim's own is the fan-out per portfolio, rounding and acceptance, `reserve`, and sweeps.
  Its second copy of snapshots, runs, the loader and settings is where findings came from that job's side had
  already closed.
- **Proving a port.** Per system, by its owner. No verb.
- **The firm's CI.** The roadmap's 4.6. The generated pipeline's `basis check --full` stage stays ready; note that
  its checkout must fetch what 7.12's contract check needs, or nothing once the released document is committed.

## Open decisions

Each has a recommendation, and none blocks a phase before the step named.

| Decision | Recommendation | Step |
|---|---|---|
| Does `monkeypatch.setattr` count as a mock? The mock gate does not see it, and basis's own tests use it 46 times | Count it in `basis review`; do not refuse it | 8.2 |
| May a repo lower its coverage floor? | Yes, as a `[tool.basis]` exception with a reason, which `basis review` lists | 8.1 |
| Where does Schemathesis run? | `check --full`, so the commit hook stays at seconds | 8.8 |
| Does a fixture run from the command need an environment? | No | 9.2 |
| Must a writing route ask for a user or declare `anonymous=True`? | Yes; it changes the example | 7.10 |
| Is `/docs` served by default? | No, behind a setting | 7.10 |
| How does a query filter a zoned datetime: binds in the column's zone, or documentation alone? | Binds in the zone | 7.3 |
| Server-sent events or WebSockets for live updates? | Server-sent events, with basis-ui's agreement | 12.1 |
| Is basis itself held to a coverage floor? | 85%, in CI | 6.1 |

## Considered, and not recommended

| Option | Why not |
|---|---|
| pandera, Great Expectations or Soda for data checks | basis already conforms every frame to its row at every boundary; what is missing is checks against a job's own history, which the run store makes cheap (12.5) |
| testcontainers | The desktops have no Docker, and CI already uses service containers |
| Pact, or other consumer-driven contracts | One UI project consumes each API through `openapi.json`; a committed released document and a truer diff (7.12) cover it |
| oasdiff in place of the Python diff | It ships as a Go binary, outside a uv-only toolchain on a Windows desktop. Keep the Python diff, test it on a corpus of schema pairs, and make what it skips visible |
| A second type checker beside ty | ty passes clean in 0.4 s with 17 reasoned suppressions in source; the exact pin, owned by basis (8.1), is the right handling of a pre-release tool |
| Lineage tooling such as OpenLineage | Each run's manifest already names its inputs by hash; nothing here asked for more |

## Sources

What the research pass read, for the steps that lean on it. Fetched on 2026-11-09.

- Claude Code's docs on [permissions](https://code.claude.com/docs/en/permissions), which say shell rules are not a
  security boundary, and on [hooks](https://code.claude.com/docs/en/hooks) (8.9).
- Anthropic, [Writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents) and
  [Building a C compiler](https://www.anthropic.com/engineering/building-c-compiler): errors that say what to
  change, few lines of output, and a verifier that is nearly perfect (8.10, 9.3, 9.5).
- [ty's README](https://github.com/astral-sh/ty): diagnostics may change between any two versions (8.1).
- [mutmut](https://pypi.org/project/mutmut/): needs `fork`, so WSL or Linux (8.8).
- [Verified fakes](https://pythonspeed.com/articles/verified-fakes) and Fowler's
  [Contract Test](https://martinfowler.com/bliki/ContractTest.html): one suite run against the fake and the real
  thing (6.5).
- [`sp_getapplock`](https://learn.microsoft.com/en-us/sql/relational-databases/system-stored-procedures/sp-getapplock-transact-sql)
  and [`DBMS_LOCK`](https://docs.oracle.com/en/database/oracle/oracle-database/19/arpls/DBMS_LOCK.html) (7.2).
- The IETF's [Idempotency-Key draft](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/):
  a key reused with another payload should be refused (12.8).
- Uber's [D3](https://www.uber.com/blog/d3-an-automated-system-to-detect-data-drifts): null rates, row counts and
  distributions against history as the checks that catch most incidents (12.5).
- [griffe's API checks](https://mkdocstrings.github.io/griffe/guide/users/checking/), Django's
  [deprecation policy](https://docs.djangoproject.com/en/dev/internals/release-process/) and Google's chapter on
  [large-scale changes](https://abseil.io/resources/swe-book/html/ch22.html): a migration succeeds when a computer
  does most of it (11.6).
