# How-to guides

Each guide is one task in an API repo. Paths are the repo's package, as `ledger/handlers.py`; after each, run
`uv run basis sync` and `uv run basis check`, and commit what they wrote with the code.

## Add a route that reads

1. The rule and the response's type go in `domain.py`, plain pydantic models and functions that import nothing of
   basis.
2. The SQL, or the call to another service, goes in a function of `repository.py` that takes the resource and returns
   domain types, raising a domain exception for what the domain gives a meaning, as `UnknownBond`.
3. The handler goes in `handlers.py`: it takes the request's parameters and each resource as
   `Annotated[api.Reader, api.given(WAREHOUSE)]`, passes them on to the repository, and applies the rule.
4. The route goes in `service.py`: `api.get(PATH, handler, deadline_s=..., response=..., errors=[...])`, listing each
   error its handler raises and each one `errors.py` gives a domain exception it may meet.
5. Test the rule in `tests/test_domain.py` on plain values, and the route in `tests/test_handlers.py` on fakes,
   each declared error among them.

The `api-add-route` skill walks an agent through the same steps.

## Add a route that writes

Declare it with `api.post`, `api.put`, `api.patch` or `api.delete`, and a handler that asks for an `api.Writer` of the
one database it writes. Its statements share one transaction, which commits after the handler returns and its
response validates, and rolls back on any failure. A route that is not a GET asks for the user,
`Annotated[User | None, api.given(User)]` in a service that declares `user=`, or declares `anonymous=True`.

A call that changes another service's state goes through `send`, with the key the other service drops a duplicate by:

```python
await ledger.send("POST", "/bookings", body, key=str(settlement.settlement_id))
```

In a route that writes, the call waits in the outbox of the database it writes, declared as
`api.database("warehouse", ..., outbox=api.outbox("accruals_outbox", max_age_s=86_400, backoff_s=60, max_backoff_s=3_600))`,
and is made once the writes commit; `basis relay`, on a schedule, makes it again until it is accepted. Print the
outbox's table with:

```sh
uv run basis ddl --dialect mssql
```

## Answer a table for a grid

A route of many rows answers Arrow to a UI that asks for `application/vnd.apache.arrow.stream`, and JSON otherwise.
Its row model names how a UI shows each number, and lives in `handlers.py`, beside the handler that answers it: a
display format is how the API presents a number, not a rule of the domain, and `domain.py` imports nothing of basis.

```python
class PositionRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: str
    security_id: str
    face: Annotated[float, api.format.money("USD")]
    weight: Annotated[float, api.format.pct(2)]
```

Every number is a `float`; the formats are `api.format.number`, `money`, `pct`, `bps`, `price` and `price32`. The route
declares `response=api.Table[PositionRow]`, and its handler returns a pandas frame or an Arrow table of those columns,
which basis checks once, never row by row. Where the rows come straight from SQL, the repository reads them with
`fetch_table`, and takes the row model from the handler, since a repository imports no handlers module:

```python
async def positions(warehouse: api.Reader, row: type[BaseModel], account: str) -> pa.Table:
    return await warehouse.fetch_table(row, POSITIONS, account=account)
```

and the handler calls `await repository.positions(warehouse, PositionRow, account)`. A route of a few rows may
instead declare `dataset=True` and answer `list[Row]`.

## Call another service

Declare it in `resources.py`, as `PRICING = api.http("pricing", deadline_s=2)`, and set its URL, and where it needs
them its CA bundle and proxy, as `BASIS_HTTP__PRICING__URL`, `__CA_BUNDLE` and `__PROXY`. In the repository, a path
is a template whose values basis encodes:

```python
await pricing.get("/prices/{security}", CleanPrice, segments={"security": security})
```

The reply's model ignores fields it does not declare (`extra="ignore"`), so the other service can add some. Each
failure is an `api.ServiceError` that answers its own problem; catch one only where it means something to the
domain, as `api.NotFound` becoming `Unpriced`. Fake the service in a test with `fakes.Http(app)`, a FastAPI app
of what it answers, and make it fail with `pricing.fails("/prices/X", api.Unavailable)`.

## Refuse a request, naming the field

Raise a subclass of `api.Error`, declared in `errors.py` with its `status` and `title`, and listed in the route's
`errors`. A 422 names each field it refuses, so a UI puts the message on it:

```python
raise PeriodReversed(msg, errors=[api.ParameterError(detail=f"is before the start, {start}", parameter="end")])
```

or `api.PointerError(detail=..., pointer=api.pointer("legs", 0, "rate"))` for a field of the body.

## Give a route a smoke example

A GET that reads nothing a deployment lacks gives `smoke=`, a read-only request as `basis call` takes one:

```python
api.get("/conventions", handlers.conventions, deadline_s=1, response=list[DayCount], errors=[], smoke={"query": {}})
```

After a release, this sends each example to the deployed API and validates its answer:

```sh
uv run basis smoke --env qa
```

## Take a request a UI may send twice

A route that places something, an order or a settlement, keeps its requests' `Idempotency-Key`s, so a UI that lost an
answer sends the request again and changes nothing more. Declare a table for the keys on the database it writes,
create it with `uv run basis ddl --dialect mssql`, and say whether the route requires a key:

```python
WAREHOUSE = api.database("warehouse", deadline_s=5, max_concurrent=8, idempotency=api.idempotency("accruals_requests", keep_s=86_400))
api.post("/settlements", handlers.record_settlement, status=HTTPStatus.CREATED, deadline_s=5, response=Settlement, errors=[...], idempotency="required")
```

The handler is unchanged. A test sends the request twice with one `Idempotency-Key` header, and finds it written once.

## Stream live updates to a UI

A page that shows a value as it changes opens a stream. Its handler is an ordinary GET's, which returns the value now;
basis calls it again every `every_s` seconds, each call within `deadline_s`, and sends the value as an event whenever
it changes:

```python
api.stream("/bonds/{security}/quote", handlers.quote, deadline_s=5, every_s=2, response=Quote, errors=[UNPRICED])
```

Test the handler as any other, on fakes. `basis call quote '{"path": {"security": "US912810TM64"}}' --fake` prints the
stream's first event, and a test that reads the stream itself ends it sooner with `Settings.given(serve={"stream_s":
0.05})`.

## Change what a UI relies on

Add beside it; never change it in place. `basis check --full`, which the push hook runs, fails on a change to
`openapi.json` that breaks a UI built against `openapi.released.json`: a response field removed, renamed or retyped, a
request field removed or newly required, an error that answers another status, or an operation removed or renamed.
A handler's name is its operation's id, so renaming a handler renames the function a UI calls. In the commit a
release tags:

```sh
uv run basis release-document v0.2.0
```
