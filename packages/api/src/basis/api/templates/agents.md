## basis-api

This repo's API runs on basis-api {version}. The API is one `api.Service`, which declares its name, its settings, its
resources and its routes; basis builds the FastAPI app around it, with request ids and JSON logs, OpenTelemetry traces,
RFC 9457 problem details for every error, health and readiness endpoints, and a graceful shutdown, and opens each
resource when the app starts. The service never makes the app itself, and the repo lists it under
`[project.entry-points."basis.apis"]` in its `pyproject.toml`.

Rules to keep:

- Declare each route with `api.get`, `api.post`, `api.put`, `api.patch` or `api.delete`, and a named handler: `def`
  where it calls blocking code, `async def` only where it awaits.
- A handler takes the service's settings as `Annotated[Settings, api.given(Settings)]`, never from the environment.
- A handler reaches the outside only through a resource the service declares, each by name with its deadline:
  `api.database` (a handler asks for an `api.Reader`, or an `api.Writer` where its route writes), `api.http`
  (`api.Client`), `api.cache` (`api.Cache[Value]`), `api.clock` (`api.Clock`) and `api.ids` (`api.Ids`), as
  `Annotated[api.Reader, api.given(WAREHOUSE)]`. It never imports a database driver or an HTTP client, or reads the
  time or makes an id itself. A handler that asks for a database, a service or a cache is `async def`.
- A route's statements share one transaction, which commits after the handler returns and its response validates, and
  rolls back when either fails. A route writes one database at most, and a GET none.
- A handler fails by raising a subclass of `api.Error` that declares its `status` and `title`, and the caller gets a
  problem details response. Any other exception is a bare 500, logged once with its traceback.
- Settings are `BASIS_*` variables, read once at startup; one that is missing stops the API with one `ERROR` line.
- A test builds the app with `api.build(service, settings=Settings.given(...), fakes={...})`, a fake from
  `basis.api.fakes` for each resource, and calls it with FastAPI's `TestClient`, so the developer's own variables and
  the outside world never change what it checks.

Commands, all through `basis`, whose `--help` lists every verb this repo has:

```sh
uv run basis serve [SERVICE]    # serve the API until SIGTERM, which drains it first; a second signal stops it at once
```
