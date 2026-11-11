# Reference

What basis-api gives an API repo, by name, as `from basis import api` names it. The rules behind each are in the
repo's `AGENTS.md`, and the reasons in basis's design page.

## The layers

Each layer is a module basis knows by its name, at any depth, so `bonds/handlers.py` is a handlers module too.
`basis check` holds them, each failure one `ERROR` line with its fix.

| Module | Holds | Imports, of the layers |
|---|---|---|
| `service.py` | The `api.Service`: its routes | any below it |
| `handlers.py` | One function per route, the only place resources arrive; and the row models a grid's routes answer, with their display formats | repositories, the declarations, domains |
| `repository.py` | All the SQL and every call to another service, each function taking the resource it needs | the declarations, domains |
| `resources.py`, `errors.py` | The declarations: the settings and the resources; the errors, a domain exception's meaning among them | each other, domains |
| `domain.py` | Pure types and rules, which import nothing of basis, so a job can run them | domains alone |
| `compute.py` | CPU-bound functions, which take values and return one, and import nothing of basis, declared with `api.compute` | (no layer) |

No layer imports a database driver, an HTTP client, storage or the standard library's sockets, processes or
databases, or reads `os.environ`, `datetime.now` or `uuid.uuid4`. A handler passes each resource `api.given` gives it
on to a repository function, and never calls its methods. A module of `tests/` and the repo's jobs are no layer.

## The service and its routes

| Name | What it declares |
|---|---|
| `api.Service(name=, settings=, routes=, resources=, user=, example_user=, fakes=, ui=)` | The API: its settings class, its routes, every resource they reach, the signed-in user's class, the claims `check --full` signs its requests with, and the fakes `basis dev` runs on, as `"module:function"` |
| `api.get`, `api.post`, `api.put`, `api.patch`, `api.delete` | A route: `(PATH, handler, *, deadline_s, response, errors, status=200, dataset=False, smoke=None, anonymous=False)` |
| `deadline_s` | The seconds the route has to answer, which caps every call it makes |
| `response` | Its handler's return annotation: a model, a list of them, or `api.Table[Row]` |
| `errors` | Each `api.Error` the handler raises and each domain error it may meet, or `[]`; any other is a 500 |
| `dataset=True` | A route of a few rows that answers `list[Row]` as Arrow to a UI that asks for it |
| `smoke=` | A read-only example request, `{"path": {...}, "query": {...}}`, that `basis smoke` sends a deployment |
| `anonymous=True` | A route that is not a GET, which anyone may call; every other such route asks for the user |
| `api.Settings` | The settings' base class: the service's own fields beside basis's, read once from `BASIS_*` variables |
| `api.User` | The signed-in user's base class: a field for each claim beyond the subject |
| `api.load` | The service the repo lists under `[project.entry-points."basis.apis"]` |
| `api.build(service, settings=, fakes=)` | The app, as a test builds it, with a fake for each resource |

## Resources

Each is declared once in `resources.py`, by name, with its deadline, and asked for in a handler's parameters as
`Annotated[TYPE, api.given(RESOURCE)]`.

| Declared with | A handler asks for | Settings |
|---|---|---|
| `api.database(name, deadline_s=, max_concurrent=, outbox=None)` | `api.Reader`, or `api.Writer` in a route that writes | `BASIS_DATABASES__NAME__*` |
| `api.outbox(table, max_age_s=, backoff_s=, max_backoff_s=)` | (the outbox of a database a route writes, for `send`) | |
| `api.http(name, deadline_s=)` | `api.Client` | `BASIS_HTTP__NAME__URL`, `__CA_BUNDLE`, `__PROXY` |
| `api.cache(name, Value, ttl_s=, max_entries=, deadline_s=)` | `api.Cache[Value]`, of a frozen model | |
| `api.clock(name)` | `api.Clock` | |
| `api.ids(name)` | `api.Ids` | |
| `api.compute(function)` | `api.Compute[[Params...], Result]`, awaited in the process pool | `BASIS_COMPUTE__WORKERS` |

An `api.Reader` gives `fetch(Row, SQL, **binds)`, `fetch_one(Row, SQL, **binds)` and `fetch_table(Row, SQL, **binds)`,
an Arrow table checked once; an `api.Writer` adds `execute(SQL, **binds)`. Every value is bound. An `api.Client` gives
`get(PATH, Reply, query=, segments=)` and its kin, and `send(METHOD, PATH, body, key=, segments=)`, a call that
changes the other service's state, made after the commit in a route that writes.

## Errors

| Name | What it is |
|---|---|
| `api.Error` | The base of a handler's own errors: a subclass declares `status` and `title` |
| `api.error(Exception, status=, title=)` | The HTTP meaning of a domain exception, declared once in `errors.py`; its type is the exception's name in kebab case |
| `api.ParameterError(detail=, parameter=)` | A query or path parameter a 422 refuses |
| `api.PointerError(detail=, pointer=)` | A field of the body a 422 refuses, at `api.pointer("legs", 0, "rate")` |
| `api.SignInRequiredError` | Raised for a route that needs a user; basis adds it to every route that asks for one |
| `api.NotFound`, `api.Conflict`, `api.Denied`, `api.Rejected`, `api.Unavailable`, `api.Timeout`, `api.BadReply` | How a call to another service fails: each an `api.ServiceError`, which answers its own problem |
| `api.Problem`, `api.ValidationProblem` | The RFC 9457 body every error answers: `type`, `title`, `status`, `detail`, and `instance`, the request's id |

## Tables and display formats

A route of many rows declares `response=api.Table[Row]`, and its handler returns a pandas frame or an Arrow table of
`Row`'s columns. Each field of `Row` is a scalar, every number a `float`, and a number may name how a UI shows it:

| Format | Shows |
|---|---|
| `api.format.number(DIGITS)` | A plain number, to `DIGITS` places |
| `api.format.money(CURRENCY)` | An amount in a currency, as `"USD"` |
| `api.format.pct(DIGITS)` | A fraction as a percentage: 0.045 is 4.5% |
| `api.format.bps(DIGITS)` | A fraction in basis points: 0.0225 is 225 bp |
| `api.format.price(DIGITS)` | A price per 100 of face |
| `api.format.price32()` | A price in 32nds, as a Treasury desk reads one |

`openapi.json` carries each as `x-basis-format`.

## Fakes

`basis.api.fakes` holds a fake of each resource, for tests and `basis dev`: `fakes.Database(answers={SQL: rows})`,
whose answer may be `fakes.Respond(function)` of the binds; `fakes.Http(app)`, a FastAPI app; `fakes.Cache()`,
`fakes.Clock(at=...)`, `fakes.Ids()` and `fakes.Compute()`, which runs a compute function in the test's process. A
fake's `fails(...)` makes the next call fail as the real resource would.

## Commands

| Command | What it does |
|---|---|
| `basis check [--full]` | Lints, type checks and tests the repo, holds its layers, builds the app on its fakes, and checks `openapi.json` matches; `--full` adds Schemathesis requests and the breaking-change check |
| `basis sync` | Writes `openapi.json` from the service, with basis's own files |
| `basis dev [SERVICE]` | Serves the API on its fakes, on `127.0.0.1`, until Ctrl+C |
| `basis call ROUTE [REQUEST] (--fake \| --real) [--env ENV]` | Runs one route in this process, its request as JSON, `-` or `@FILE`, and prints its response; a problem exits 1 with its `ERROR` line |
| `basis serve [SERVICE] [--env ENV]` | Serves the API until SIGTERM, which drains it first |
| `basis relay [SERVICE] [--env ENV]` | Makes the calls the outboxes still hold; a scheduler runs it |
| `basis ddl [SERVICE] --dialect DIALECT` | Prints the CREATE TABLE of each outbox, for `mssql` or `oracle` |
| `basis smoke --env ENV` | Checks a deployment: its document, its readiness, and each route's `smoke=` example |
| `basis release-document VERSION` | Records `openapi.json` as `openapi.released.json`, in the commit a release tags |

`serve`, `call --real`, `relay` and `smoke` act in the environment `--env` names, or `BASIS_ENV`'s, with its
settings from `~/.config/basis/ENV.env`, or in a pod its deployment's.
