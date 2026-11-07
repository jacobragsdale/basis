## basis-api

This repo's API runs on basis-api {version}. The API is one `api.Service`, which declares its name, its settings, its
resources and its routes; basis builds the FastAPI app around it, with request ids and JSON logs, OpenTelemetry traces,
RFC 9457 problem details for every error, health and readiness endpoints, and a graceful shutdown, and opens each
resource when the app starts. The service never makes the app itself, and the repo lists it under
`[project.entry-points."basis.apis"]` in its `pyproject.toml`.

Rules to keep:

- Declare each route with `api.get`, `api.post`, `api.put`, `api.patch` or `api.delete`, and a named handler: `def`
  where it calls blocking code, `async def` only where it awaits. Every route declares `deadline_s`, the seconds it has
  to answer, which caps every call it makes; `response`, which is its handler's return annotation; and `errors`, each
  `api.Error` its handler raises, or `[]`. An error it does not declare, or an `HTTPException`, answers 500.
- A route reads its request strictly: a key it does not declare is refused, a JSON body's values have their own JSON
  types, a decimal is a string (`"0.045"`, never `0.045`), a time has its offset, and query and path text reads by its
  type's grammar (`10`, `0.045`, `true`, `2026-11-06`). Declare query parameters one by one, or as one model with
  `Query()`. A request model reads each field by one key, a plain `alias` or its name, and a body holds tuples or lists
  of models, never sets of them. A route takes no `BackgroundTasks`: its work ends with its response.
- A handler takes the service's settings as `Annotated[Settings, api.given(Settings)]`, never from the environment.
- A handler reaches the outside only through a resource the service declares, each by name with its deadline:
  `api.database` (a handler asks for an `api.Reader`, or an `api.Writer` where its route writes; its `max_concurrent`
  bounds the statements that run at once), `api.http` (`api.Client`), `api.cache` (`api.Cache[Value]`, of a frozen
  model), `api.clock` (`api.Clock`) and `api.ids` (`api.Ids`), as `Annotated[api.Reader, api.given(WAREHOUSE)]`, in
  the handler's own parameters, never a dependency's. It never imports a database driver or an HTTP client, or reads
  the time or makes an id itself. A handler that asks for a database, a service or a cache is `async def`.
- CPU-bound work, such as a solver, goes in a module named `compute`: plain functions that take values and return one,
  importing nothing of basis. Declare each with `api.compute(function)`, and ask for it as
  `Annotated[api.Compute[[Params...], Result], api.given(SOLVE)]`; awaiting it runs it in the process pool, sized by
  `BASIS_COMPUTE__WORKERS`, within the route's deadline. A script that calls `api.serve` itself guards it with
  `if __name__ == "__main__":`, since each process of the pool imports the script that started it.
- A route's statements share one transaction, which commits after the handler returns and its response validates, and
  rolls back when either fails, or when a statement fails or misses its deadline, even if the handler carries on. A
  route writes one database at most, and a GET none.
- A call that changes another service's state goes through `send`, as
  `await ledger.send("POST", "/bookings", body, key=str(settlement.settlement_id))`, with an idempotency key the other
  service drops a duplicate by. In a route that writes, it waits in the outbox of the database the route writes,
  declared as `api.database(..., outbox=api.outbox(TABLE, max_age_s=..., backoff_s=..., max_backoff_s=...))`, and is
  made only once the writes commit; if it fails then, the request still answers as it would, and `basis relay`, which
  the repo's scheduler runs, makes it again until it is accepted or ages out. A 4xx but 408, 409, 425 or 429 refuses
  it for good, and fails the next relay, as an aged-out call does. Never make such a call with `post` in a route that
  writes: one made before the commit cannot be rolled back. `basis ddl` prints the outbox's table.
- Another service's URL, CA bundle and proxy are its settings, `BASIS_HTTP__NAME__URL`, `__CA_BUNDLE` and `__PROXY`;
  basis reads no `SSL_CERT_FILE` or `HTTP_PROXY`.
- A service whose callers sign in declares `user=User`, its subclass of `api.User` with a field for each claim it
  needs beside the subject, and a handler asks for `Annotated[User | None, api.given(User)]`: None is an anonymous
  request, and a route that needs a user raises `api.SignInRequiredError`, which basis adds to the errors of every
  route that asks for the user, so it is never declared. basis verifies every request's bearer token from the
  `BASIS_IDENTITY__*` settings; a handler never reads the `Authorization` header or decodes a token itself.
- A handler fails by raising a subclass of `api.Error` that declares its `status` and `title`, and the caller gets a
  problem details response. Any other exception is a bare 500, logged once with its traceback. A 422 names the fields
  it refuses, so a UI puts each error on its field: `raise PeriodReversed(msg, errors=[api.ParameterError(detail=...,
  parameter="end")])` for a query or path parameter, or `api.PointerError(detail=..., pointer=api.pointer("legs", 0,
  "rate"))` for a field of the body.
- The repo commits `openapi.json`, its API's OpenAPI document, which UIs generate their clients from: `basis sync`
  writes it from the service, and `basis check`, which the commit hook runs, fails until it matches, so run
  `uv run basis sync` after changing a route or a model, and commit what it writes. Each operation's id is its
  handler's name, so renaming a handler renames the function a UI calls.
- `[tool.basis.api] releases` in `pyproject.toml` is the glob of the repo's release tags, as `"v*"`. `basis check
  --full`, which the push hook runs, fails on a change to `openapi.json` that breaks a UI built against the last
  release: a field a response gives removed, renamed or retyped, a field a request takes removed or newly required,
  or an operation removed or renamed. Add beside what a UI relies on; never change it in place. The check reads the
  repo's release tags, so a CI checkout of the repo fetches its whole history and its tags.
- Every time a response gives carries its offset, as the clock's do: one without is a 500.
- A route that answers rows, as a grid shows them, declares `dataset=True` and answers `list[Row]`, each field of `Row`
  a scalar, a decimal with its `Field(max_digits=..., decimal_places=...)`; it answers Arrow to a request that asks for
  `application/vnd.apache.arrow.stream`, and JSON otherwise.
- A UI calls the API from its own host, under `/api`, which the ingress removes, so no route's path starts with it.
  Where a UI's pages are on another origin, the settings name it, as `BASIS_ALLOWED_ORIGINS='["https://ui.example"]'`;
  without it, no other origin's page may call the API.
- Settings are `BASIS_*` variables, read once at startup; one that is missing stops the API with one `ERROR` line.
- A test builds the app with `api.build(service, settings=Settings.given(...), fakes={...})`, a fake from
  `basis.api.fakes` for each resource, and `fakes.Compute()` for a compute function to run in the test's process, and
  calls it with FastAPI's `TestClient`, so the developer's own variables and the outside world never change what it
  checks. A test of a signed-in route gives the settings `identity={..., "keys": {"kind": "development"}}` and signs
  its token with `basis.api.tokens.sign`.
- The service names its fakes for `basis dev` and `basis call --fake` as `api.Service(..., fakes="module:function")`:
  a function of the repo's own that returns a fake for each database and other service, from synthetic data in it or
  in the repo's fixtures.

Commands, all through `basis`, whose `--help` lists every verb this repo has:

```sh
uv run basis serve [SERVICE]    # serve the API until SIGTERM, which drains it first; a second signal stops it at once
uv run basis dev [SERVICE]      # serve it on its fakes, on 127.0.0.1, until Ctrl+C
uv run basis call ROUTE '{"path": {...}, "query": {...}, "body": ...}' --fake   # one route in-process, its response as JSON; --real for the real resources
uv run basis ddl [SERVICE] --dialect mssql   # the CREATE TABLE of each outbox, for whoever has the rights to run it
uv run basis relay [SERVICE]    # make the calls the outboxes still hold; the scheduler runs it, and it fails when one ages out
```
