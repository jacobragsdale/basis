## basis-api

This repo's API runs on basis-api {version}. The API is one `api.Service`, which declares its name, its settings and
its routes; basis builds the FastAPI app around it, with request ids and JSON logs, OpenTelemetry traces, RFC 9457
problem details for every error, health and readiness endpoints, and a graceful shutdown. The service never makes the
app itself, and the repo lists it under `[project.entry-points."basis.apis"]` in its `pyproject.toml`.

Rules to keep:

- Declare each route with `api.get`, `api.post`, `api.put`, `api.patch` or `api.delete`, and a named handler: `def`
  where it calls blocking code, `async def` only where it awaits.
- A handler takes the service's settings as `Annotated[Settings, api.given(Settings)]`, never from the environment.
- A handler fails by raising a subclass of `api.Error` that declares its `status` and `title`, and the caller gets a
  problem details response. Any other exception is a bare 500, logged once with its traceback.
- Settings are `BASIS_*` variables, read once at startup; one that is missing stops the API with one `ERROR` line.
- A test builds the app with `api.build(service, settings=Settings.given(...))` and calls it with FastAPI's
  `TestClient`, so the developer's own variables never change what it checks.

Commands, all through `basis`, whose `--help` lists every verb this repo has:

```sh
uv run basis serve [SERVICE]    # serve the API until SIGTERM, which drains it first; a second signal stops it at once
```
