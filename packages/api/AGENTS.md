# Agent guide

This package is basis-api, which API repos depend on. A service declares its name, its settings, its resources and
its routes as an `api.Service`, and basis builds the FastAPI app around them, the same in every repo; a service never
makes the app itself. The plumbing it runs on is basis-core's, in `packages/core`, which imports nothing from here.
[The design](../../docs/design.md#apis-basisapi) holds the decisions, and [the roadmap](../../docs/roadmap.md#phase-2-apis)
the steps still to come: contract tests (2.6) and `basis-api new app` (2.7). What a UI can rely on, field by field,
is [the contract page](../../docs/contract.md).

## Where things are

Paths are under `src/basis/api/`.

| Path | Holds |
|---|---|
| `__init__.py` | The declarations' names; `api.build` and `api.serve` load on first use, so a service's modules import no server. |
| `service.py` | `Service`, `Route` and `get`/`post`/`put`/`patch`/`delete`, with what each route declares (its deadline, its response, its errors, and whether it answers a dataset) and `Route.answers`; `given`, what a handler asks basis for, and the checks of what it asks for; `load`, a service by entry point (`basis.apis`) or module. |
| `strict.py` | How a request is read, strictly: a JSON body in pydantic's strict mode, with decimals as strings and times with offsets, and query and path text by each type's grammar. Its docstring is the reference. |
| `compute.py` | `compute`, which declares a pure function a route runs in the process pool, and `Compute`, what a handler gets for it. Its docstring is the reference. |
| `resources.py` | The resources a service declares (`database`, `http`, `cache`, `clock`, `ids`), a database's `outbox`, what a handler gets for each (`Reader`, `Writer`, `Client`, `Cache`, `Clock`, `Ids`), with `Client.send`, and `DeadlineExceededError` and `CallFailedError`. Its docstring is the reference. |
| `opened.py` | Each resource as the app opens it, through basis-core's databases, httpx2, memory and the system's clock, or a fake: every call's deadline and span, a request's transaction with its outbox rows, the calls made after its commit, and the checks at build and startup. |
| `outboxes.py` | The outbox: a row's life from `send` to `done`, `refused` or `expired`, the 4xx a call is retried after, its backoff, its one compare-and-set change (`Table.update`) and the transitions built on it, its statements in each dialect (`Sql`), and its table's DDL. Its docstring is the reference. |
| `relay.py` | `basis relay`: one run over a service's outboxes, which claims each due row, makes its call and records it, and reports each refused one, each statement with a span and a deadline, a failure stopping only its row; it fails, naming each call refused or expired, and each row a failure stopped. |
| `deadlines.py` | A route's deadline, which caps each call its request makes: `limit`, how long one call may take, and whose deadline sets it. |
| `pool.py` | The process pool, spawned at startup: each compute call's deadline, span and log fields, and the in-process runs of `fakes.Compute`. |
| `routes.py` | Each route as the app serves it: its deadline, its request read strictly before FastAPI's handler, its undeclared errors refused, a dataset's Arrow when the request asks for it, and `add`, which adds the routes to an app with each operation's id its handler's name and its errors under their statuses. |
| `openapi.py` | The OpenAPI document, as the app serves it and a repo commits it: `Application`, whose `openapi()` `finish`es FastAPI's (problem media types, strict models, a request's decimals as strings of their digits); `document` and `text`, made without settings; `operations`, `mapping` and `array`, which `contract.py` reads documents by too. Its docstring is the reference. |
| `decimals.py` | A request's decimal as the document gives it: its pattern from its digits and places, and its bounds in `x-basis-decimal`, from the types the routes read. Its docstring is the reference. |
| `contract.py` | The breaking-change check, basis-api's `Part.contract`: `openapi.json` against the one at the last release the repo's `[tool.basis.api] releases` glob matches, and what breaks a UI on each side. Its docstring is the reference. |
| `dataset.py`, `arrow.py` | A dataset route's rows as Arrow columns, checked when the route is declared, which declarations import; and the stream itself, with pyarrow, and the `Accept` that asks for it. Their docstrings are the reference. |
| `origins.py` | The `allowed_origins` setting, `BASIS_ALLOWED_ORIGINS`: each origin as a browser sends it, and the CORS middleware, outermost, when there are any. |
| `fakes.py` | A fake of each kind of resource, a fake database's outbox in memory among them, and `Compute`, which runs a compute function in the process, which `api.build(..., fakes=...)` and `relay.plan` take for tests. |
| `settings.py` | `Settings`, basis-core's with `serve`, `traces`, `http`, `compute`, `identity` and `allowed_origins`; `Settings.given`, settings from arguments alone, for tests; `read`, which names each variable to set, and refuses the development key off a declared desktop. |
| `identity.py` | Who makes a request: `User`, which a service subclasses with the claims it needs; `SignInRequiredError` and the other identity errors; the `Identity` settings and their keys (`Jwks`, `PublicKey`, `Development`); `Machine`, how a desktop is declared, and the development key's refusal. Its docstring is the reference. |
| `tokens.py` | Verifying bearer tokens with PyJWT: `plan`, the checks at build; `Verifier` and `KeySet`, the JWK set's fetches and cache; `Bearer`, the middleware inside the request boundary; `DEVELOPMENT_KEY` and `sign`, for tests and `basis dev`. |
| `errors.py` | `Error`, which a service subclasses with a status and a title, or with `abstract=True` as a base for others; `Problem`, and `ValidationProblem`, every 422's, whose `errors` are `PointerError`s and `ParameterError`s; `pointer`; `problem`, the one exception map. |
| `app.py` | `build`: the app, its lifespan, which opens and closes the resources, its exception handlers, its health endpoints and the `Lifecycle` readiness reports. |
| `requests.py` | The request boundary: the request id, log fields, `X-Request-Id` and `traceparent` out, one log line per request, and the 500 for an exception nothing expected. |
| `traces.py` | The app's own tracer provider, which FastAPI's telemetry makes server spans with, and the exporter the settings name. |
| `server.py` | `serve`: uvicorn, with JSON logs through basis-core, a drain on SIGTERM before the graceful shutdown, and a second signal that stops at once; on the service's fakes and 127.0.0.1 alone for `basis dev`. |
| `cli.py` | `part`, which the `basis.verbs` entry point named `api` gives the `basis` command: `basis serve [SERVICE]`, `basis dev [SERVICE]`, `basis call ROUTE [REQUEST] --fake\|--real` and `basis relay [SERVICE]`, its side of core's `basis ddl [SERVICE] --dialect DIALECT`, the files below, and the contract. |
| `call.py` | `basis call`: one request to one route, in this process, through FastAPI's `TestClient`, its response printed as JSON. |
| `dev.py` | The fakes a service declares with `Service(fakes="module:function")`, for `basis dev` and `basis call --fake`, checked to fake every database and other service. |
| `sync.py`, `templates/agents.md` | What `basis sync` writes for an API: its block of `AGENTS.md`, its Dockerfile line, the image's `serve` and `relay` commands, and the repo's `openapi.json`, made from its one service, a failure to make it one `ERROR` line. |
| `example/` | The example service, accrued interest on a bond, which reads and writes a database, calls a pricing service, uses a cache, the clock and ids, solves for a yield in its process pool (`compute.py`), answers a dataset, a bond's coupons, and books each settlement with a ledger service through the warehouse's outbox, with its fakes for `basis dev` (`dev.py`) and its tests on fakes. Its `openapi.json` is the sample basis-ui pins; `python -m basis.api.example` writes it, and its tests check it is current and keeps its contract with the last `v*` release. |

## Rules to keep

- A service's declarations, and the example's, import no server, exporter, database driver, HTTP client or fake;
  `lint-imports` enforces it. A handler reaches the outside only through a resource that `api.given` gives it, in its
  own parameters: a route refuses one asked for in a dependency of the handler's, so its signature shows all it reaches.
- A compute module imports no part of basis, no driver, client, server or settings, which its contract enforces; a
  repo's compute modules are those named `compute`, as `api.compute` requires. Its functions take values and return
  one, across the pool's processes by pickling.
- Every route declares `deadline_s`, `response` (its handler's return annotation) and `errors`, with no default for
  any. A route answers only the errors in `Route.answers`: its own, and basis's that it can meet, which `service._met`
  derives from what it asks for, `SignInRequiredError` among them where it asks for the user. Any other `api.Error`, or an `HTTPException`, is a defect, a 500. A new error that
  basis raises inside a route joins `BASIS_ERRORS` and `_met`.
- A request is read strictly before FastAPI sees it (`strict.py`): a key nothing declares is refused, a body's values
  have their own JSON types, a decimal is a string, a time has its offset, and query and path text reads by its type's
  grammar. A type basis cannot read strictly is refused when the route is declared, and so is a request model that
  reads a field by any key but a plain alias or its name, a set of models in a body, and `BackgroundTasks`. The body is
  validated once: FastAPI's handler gets basis's value through `routes._ValidatedOnce`, and never validates it again.
  A body sent to a route that declares none is a 422.
- A route's deadline caps every call its request makes, the commit included; never the rollback, so a connection
  always goes back. A compute call has only its route's deadline.
- Every resource is declared by name with everything it needs, its deadline and a database's `max_concurrent`
  included, and basis guesses none of it. Every call through one has a span, and every call that can wait a deadline,
  in `opened.py`, whether the resource is real or fake; a fake replaces only what the resource reaches.
- A request's statements in a database share one transaction, which commits only after the handler returns and the
  response validates, and rolls back otherwise; a route writes one database at most, and a GET none. A statement that
  fails, misses its deadline or is cancelled is never followed by a commit, and the request answers an error, not a
  2xx. Each database's statements run on its own thread pool; the commit and the rollback have its deadline too.
- A call that changes another service's state, `Client.send` with its `Idempotency-Key`, is a row of the outbox of the
  database its route writes, in the request's transaction, and is made only once that commits, at once with the
  request's other calls and within the route's deadline; its failure then changes nothing the request answers, and
  `basis relay`, run by the repo's scheduler and never in the API, makes it again until it is accepted, refused for
  good by a 4xx but 408, 409, 425 or 429, or ages out. A route that writes no database makes it at once. Only the
  route whose transaction it is sends through it (`opened._WRITING`, set with the route's deadline and reset when the
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
- A request's decimal in the document has its digits and places in its pattern, and its bounds in `x-basis-decimal`,
  from its type's declarations (`decimals.py`), never from pydantic's schema of it.
- A dataset route's rows are flat, each field a column Arrow holds, which `dataset.rows` checks when the route is
  declared; its Arrow is written only after the rows validate, so a request's writes commit after both.
- No page of another origin calls the API unless the settings name its origin, as a browser sends it.
- An unexpected error is logged once, by the request boundary, with its traceback, and its message never reaches the
  client.
- Settings come from `Settings` alone, as `BASIS_*` variables, and a failure names them with basis-core's
  `settings.variable`; `OTEL_*` variables configure nothing basis decides.
- A failure the developer can fix is a `BasisError`, such as `ServiceError` or basis-core's `SettingsError`, whose
  message ends with the fix, so `basis` prints it as one line.
- Each app has its own tracer provider; nothing sets OpenTelemetry's global one.
- A token that does not verify is a 401, never anonymity; only a request with no `Authorization` header is anonymous.
  Verification accepts only the settings' algorithms, all of them asymmetric, and a key only for an algorithm it fits;
  it never reads a key the token carries. Every check it makes is in `tokens.py`'s docstring, and each has a test.
- The development key is published, so every app refuses it on any machine that is not a declared desktop
  (`identity.Machine`), in `tokens.plan`, which every app passes through; only settings from `Settings.given`, which a
  test makes in code and no variable can mark, take it anywhere.
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

The HTTP client is httpx2: Pydantic's maintained continuation of httpx, which its README says sees little activity
now, with the API agents already know, an async client whose calls a deadline can cancel, and the ASGI transport that
the fake of another service runs on, in the process. Starlette's `TestClient` already prefers it, so it adds no new
dependency to the workspace. aiohttp is async too, but has no ASGI transport, so a fake would need a server; requests
and urllib3 are sync only, so each call would hold a worker thread past its deadline.
