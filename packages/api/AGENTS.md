# Agent guide

This package is basis-api, which API repos depend on. A service declares its name, its settings, its resources and
its routes as an `api.Service`, and basis builds the FastAPI app around them, the same in every repo; a service never
makes the app itself. The plumbing it runs on is basis-core's, in `packages/core`, which imports nothing from here.
[The design](../../docs/design.md#apis-basisapi) holds the decisions, and [the roadmap](../../docs/roadmap.md#phase-2-apis)
the steps still to come: the UI contract (2.5), contract tests (2.6) and `basis-api new app` (2.7).

## Where things are

Paths are under `src/basis/api/`.

| Path | Holds |
|---|---|
| `__init__.py` | The declarations' names; `api.build` and `api.serve` load on first use, so a service's modules import no server. |
| `service.py` | `Service`, `Route` and `get`/`post`/`put`/`patch`/`delete`, with what each route declares (its deadline, its response and its errors) and `Route.answers`; `given`, what a handler asks basis for, and the checks of what it asks for; `load`, a service by entry point (`basis.apis`) or module. |
| `strict.py` | How a request is read, strictly: a JSON body in pydantic's strict mode, with decimals as strings and times with offsets, and query and path text by each type's grammar. Its docstring is the reference. |
| `compute.py` | `compute`, which declares a pure function a route runs in the process pool, and `Compute`, what a handler gets for it. Its docstring is the reference. |
| `resources.py` | The resources a service declares (`database`, `http`, `cache`, `clock`, `ids`), what a handler gets for each (`Reader`, `Writer`, `Client`, `Cache`, `Clock`, `Ids`), and `DeadlineExceededError` and `CallFailedError`. Its docstring is the reference. |
| `opened.py` | Each resource as the app opens it, through basis-core's databases, httpx2, memory and the system's clock, or a fake: every call's deadline and span, a request's transaction, and the checks at build and startup. |
| `deadlines.py` | A route's deadline, which caps each call its request makes: `limit`, how long one call may take, and whose deadline sets it. |
| `pool.py` | The process pool, spawned at startup: each compute call's deadline, span and log fields, and the in-process runs of `fakes.Compute`. |
| `routes.py` | Each route as the app serves it: its deadline, its request read strictly before FastAPI's handler, its undeclared errors refused, and its errors in the OpenAPI document as `application/problem+json`. |
| `fakes.py` | A fake of each kind of resource, and `Compute`, which runs a compute function in the process, which `api.build(..., fakes=...)` takes for tests. |
| `settings.py` | `Settings`, basis-core's with `serve`, `traces`, `http`, `compute` and `identity`; `Settings.given`, settings from arguments alone, for tests; `read`, which names each variable to set, and refuses the development key off a declared desktop. |
| `identity.py` | Who makes a request: `User`, which a service subclasses with the claims it needs; `SignInRequiredError` and the other identity errors; the `Identity` settings and their keys (`Jwks`, `PublicKey`, `Development`); `Machine`, how a desktop is declared, and the development key's refusal. Its docstring is the reference. |
| `tokens.py` | Verifying bearer tokens with PyJWT: `plan`, the checks at build; `Verifier` and `KeySet`, the JWK set's fetches and cache; `Bearer`, the middleware inside the request boundary; `DEVELOPMENT_KEY` and `sign`, for tests and `basis dev`. |
| `errors.py` | `Error`, which a service subclasses with a status and a title, or with `abstract=True` as a base for others; `Problem`; `problem`, the one exception map. |
| `app.py` | `build`: the app, its lifespan, which opens and closes the resources, its exception handlers, its health endpoints and the `Lifecycle` readiness reports. |
| `requests.py` | The request boundary: the request id, log fields, `X-Request-Id` and `traceparent` out, one log line per request, and the 500 for an exception nothing expected. |
| `traces.py` | The app's own tracer provider, which FastAPI's telemetry makes server spans with, and the exporter the settings name. |
| `server.py` | `serve`: uvicorn, with JSON logs through basis-core, a drain on SIGTERM before the graceful shutdown, and a second signal that stops at once; on the service's fakes and 127.0.0.1 alone for `basis dev`. |
| `cli.py` | `part`, which the `basis.verbs` entry point named `api` gives the `basis` command: `basis serve [SERVICE]`, `basis dev [SERVICE]` and `basis call ROUTE [REQUEST] --fake\|--real`, and the files below. |
| `call.py` | `basis call`: one request to one route, in this process, through FastAPI's `TestClient`, its response printed as JSON. |
| `dev.py` | The fakes a service declares with `Service(fakes="module:function")`, for `basis dev` and `basis call --fake`, checked to fake every database and other service. |
| `sync.py`, `templates/agents.md` | What `basis sync` writes for an API: its block of `AGENTS.md`, and its Dockerfile line, the image's `serve` command. |
| `example/` | The example service, accrued interest on a bond, which reads and writes a database, calls a pricing service, uses a cache, the clock and ids, and solves for a yield in its process pool (`compute.py`), with its fakes for `basis dev` (`dev.py`) and its tests on fakes. |

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
- How the app reaches another service is its settings' alone: its URL, CA bundle and proxy, never `SSL_CERT_FILE`,
  `HTTP_PROXY` or any other variable of the environment's.
- Every error a client sees is a problem from `errors.problem`. A new kind of error is a case there, or an `Error`
  subclass; never a response built in a handler.
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
