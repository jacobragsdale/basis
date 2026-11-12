# Agent guide

This package is basis-api, which API repos depend on. A service declares its name, its settings, its resources and
its routes as an `api.Service`, and basis builds the FastAPI app around them, the same in every repo; a service never
makes the app itself. The plumbing it runs on is basis-core's, in `packages/core`, which imports nothing from here:
another service's client, the clock, ids, each call's deadline and span, tracing and the service errors every resource
raises are basis-core's, which jobs share (`basis.core.resources`, `opened`, `fakes`, `deadlines`, `traces` and
`errors`), and this package builds on them.
[The design](../../docs/design.md#apis-basisapi) holds the decisions, and [the roadmap](../../docs/roadmap.md#phase-2-apis)
and [the plan](../../PLAN.md) the steps. What a UI can rely on, field by field, is
[the contract page](../../docs/contract.md).

## Where things are

Paths are under `src/basis/api/`.

| Path | Holds |
|---|---|
| `__init__.py` | The declarations' names; `api.build` and `api.serve` load on first use, so a service's modules import no server. |
| `service.py` | `Service`, `Route` and `get`/`post`/`put`/`patch`/`delete`, with what each route declares (its deadline, its response, its errors, and whether it answers a dataset) and `Route.answers`, and the checks of what each handler asks for. |
| `dependencies.py` | `given`, what a handler asks basis for, as FastAPI's dependencies, and `Runtime` and `running`, what a started app gives them; a database's is the request's transaction. |
| `loading.py` | `load`, a service by entry point (`basis.apis`) or module. |
| `internals.py` | The parts of FastAPI's internals basis builds on (`ModelField`, `Dependant`, `get_dependant`, a dependant's `body_params` and `call`), and `ValidatedOnce`; `tests/test_internals.py` fails on a FastAPI upgrade that moves one. Nothing else imports them. |
| `strict.py` | How a request is read, strictly: a JSON body in pydantic's strict mode, with every number a JSON number, a `Decimal` refused, and times with offsets, and query and path text by each type's grammar. Its docstring is the reference. |
| `compute.py` | `compute`, which declares a pure function a route runs in the process pool, and `Compute`, what a handler gets for it. Its docstring is the reference. |
| `resources.py` | The resources a service declares (`database`, `http`, `cache`, `clock`, `ids`), a database's `outbox`, and what a handler gets for each (`Reader`, `Writer`, `Client`, `Cache`, `Clock`, `Ids`), with `Client.send`. `http`, `clock` and `ids` are basis-core's declarations, which jobs share. Its docstring is the reference. |
| `opened.py` | Each resource as the app opens it, through basis-core's databases, basis-core's resources (another service's client, the clock and ids, `basis.core.opened`) and memory, or a fake (`Resources`), and the check at startup that each database answers. Its docstring says what every call gets. |
| `planning.py` | `plan`, the checks when the app is built: each fake fakes a resource of the service's, of its kind, and the settings configure every other. |
| `sessions.py` | A request's side of a database: `OpenDatabase`, its threads and spans; `DatabaseSession`, the request's transaction, every statement's and the commit's deadline; the outbox rows a route that writes sends, made once it commits; and `HttpClient`, `api.Client`, whose `send` writes them. |
| `caches.py` | `MemoryCache`, a cache resource in the process's memory. |
| `outboxes.py` | The outbox: a row's life from `send` to `done`, `refused` or `expired`, the 4xx a call is retried after, its backoff, its one compare-and-set change (`Table.update`) and the transitions built on it, its statements in each dialect (`Sql`), and its table's DDL. Its docstring is the reference. |
| `relay.py` | `basis relay`: one run over a service's outboxes, which claims each due row, makes its call and records it, and reports each refused one, each statement with a span and a deadline, a failure stopping only its row; it fails, naming each call refused or expired, and each row a failure stopped. |
| `deadlines.py` | A route's deadline, basis-core's deadline around a block's calls (`basis.core.deadlines`, of kind `route`), which caps each call its request makes, basis-core's client's included: `limit`, how long one call may take, and whose deadline sets it, and `exceeded`, its 504. |
| `pool.py` | The process pool, spawned at startup: each compute call's deadline, span and log fields, an exception sent back only where pickle makes it again (`UncrossableError` otherwise), new processes once calls past their deadlines hold every one, and the in-process runs of `fakes.Compute`, which answer exceptions as the pool does. |
| `routes.py` | Each route as the app serves it: its deadline, its request read strictly before FastAPI's handler, a domain exception answered as the most specific meaning the route declares, its undeclared errors refused, a dataset's Arrow when the request asks for it, and `add`, which adds the routes to an app with each operation's id its handler's name and its errors under their statuses. |
| `idempotent.py` | A request that can be sent twice: a route's `idempotency=`, its requests' `Idempotency-Key`s kept in the written database's `api.idempotency` table, in the request's transaction, with the request's fingerprint and its answer; the same request again answers the same, another with the key is refused, and a key in use is a 409. Its table's statements in each dialect (`Sql`) and its DDL. Its docstring is the reference. |
| `streams.py` | A stream of live updates, `api.stream`, as server-sent events: its handler called again every `every_s`, each call within the route's deadline and in a transaction of its own, an event when its answer changes, a heartbeat, a slow reader dropped, and its end when the token expires, `serve.stream_s` passes or the app drains. Its docstring is the reference. |
| `openapi.py` | The OpenAPI document, as the app serves it and a repo commits it: `Application`, whose `openapi()` `finish`es FastAPI's (problem media types and strict models; every number a JSON number); `document` and `text`, made without settings; `operations`, `mapping` and `array`, which `contract.py` reads documents by too. Its docstring is the reference. |
| `contract.py` | The breaking-change check, basis-api's `Part.contract`: `openapi.json` against the committed `openapi.released.json`, which `basis release-document VERSION` writes (`release`), with no tag or history read; what breaks a UI on each side, each model of a union by itself, each operation's `x-basis-errors` by type, and each keyword it does not compare listed. Its docstring is the reference. |
| `dataset.py`, `arrow.py` | A dataset route's rows as Arrow columns, checked when the route is declared, which declarations import; and the stream itself, with pyarrow, and the `Accept` that asks for it. Their docstrings are the reference. |
| `ui.py` | A built UI the app serves at `/` (`Service(ui="web/dist")`), its routes then under `/api` and its document listing `/api` as its server, with its paths unprefixed: a file as it is, any other path the UI's `index.html`, a path under `/api` no route has the API's 404, and a folder not built yet a 503 that says how. Its docstring is the reference. |
| `tables.py` | `api.Table[Row]`, what a dataset route answers when its handler returns a pandas frame or an Arrow table, which `arrow.conform` checks once against the row's columns (names, types, nulls, zones), never row by row, and answers as Arrow (`arrow.write`) or JSON written from it (`arrow.as_json`); a `Table` row declares no value constraints; `Reader.fetch_table` reads a query's Arrow the same way. Its docstring is the reference. |
| `format.py` | `api.format.number`, `money`, `pct`, `bps`, `price` and `price32`: a number's display format, `Annotated[float, api.format.money("USD")]`, each basis-core's `display.Display`, checked where it is declared, which pydantic writes into the field's schema as `x-basis-format` and `arrow.schema` into a dataset's Arrow field metadata as `basis.format`, the object agreed with basis-ui (contract.md, "Display formats"). Its docstring is the reference. |
| `origins.py` | The `allowed_origins` setting, `BASIS_ALLOWED_ORIGINS`: each origin as a browser sends it, and the CORS middleware, outermost, when there are any. |
| `fakes.py` | A fake of each kind of resource, a fake database's outbox in memory among them, basis-core's `Http`, `Clock` and `Ids`, and `Compute`, which runs a compute function in the process, which `api.build(..., fakes=...)` and `relay.plan` take for tests; `Database.fails`, a statement, the outbox's among them (`OUTBOX_PROBE` and the rest), or each, failing with a kind of service error, and each login too for an `Unavailable` or `Denied` one, as basis-core's `Http.fails` makes a path fail; both keep their failures in basis-core's `fakes.Failures`, and a fake service's client waits no time (`Http.waits`). |
| `settings.py` | `Settings`, basis-core's, which holds `http`, `traces` and `git_sha` too, with `serve`, `compute`, `identity` and `allowed_origins`; `Settings.given`, settings from arguments alone, for tests; `read`, which names each variable to set, and refuses the development key off a declared desktop. |
| `identity.py` | Who makes a request: `User`, which a service subclasses with the claims it needs; `SignInRequiredError` and the other identity errors; the `Identity` settings and their keys (`Jwks`, `PublicKey`, `Development`); `Machine`, how a desktop is declared, and the development key's refusal. Its docstring is the reference. |
| `tokens.py` | Verifying bearer tokens with PyJWT: `plan`, the checks at build; `Verifier` and `KeySet`, the JWK set's fetches and cache; `Bearer`, the middleware inside the request boundary; `DEVELOPMENT_KEY` and `sign`, for tests and `basis dev`. |
| `errors.py` | `Error`, which a service subclasses with a status and a title, or with `abstract=True` as a base for others; `error`, the HTTP meaning of a domain's plain exception, a `DomainError` a route declares; `DeclarationError`, a service that does not declare what basis needs; each kind of basis-core's `ServiceError` as its problem (`described`); `Problem`, and `ValidationProblem`, every 422's, whose `errors` are `PointerError`s and `ParameterError`s; `pointer`; `problem`, the one exception map. |
| `app.py` | `build`: the app, its own tracer provider (`basis.core.traces`), which FastAPI's telemetry makes server spans with, and the exporter the settings name, its lifespan, which opens and closes the resources, its exception handlers, its health endpoints and the `Lifecycle` readiness reports. |
| `requests.py` | The request boundary: the request id in its log fields, beside the trace's and span's ids basis-core's logs add, `X-Request-Id` and `traceparent` out, one log line per request, and the 500 for an exception nothing expected. |
| `server.py` | `serve`: uvicorn, with JSON logs through basis-core, a drain on SIGTERM before the graceful shutdown, and a second signal that stops at once; on the service's fakes and 127.0.0.1 alone for `basis dev`. |
| `cli.py` | `part`, which the `basis.verbs` entry point named `api` gives the `basis` command: `basis serve [SERVICE]`, `basis dev [SERVICE]`, `basis call ROUTE [REQUEST] --fake\|--real`, `basis relay [SERVICE]` and `basis release-document VERSION`, each relay run one trace, its side of core's `basis ddl [SERVICE] --dialect DIALECT`, the files below, the contract, and its checks of the repo's code. |
| `layers.py` | An API's layers, which `basis check` holds with grimp in every API repo: imports go down service, handlers, repository, the declarations and domain; no layer reaches a driver, a client or a socket (`OUTSIDE`, from basis-core's `testing.FORBIDDEN`) or reads the environment, the clock or new ids itself; a domain module stays pure; and a handler never calls a method of what `api.given` gives it, through a type alias too. Tests and jobs are no layer. Its docstring is the reference. |
| `fuzz.py` | The app on its fakes, built from its settings' defaults: in `basis check`, `builds`; in `basis check --full`, sent Schemathesis's requests from its own document, through `from_asgi`, signed in as the service's `example_user`, each failure a file of the request as sent, which `basis call ROUTE @FILE --fake` makes again. Its docstring is the reference. |
| `smoke.py` | `basis smoke --env ENV`, basis-api's side (`Part.smoke`): the API as the environment serves it, at `BASIS_SMOKE__URL`, the base its document's paths are under there: its `openapi.json` is the committed one but for the marker, `health/ready` answers 200, and each GET route's read-only example, `smoke=`, answers a 2xx whose JSON its response holds, strictly; GETs alone. An app that serves a UI answers its document and health under `/api` too. Its docstring is the reference. |
| `call.py` | `basis call`: one request to one route, in this process, through FastAPI's `TestClient`, its response printed as JSON, its request given as JSON, from stdin or from a file (`@FILE`), as its parts or as it was sent, and signed in as a `user` on the fakes; `answer`, the same for a caller in Python, `api.call_route`, with settings in code. |
| `dev.py` | The fakes a service declares with `Service(fakes="module:function")`, for `basis dev` and `basis call --fake`, checked to fake every database and other service. |
| `new.py`, `templates/repo/` | `basis-api new app NAME [JOB]` (the `basis-api` console script, `cli.scaffold`): an API repo of the example in its layers, its package the name with underscores, declaring `[tool.basis] parts = ["api", "job"]` and `coverage = 90`, its first job made by `basis new job` and its files by `basis sync`, both once the repo is installed. |
| `docs/`, `skills/` | The tutorial, how-to and reference `basis docs` lists as `api/PAGE`, and the `api-*` skills `basis sync` writes; the tests in `tests/test_documented.py` hold each name and command they give to the code. |
| `sync.py`, `templates/agents.md` | What `basis sync` writes for an API: its block of `AGENTS.md`, its skills, its Dockerfile line, the image's `serve` and `relay` commands, and the repo's `openapi.json`, made from its one service, a failure to make it one `ERROR` line. |
| `example/` | The example service, accrued interest on a bond, in its layers (`service.py`, `handlers.py`, `repository.py`, `domain.py`, `resources.py` and `errors.py`), which reads and writes a database, calls a pricing service, uses a cache, the clock and ids, solves for a yield in its process pool (`compute.py`), answers a dataset, a bond's coupons, and books each settlement with a ledger service through the warehouse's outbox, with its fakes for `basis dev` (`dev.py`), and its tests by layer: the domain's on values, the handlers' on fakes, and the repository's in the integration tier. Its `openapi.json` is the sample basis-ui pins; `python -m basis.api.example` writes it, and its tests check it is current and keeps its contract with `openapi.released.json`, which `scripts/release.py version` writes beside it. The package's `tests/test_gates.py` makes a repo of a copy of it, which passes every gate `basis check` holds, and of a copy that breaks each rule. |

## Rules to keep

- A service's declarations, and the example's, import no server, exporter, database driver, HTTP client or fake;
  `lint-imports` enforces it. A handler reaches the outside only through a resource that `api.given` gives it, in its
  own parameters: a route refuses one asked for in a dependency of the handler's, so its signature shows all it reaches.
- A compute module imports no part of basis, no driver, client, server or settings, which its contract enforces; a
  repo's compute modules are those named `compute`, as `api.compute` requires. Its functions take values and return
  one, across the pool's processes by pickling.
- An API's code keeps its layers, which `basis check` holds in every API repo (`layers.py`), and the example's keeps
  them too, which the package's `tests/test_gates.py` checks; a domain module imports nothing of basis, and gives an exception's
  HTTP meaning to `errors.py`, as `api.error(domain.Matured, status=..., title=...)`, never an `api.Error`.
- `basis check` builds the app on its fakes, and `basis check --full` sends it Schemathesis's requests (`fuzz.py`,
  `Part.full_checks`), in its own process, with no network, writing nothing to the repo and logging nothing while it
  does, and the same requests on every run, so it is never flaky; Schemathesis is a repo's development dependency, and basis-api imports it only there.
- Every route declares `deadline_s`, `response` (its handler's return annotation) and `errors`, with no default for
  any. A route answers only the errors in `Route.answers`: its own, and basis's that it can meet, which `service._met`
  derives from what it asks for: each kind of `api.ServiceError` that one of its resources or compute functions can
  raise, and `SignInRequiredError` where it asks for the user. Any other `api.Error` or kind, a domain exception whose
  meaning it does not declare, or an `HTTPException`, is a defect, a 500. A new error that basis raises inside a route
  joins `BASIS_ERRORS` and `_met`.
- A request is read strictly before FastAPI sees it (`strict.py`): a key nothing declares is refused, a body's values
  have their own JSON types, every number is a JSON number a float holds and a `Decimal` is refused when the route is declared, an object gives each key once, a time has its offset, and query and path text reads by its type's
  grammar. A body is no larger than `serve.max_body_bytes`, a 413 `BodyTooLargeError` otherwise, and one over `routes.OFF_THE_LOOP` is parsed in a thread. A type basis cannot read strictly is refused when the route is declared, and so is a request model that
  reads a field by any key but a plain alias or its name, and `BackgroundTasks`. The body is
  validated once: FastAPI's handler gets basis's value through `internals.ValidatedOnce`, and never validates it again.
  A body sent to a route that declares none is a 422.
- A route that declares `idempotency=` writes one database, which declares `api.idempotency`; its key is kept in the
  request's transaction, so it commits exactly when the writes do, and a replay runs no handler, writes nothing and
  sends nothing. The key is read strictly with the request, and the fake keeps its table in memory, a key another
  transaction holds failing as a unique key's insert does.
- A route never answers a `Response` of its own: one declared is refused, and one returned is a 500 whose writes roll
  back. A stream (`streams.py`) is basis's own response, each call of its handler a request of its own, with the
  route's deadline.
- A route's deadline caps every call its request makes, the commit included; never the rollback, so a connection
  always goes back. A compute call has only its route's deadline.
- Every resource is declared by name with everything it needs, its deadline and a database's `max_concurrent`
  included, and basis guesses none of it. Every call through one has a span, and every call that can wait a deadline,
  in `opened.py`, `sessions.py`, `caches.py` or basis-core's `opened.py`, whether the resource is real or fake; a fake replaces only what the
  resource reaches. Every resource, real or fake, fails with one of basis-core's kinds of `ServiceError`, which
  `api` gives as they are, with no class of its own and no conversion: a handler, or the repository it passes a
  resource to, catches a kind that means something to it, and any other answers its problem (`errors.described`), a
  `Timeout` 504, an `Unavailable` 503 and every other kind 502, so a handler needs no `try`. A fake fails on demand,
  and makes its failure as the real resource does, which each resource's contract suite checks against both
  (basis-core's `tests/test_http_contract.py`, and `tests/integration/test_database_contract.py`).
- A request's statements in a database share one transaction, which commits only after the handler returns and the
  response validates, and rolls back otherwise; a route writes one database at most, and a GET none. A statement that
  fails, misses its deadline or is cancelled is never followed by a commit, and the request answers an error, not a
  2xx. Each database's statements run on its own thread pool; the commit and the rollback have its deadline too.
- A call that changes another service's state, `Client.send` with its `Idempotency-Key`, is a row of the outbox of the
  database its route writes, in the request's transaction, and is made only once that commits, at once with the
  request's other calls and within the route's deadline; its failure then changes nothing the request answers, and
  `basis relay`, run by the repo's scheduler and never in the API, makes it again until it is accepted, refused for
  good by a 4xx but 408, 409, 425 or 429, or ages out: the rule is the status's, whatever kind the call fails with, so
  a `Conflict` and an `Unavailable` are made again, and a `NotFound`, a `Rejected` and a `Denied` refused. A route that writes no database makes it at once. Only the
  route whose transaction it is sends through it (`sessions._WRITING`, set with the route's deadline and reset when the
  transaction ends), so another app reached in the process never does. `outboxes.py`'s docstring says what each row's
  `status` and `next_at` mean; every change to a row but its insert is one compare-and-set on its status and attempts,
  in the SQL and in the fake, and an outbox's `max_age_s` outlasts the deadline, and a backoff, of every route that
  writes its database, which the service checks.
- How the app reaches another service is its settings' alone: its URL, CA bundle and proxy, never `SSL_CERT_FILE`,
  `HTTP_PROXY` or any other variable of the environment's.
- Every error a client sees is a problem from `errors.problem`. A new kind of error is a case there, or an `Error`
  subclass; never a response built in a handler. Every 422 is a `ValidationProblem`, whose `errors` name each field at
  fault, a body's by its JSON Pointer in fragment form and a parameter's by its name, whether basis raised it or the
  handler did.
- The OpenAPI document says what the app does: `openapi.finish` makes it so, and the app serves the document a repo
  commits, but for its first key, the marker. A change to what it holds is a change to the UI contract, and to
  basis-ui's: say so in the pull request, write the example's sample again (`python -m basis.api.example`), and update
  [the contract page](../../docs/contract.md).
- Every time a route reads or answers carries its offset: it is pydantic's `AwareDatetime`, and `strict.aware` refuses
  a route whose request or response holds a plain `datetime`, another of pydantic's datetimes, or a time of day,
  when it is declared, so pydantic refuses a time without its offset in the one validation it makes.
- Every number basis gives or takes is a JSON number, a float or an int, never a string; a `Decimal` anywhere in a
  route's request or response is refused when the route is declared (`strict.aware`), and NaN and ±inf travel as null,
  in JSON (pydantic's own) and Arrow (`arrow.py`) alike. A dataset's numbers are floats, its float columns always
  nullable, and an `int`, a `Decimal` or a choice of integers in its rows is refused (`dataset.py`).
- A dataset route's rows are flat, each field a column Arrow holds, which `dataset.rows` checks when the route is
  declared; its Arrow is written only after the rows validate, so a request's writes commit after both.
- No page of another origin calls the API unless the settings name its origin, as a browser sends it.
- An unexpected error is logged once, by the request boundary, with its traceback, and its message never reaches the
  client.
- Settings come from `Settings` alone, as `BASIS_*` variables, and a failure names them with basis-core's
  `settings.variable`; no `OTEL_*` variable changes what basis does (`basis.core.traces`), but `OTEL_SERVICE_NAME` and
  `OTEL_RESOURCE_ATTRIBUTES`, a platform's names for the process, which its spans and log records carry
  (`basis.core.telemetry`).
- A failure the developer can fix is a `BasisError`, such as `DeclarationError` or basis-core's `SettingsError`, whose
  message ends with the fix, so `basis` prints it as one line.
- Each app has its own tracer provider, from `basis.core.traces`; nothing sets OpenTelemetry's global one. Spans go
  only where `BASIS_TRACES__*` says, to stderr for `console`, never stdout, and nowhere when it is unset.
- A token that does not verify is a 401, never anonymity; only a request with no `Authorization` header is anonymous.
  Verification accepts only the settings' algorithms, all of them asymmetric, and a key only for an algorithm it fits;
  it never reads a key the token carries. Every check it makes is in `tokens.py`'s docstring, and each has a test.
- The development key is published, so every app refuses it on any machine that is not a declared desktop
  (`identity.Machine`), in `tokens.plan`, which every app passes through; only settings from `Settings.given`, which a
  test makes in code and no variable can mark, take it anywhere, and those `dev.settings` makes for an app on its fakes
  alone, in `basis dev` and `basis call --fake`. `api.serve` refuses it on any host but loopback, whatever the settings.
- A route that is not a GET asks for the user or declares `anonymous=True` (`service._check_anonymous`).
- A path to another service is a template, its values given as `segments=` and encoded each as one segment
  (`resources.filled`); a path with a query, a fragment or a dot segment is refused, and the outbox holds none.
- A commit that fails or passes its deadline once begun answers `OutcomeUnknownError`, logged with `outcome: unknown`
  and its reason; a service error's cause, which its problem never holds, is logged when it answers.
- `/docs` is served only with `serve.docs`, its Swagger UI pinned to one version; a refused token's problem names
  nothing of the API's sign-in, which its log line's `why` does.
- A token whose key the JWK set holds never waits for a fetch; only one naming a key it does not hold does.
- A test builds its app with `Settings.given(...)`, so the developer's environment never changes what it checks.

## Checks

From the workspace's root:

```sh
uv run pre-commit run --all-files   # ruff, ty, the lockfile and the import contracts
uv run pytest packages/api          # this package's tests and the example's, a few of them on a real process pool
uv run pytest -m integration packages/api   # real SQL Server and Oracle, as packages/job/tests/integration/README.md sets them up
```

Tokens are verified with PyJWT, which every basis environment already holds (mssql-python needs azure-identity, which
needs msal, which needs `pyjwt[crypto]`), so it adds no package to a repo's lock and no second JWT library to keep
patched. Its `decode` requires the algorithms it accepts, a JWK it parses is bound to one algorithm, its HMAC refuses a
PEM public key as a secret, and it refuses a critical header extension it does not know. It reads the system's clock
for `exp`, `nbf` and `iat`, so basis checks those itself, against a clock it is given. joserfc, the other maintained
choice, takes a clock and types each kind of key, but would be a second JWT library in every repo, for checks basis
makes itself anyway by accepting asymmetric algorithms only and fitting each key to the token's algorithm.

The HTTP client, basis-core's, is httpx2: Pydantic's maintained continuation of httpx, which its README says sees little activity
now, with the API agents already know, an async client whose calls a deadline can cancel, and the ASGI transport that
the fake of another service runs on, in the process. Starlette's `TestClient` already prefers it, so it adds no new
dependency to the workspace. aiohttp is async too, but has no ASGI transport, so a fake would need a server; requests
and urllib3 are sync only, so each call would hold a worker thread past its deadline.
