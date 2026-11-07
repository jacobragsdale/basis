# Agent guide

This package is basis-api, which API repos depend on. A service declares its name, its settings and its routes as an
`api.Service`, and basis builds the FastAPI app around them, the same in every repo; a service never makes the app
itself. The plumbing it runs on is basis-core's, in `packages/core`, which imports nothing from here.
[The design](../../docs/design.md#apis-basisapi) holds the decisions, and [the roadmap](../../docs/roadmap.md#phase-2-apis)
the steps still to come: resources (2.2), routes' deadlines, response models and errors (2.3), identity (2.4), the UI
contract (2.5), contract tests (2.6) and `basis-api new app` (2.7).

## Where things are

Paths are under `src/basis/api/`.

| Path | Holds |
|---|---|
| `__init__.py` | The declarations' names; `api.build` and `api.serve` load on first use, so a service's modules import no server. |
| `service.py` | `Service`, `Route` and `get`/`post`/`put`/`patch`/`delete`; `given`, what a handler asks basis for; `load`, a service by entry point (`basis.apis`) or module. |
| `settings.py` | `Settings`, basis-core's with `serve` and `traces`; `Settings.given`, settings from arguments alone, for tests; `read`, which names each variable to set. |
| `errors.py` | `Error`, which a service subclasses with a status and a title, or with `abstract=True` as a base for others; `Problem`; `problem`, the one exception map. |
| `app.py` | `build`: the app, its lifespan, its exception handlers, its health endpoints and the `Lifecycle` readiness reports. |
| `requests.py` | The request boundary: the request id, log fields, `X-Request-Id` and `traceparent` out, one log line per request, and the 500 for an exception nothing expected. |
| `traces.py` | The app's own tracer provider, which FastAPI's telemetry makes server spans with, and the exporter the settings name. |
| `server.py` | `serve`: uvicorn, with JSON logs through basis-core, a drain on SIGTERM before the graceful shutdown, and a second signal that stops at once. |
| `cli.py` | `part`, which the `basis.verbs` entry point named `api` gives the `basis` command: `basis serve [SERVICE]`, and the files below. |
| `sync.py`, `templates/agents.md` | What `basis sync` writes for an API: its block of `AGENTS.md`, and its Dockerfile line, the image's `serve` command. |
| `example/` | The example service, accrued interest on a bond, with its tests. |

## Rules to keep

- A service's declarations, and the example's, import no server, exporter or database; `lint-imports` enforces it.
- Every error a client sees is a problem from `errors.problem`. A new kind of error is a case there, or an `Error`
  subclass; never a response built in a handler.
- An unexpected error is logged once, by the request boundary, with its traceback, and its message never reaches the
  client.
- Settings come from `Settings` alone, as `BASIS_*` variables, and a failure names them with basis-core's
  `settings.variable`; `OTEL_*` variables configure nothing basis decides.
- A failure the developer can fix is a `BasisError`, such as `ServiceError` or basis-core's `SettingsError`, whose
  message ends with the fix, so `basis` prints it as one line.
- Each app has its own tracer provider; nothing sets OpenTelemetry's global one.
- A test builds its app with `Settings.given(...)`, so the developer's environment never changes what it checks.

## Checks

From the workspace's root:

```sh
uv run pre-commit run --all-files   # ruff, ty, the lockfile and the import contracts
uv run pytest packages/api          # this package's tests and the example's
```
