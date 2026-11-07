import asyncio
import contextlib
import threading
from collections.abc import Awaitable, Callable, Generator, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated, override
from uuid import UUID

import httpx2
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NoOpTracer
from pydantic import BaseModel, ConfigDict

from basis import api
from basis.api import fakes
from basis.api.app import Stage
from basis.api.opened import MemoryCache
from basis.core.settings import SettingsError


class Price(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    security: str
    price: Decimal


class Refused(api.Error):
    status = HTTPStatus.CONFLICT
    title = "The price is refused"


WAREHOUSE = api.database("warehouse", deadline_s=1)
QUICK = api.database("quick", deadline_s=0.05)
PRICING = api.http("pricing", deadline_s=0.5)
PRICES = api.cache("prices", Price, ttl_s=60, max_entries=2, deadline_s=1)
CLOCK = api.clock("clock")
IDS = api.ids("ids")
SECURITY = "US912810TM64"
SELECT = "SELECT security, price FROM prices WHERE security = %(security)s"
INSERT = "INSERT INTO prices (security, price) VALUES (%(security)s, %(price)s)"
SLOW = "SELECT security, price FROM prices WITH (HOLDLOCK)"
AT = datetime(2026, 11, 6, 14, 30, tzinfo=UTC)
PRICE = {"security": SECURITY, "price": "101.25"}


async def read_price(security: str, warehouse: Annotated[api.Reader, api.given(WAREHOUSE)]) -> Price:
    found = await warehouse.fetch_one(Price, SELECT, security=security)
    if found is None:
        raise HTTPException(HTTPStatus.NOT_FOUND)
    return found


async def set_price(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)]) -> Price:
    await warehouse.execute(INSERT, security=price.security, price=price.price)
    return price


async def set_then_refuse(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)]) -> Price:
    await warehouse.execute(INSERT, security=price.security, price=price.price)
    msg = f"{price.security} is refused after all"
    raise Refused(msg)


async def set_then_break(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)]) -> Price:
    await warehouse.execute(INSERT, security=price.security, price=price.price)
    msg = "a defect after the write"
    raise RuntimeError(msg)


async def set_then_misanswer(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)]) -> Price:
    await warehouse.execute(INSERT, security=price.security, price=price.price)
    return {"security": price.security, "price": "not a number"}  # ty: ignore[invalid-return-type] -- a response that does not validate, on purpose


async def slow_read(quick: Annotated[api.Reader, api.given(QUICK)]) -> list[Price]:
    return await quick.fetch(Price, SLOW)


async def set_then_wait(price: Price, quick: Annotated[api.Writer, api.given(QUICK)]) -> Price:
    """Writes, then carries on past a statement that missed its deadline."""
    await quick.execute(INSERT, security=price.security, price=price.price)
    with contextlib.suppress(api.DeadlineExceededError):
        await quick.fetch(Price, SLOW)
    return price


async def quote(security: str, pricing: Annotated[api.Client, api.given(PRICING)]) -> Price:
    return await pricing.get(f"/prices/{security}", Price)


async def cached(security: str, prices: Annotated[api.Cache[Price], api.given(PRICES)], pricing: Annotated[api.Client, api.given(PRICING)]) -> Price:
    return await prices.get(security, lambda: pricing.get(f"/prices/{security}", Price))


def stamp(clock: Annotated[api.Clock, api.given(CLOCK)], ids: Annotated[api.Ids, api.given(IDS)]) -> dict[str, str]:
    return {"at": clock.now().isoformat(), "id": str(ids.new()), "next": str(ids.new())}


SERVICE = api.Service(
    name="prices",
    settings=api.Settings,
    resources=[WAREHOUSE, QUICK, PRICING, PRICES, CLOCK, IDS],
    routes=[
        api.get("/prices/{security}", read_price),
        api.post("/prices", set_price),
        api.post("/refused", set_then_refuse),
        api.post("/broken", set_then_break),
        api.post("/misanswered", set_then_misanswer),
        api.get("/slow", slow_read),
        api.post("/stubborn", set_then_wait),
        api.get("/quotes/{security}", quote),
        api.get("/cached/{security}", cached),
        api.get("/stamp", stamp),
    ],
)


def _pricing(*, status: int = HTTPStatus.OK, body: object = None, wait_s: float = 0, seen: list[Request] | None = None) -> FastAPI:
    pricing = FastAPI()

    @pricing.get("/prices/{security}")
    async def price(security: str, request: Request) -> object:
        if seen is not None:
            seen.append(request)
        await asyncio.sleep(wait_s)
        if status != HTTPStatus.OK:
            raise HTTPException(status)
        return body if body is not None else {"security": security, "price": "99.50"}

    return pricing


def _app(warehouse: fakes.Database | None = None, *, quick: fakes.Database | None = None, pricing: FastAPI | None = None, **settings: object) -> api.App:
    given: dict[api.Resource, fakes.Fake] = {WAREHOUSE: warehouse or fakes.Database(), QUICK: quick or fakes.Database(), PRICES: fakes.Cache(), CLOCK: fakes.Clock(at=AT), IDS: fakes.Ids()}
    if pricing is not None or "http" not in settings:
        given[PRICING] = fakes.Http(pricing or _pricing())
    return api.build(SERVICE, settings=api.Settings.given(**settings), fakes=given)


def _written() -> fakes.Database:
    return fakes.Database(answers={INSERT: 1, SELECT: [{"security": SECURITY, "price": Decimal("101.25")}]})


def _statement() -> fakes.Statement:
    return fakes.Statement(INSERT, {"security": SECURITY, "price": Decimal("101.25")})


def _spans(app: api.App) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    app.provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


def _named(spans: tuple[ReadableSpan, ...], name: str) -> ReadableSpan:
    [found] = [span for span in spans if span.name == name]
    return found


# Declarations


def _service(*routes: api.Route, resources: list[object] | None = None) -> Callable[[], api.Service]:
    return lambda: api.Service(name="prices", settings=api.Settings, resources=resources if resources is not None else [WAREHOUSE, QUICK, PRICES], routes=list(routes))  # ty: ignore[invalid-argument-type] -- some cases declare a wrong resource on purpose


async def _as_writer_in_a_get(warehouse: Annotated[api.Writer, api.given(WAREHOUSE)]) -> None: ...
async def _as_a_client(warehouse: Annotated[api.Client, api.given(WAREHOUSE)]) -> None: ...
async def _as_another_cache(prices: Annotated[api.Cache[str], api.given(PRICES)]) -> None: ...
def _sync(warehouse: Annotated[api.Reader, api.given(WAREHOUSE)]) -> None: ...
async def _twice(first: Annotated[api.Reader, api.given(WAREHOUSE)], second: Annotated[api.Reader, api.given(WAREHOUSE)]) -> None: ...
async def _two_writes(first: Annotated[api.Writer, api.given(WAREHOUSE)], second: Annotated[api.Writer, api.given(QUICK)]) -> None: ...
async def _undeclared(pricing: Annotated[api.Client, api.given(PRICING)]) -> None: ...


class Mutable(BaseModel):
    price: Decimal


@pytest.mark.parametrize(
    ("declare", "message"),
    [
        (lambda: api.database("Warehouse", deadline_s=1), "lower-case letters, digits and underscores"),
        (lambda: api.http("pricing-service", deadline_s=1), "lower-case letters, digits and underscores"),
        (lambda: api.database("warehouse", deadline_s=0), "give it deadline_s, the seconds each call may take, above 0"),
        (lambda: api.http("pricing", deadline_s=float("nan")), "give it deadline_s"),
        (lambda: api.cache("prices", Price, ttl_s=0, max_entries=1, deadline_s=1), "give it ttl_s"),
        (lambda: api.cache("prices", Price, ttl_s=1, max_entries=0, deadline_s=1), "give it max_entries of 1 or more"),
        (lambda: api.cache("prices", Mutable, ttl_s=1, max_entries=1, deadline_s=1), "holds Mutable, which can change"),
        (lambda: api.get("/w", _as_writer_in_a_get), "a GET only reads"),
        (lambda: api.post("/w", _as_a_client), r"asks for warehouse, a database: annotate it Annotated\[api.Reader"),
        (lambda: api.post("/w", _as_another_cache), r"a cache of Price: annotate it Annotated\[api.Cache\[Price\]"),
        (lambda: api.get("/w", _sync), "whose calls are awaited: declare it `async def _sync`"),
        (lambda: api.get("/w", _twice), "asks for warehouse twice"),
        (lambda: api.post("/w", _two_writes), "writes warehouse and quick, and a request commits one transaction"),
        (_service(api.get("/w", _undeclared)), "asks for pricing, which service prices does not declare"),
        (_service(api.get("/p/{security}", read_price), resources=[WAREHOUSE, api.database("warehouse", deadline_s=2)]), "declares warehouse twice"),
        (_service(api.get("/p/{security}", read_price), resources=[WAREHOUSE, "pricing"]), "has a str among its resources"),
        (lambda: api.given("warehouse"), "or one of its resources"),  # ty: ignore[invalid-argument-type] -- wrong on purpose
    ],
    ids=[
        "name", "hyphen", "no-deadline", "nan-deadline", "no-ttl", "no-entries", "mutable-value", "writes-in-a-get", "wrong-kind",
        "another-cache", "sync-handler", "asked-twice", "two-written", "undeclared", "same-name", "not-a-resource", "given-a-name",
    ],
)  # fmt: skip
def test_resources_cannot_be_declared_wrong(declare: Callable[[], object], message: str) -> None:
    with pytest.raises(api.ServiceError, match=message):
        declare()


def test_a_route_knows_the_database_it_writes() -> None:
    assert [(route.name, route.writes) for route in SERVICE.routes[:3]] == [("read_price", None), ("set_price", WAREHOUSE), ("set_then_refuse", WAREHOUSE)]


def test_every_resource_without_a_fake_needs_its_settings() -> None:
    with pytest.raises(SettingsError) as raised:
        api.build(SERVICE, settings=api.Settings.given(), fakes={QUICK: fakes.Database()})
    assert str(raised.value) == (
        "service prices's resources are not all configured, so the API cannot start: set BASIS_DATABASES__WAREHOUSE__KIND and the rest of its block, for the database"
        " warehouse; BASIS_HTTP__PRICING__URL, the base URL of the service pricing"
    )


def test_a_database_a_route_writes_needs_its_write_login() -> None:
    warehouse = {"kind": "mssql", "connection_string": "Server=nowhere", "isolation": "snapshot", "timeout_s": 5, "max_rows": 100}
    with pytest.raises(SettingsError, match="set BASIS_DATABASES__WAREHOUSE__WRITE__CONNECTION_STRING, the login through which routes write warehouse"):
        api.build(SERVICE, settings=api.Settings.given(databases={"warehouse": warehouse}), fakes={QUICK: fakes.Database(), PRICING: fakes.Http(_pricing())})


@pytest.mark.parametrize(
    ("given", "message"),
    [
        ({api.database("elsewhere", deadline_s=1): fakes.Database()}, "a fake is given for elsewhere, which service prices does not declare"),
        ({WAREHOUSE: fakes.Clock(at=AT)}, "warehouse is given a fakes.Clock"),
    ],
    ids=["undeclared", "wrong-kind"],
)
def test_a_fake_fakes_a_declared_resource_of_its_kind(given: dict[api.Resource, fakes.Fake], message: str) -> None:
    with pytest.raises(api.ServiceError, match=message):
        api.build(SERVICE, settings=api.Settings.given(), fakes=given)


# A request's transaction


def test_a_write_commits_once_the_response_validates() -> None:
    warehouse = _written()
    with TestClient(_app(warehouse).asgi) as client:
        response = client.post("/prices", json=PRICE)
    assert (response.status_code, response.json()) == (HTTPStatus.OK, PRICE)
    assert (warehouse.committed, warehouse.rolled_back) == ([_statement()], [])


@pytest.mark.parametrize(
    ("path", "status", "kind"),
    [("/refused", HTTPStatus.CONFLICT, "refused"), ("/broken", HTTPStatus.INTERNAL_SERVER_ERROR, "about:blank"), ("/misanswered", HTTPStatus.INTERNAL_SERVER_ERROR, "about:blank")],
    ids=["declared-error", "unexpected-error", "response-does-not-validate"],
)
def test_a_write_rolls_back_when_the_request_fails(path: str, status: int, kind: str) -> None:
    warehouse = _written()
    with TestClient(_app(warehouse).asgi) as client:
        response = client.post(path, json=PRICE)
    assert (response.status_code, response.json()["type"]) == (status, kind)
    assert (warehouse.committed, warehouse.rolled_back) == ([], [_statement()])


def test_a_route_that_reads_never_commits() -> None:
    warehouse = _written()
    with TestClient(_app(warehouse).asgi) as client:
        assert client.get(f"/prices/{SECURITY}").json() == PRICE  # the fake refuses a commit of a transaction that only reads
    assert (warehouse.committed, warehouse.rolled_back) == ([], [])


def test_rows_that_do_not_fit_the_model_are_a_defect() -> None:
    warehouse = fakes.Database(answers={SELECT: [{"security": SECURITY, "price": "a lot"}]})
    with TestClient(_app(warehouse).asgi) as client:
        assert client.get(f"/prices/{SECURITY}").status_code == HTTPStatus.INTERNAL_SERVER_ERROR


# Deadlines


def _held(release: threading.Event) -> fakes.Respond:
    """An answer that waits until the test releases it, as a statement waits on a lock."""

    def answer(_: Mapping[str, api.Bind]) -> list[Mapping[str, object]]:
        release.wait(5)
        return []

    return fakes.Respond(answer)


def test_a_statement_past_its_deadline_is_a_504() -> None:
    release = threading.Event()
    with TestClient(_app(quick=fakes.Database(answers={SLOW: _held(release)})).asgi) as client:
        response = client.get("/slow")
        release.set()
    assert (response.status_code, response.json()["type"], response.json()["detail"]) == (HTTPStatus.GATEWAY_TIMEOUT, "deadline-exceeded", "quick did not answer within its deadline of 0.05s")


def test_a_write_never_commits_after_a_statement_misses_its_deadline() -> None:
    """The handler carries on past the deadline, and the statement goes on to finish: the request still fails, and
    rolls back once the statement ends."""
    release = threading.Event()
    quick = fakes.Database(answers={INSERT: 1, SLOW: _held(release)})
    with TestClient(_app(quick=quick).asgi) as client:
        response = client.post("/stubborn", json=PRICE)
        release.set()
    assert (response.status_code, response.json()["type"]) == (HTTPStatus.GATEWAY_TIMEOUT, "deadline-exceeded")
    assert (quick.committed, quick.rolled_back) == ([], [_statement()])


def test_a_call_to_another_service_past_its_deadline_is_a_504() -> None:
    with TestClient(_app(pricing=_pricing(wait_s=5)).asgi) as client:
        response = client.get(f"/quotes/{SECURITY}")
    assert (response.status_code, response.json()["detail"]) == (HTTPStatus.GATEWAY_TIMEOUT, "pricing did not answer within its deadline of 0.5s")


def test_a_cache_read_past_its_deadline_is_a_504() -> None:
    async def main() -> None:
        cache = MemoryCache(api.cache("prices", Price, ttl_s=60, max_entries=2, deadline_s=0.05), fakes.Cache().monotonic, NoOpTracer())
        await cache.get(SECURITY, _never)

    with pytest.raises(api.DeadlineExceededError, match=r"prices did not answer within its deadline of 0\.05s"):
        asyncio.run(main())


async def _never() -> Price:
    await asyncio.Event().wait()
    raise AssertionError


# Spans


def test_every_call_through_a_resource_has_a_span_in_the_requests_trace() -> None:
    seen: list[Request] = []
    app = _app(_written(), pricing=_pricing(seen=seen))
    exporter = _spans(app)
    with TestClient(app.asgi) as client:
        client.post("/prices", json=PRICE).raise_for_status()
        client.get(f"/cached/{SECURITY}").raise_for_status()
        client.get("/stamp").raise_for_status()
    spans = exporter.get_finished_spans()
    execute, commit = _named(spans, "warehouse execute"), _named(spans, "warehouse commit")
    assert dict(execute.attributes or {}) == {"db.system.name": "fake", "db.query.text": INSERT, "basis.resource": "warehouse", "basis.deadline_s": 1}
    called, cache = _named(spans, "pricing GET"), _named(spans, "prices get")
    assert {"http.request.method": "GET", "url.path": f"/prices/{SECURITY}", "http.response.status_code": 200}.items() <= dict(called.attributes or {}).items()
    assert dict(cache.attributes or {}) == {"basis.resource": "prices", "basis.deadline_s": 1, "basis.cache.hit": False}
    assert dict(_named(spans, "clock now").attributes or {})["basis.value"] == "2026-11-06T14:30:00+00:00"
    servers = {span.context.trace_id: span for span in spans if span.name.startswith(("POST", "GET"))}
    for span in (execute, commit, called, cache):
        assert span.parent is not None
        assert span.context.trace_id in servers
    assert called.parent is not None
    assert called.parent.span_id == cache.context.span_id  # the load runs inside the cache's read
    [request] = seen
    trace_id, parent = request.headers["traceparent"].split("-")[1:3]
    assert (int(trace_id, 16), int(parent, 16)) == (called.context.trace_id, called.context.span_id)


# Other services


@pytest.mark.parametrize(
    ("pricing", "detail"),
    [
        (_pricing(status=HTTPStatus.SERVICE_UNAVAILABLE), f"pricing answered 503 to GET /prices/{SECURITY}"),
        (_pricing(body={"security": SECURITY}), f"pricing answered GET /prices/{SECURITY} with a body that is not a Price"),
    ],
    ids=["error-status", "body-does-not-fit"],
)
def test_another_services_failure_is_a_502(pricing: FastAPI, detail: str) -> None:
    with TestClient(_app(pricing=pricing).asgi) as client:
        response = client.get(f"/quotes/{SECURITY}")
    assert (response.status_code, response.json()["type"], response.json()["detail"]) == (HTTPStatus.BAD_GATEWAY, "call-failed", detail)


def test_a_service_that_cannot_be_reached_is_a_502() -> None:
    with TestClient(_app(http={"pricing": {"url": "http://127.0.0.1:9"}}).asgi) as client:  # the discard port, which nothing serves here
        response = client.get(f"/quotes/{SECURITY}")
    assert (response.status_code, response.json()["detail"]) == (HTTPStatus.BAD_GATEWAY, f"pricing could not be reached for GET /prices/{SECURITY}: ConnectError")


# The cache


def _counting(made: list[str], *, fails: bool = False) -> Callable[[str], Callable[[], Awaitable[Price]]]:
    """A loader of each security's price, which records each load and prices it at the count of loads so far."""

    def loader(security: str) -> Callable[[], Awaitable[Price]]:
        async def load() -> Price:
            made.append(security)
            await asyncio.sleep(0)
            if fails:
                msg = "the pricing service is down"
                raise RuntimeError(msg)
            return Price(security=security, price=Decimal(len(made)))

        return load

    return loader


def test_a_value_is_kept_until_its_time_to_live_passes() -> None:
    made: list[str] = []
    clock = fakes.Cache()
    load = _counting(made)

    async def main() -> list[Decimal]:
        cache = MemoryCache(PRICES, clock.monotonic, NoOpTracer())
        first = await cache.get("A", load("A"))
        clock.advance(59.9)
        kept = await cache.get("A", load("A"))
        clock.advance(0.1)
        return [first.price, kept.price, (await cache.get("A", load("A"))).price]

    assert (asyncio.run(main()), made) == ([1, 1, 2], ["A", "A"])


def test_past_its_entries_the_value_read_longest_ago_goes() -> None:
    made: list[str] = []
    load = _counting(made)

    async def main() -> None:
        cache = MemoryCache(PRICES, fakes.Cache().monotonic, NoOpTracer())
        for security in ("A", "B", "A", "C", "A", "B"):  # C pushes out B, read longer ago than A
            await cache.get(security, load(security))

    asyncio.run(main())
    assert made == ["A", "B", "C", "B"]


def test_requests_that_ask_at_once_share_one_load() -> None:
    made: list[str] = []
    load = _counting(made)

    async def main() -> list[Price]:
        cache = MemoryCache(PRICES, fakes.Cache().monotonic, NoOpTracer())
        return list(await asyncio.gather(*(cache.get("A", load("A")) for _ in range(3))))

    assert (len({id(found) for found in asyncio.run(main())}), made) == (1, ["A"])


def test_a_load_that_fails_fails_every_request_waiting_on_it_and_keeps_nothing() -> None:
    made: list[str] = []

    async def main() -> list[object]:
        cache = MemoryCache(PRICES, fakes.Cache().monotonic, NoOpTracer())
        failed = await asyncio.gather(*(cache.get("A", _counting(made, fails=True)("A")) for _ in range(2)), return_exceptions=True)
        return [*failed, await cache.get("A", _counting(made)("A"))]

    first, second, then = asyncio.run(main())
    assert (str(first), str(second), then, made) == ("the pricing service is down", "the pricing service is down", Price(security="A", price=Decimal(2)), ["A", "A"])


def test_a_load_whose_request_stops_is_taken_up_by_a_request_waiting_on_it() -> None:
    made: list[str] = []

    async def main() -> Price:
        cache = MemoryCache(PRICES, fakes.Cache().monotonic, NoOpTracer())
        stopped = asyncio.ensure_future(cache.get("A", _never))
        await asyncio.sleep(0)
        waiting = asyncio.ensure_future(cache.get("A", _counting(made)("A")))
        await asyncio.sleep(0)
        stopped.cancel()
        return await waiting

    assert (asyncio.run(main()), made) == (Price(security="A", price=Decimal(1)), ["A"])


# The clock and ids


def test_the_clock_and_ids_are_the_fakes_given() -> None:
    with TestClient(_app().asgi) as client:
        stamped = client.get("/stamp").json()
    assert stamped == {"at": "2026-11-06T14:30:00+00:00", "id": "00000000-0000-7000-8000-000000000001", "next": "00000000-0000-7000-8000-000000000002"}


def test_the_systems_clock_is_in_utc_and_its_ids_are_uuid7s() -> None:
    service = api.Service(name="stamps", settings=api.Settings, resources=[CLOCK, IDS], routes=[api.get("/stamp", stamp)])
    with TestClient(api.build(service, settings=api.Settings.given()).asgi) as client:
        stamped = client.get("/stamp").json()
    assert (datetime.fromisoformat(stamped["at"]).utcoffset(), UUID(stamped["id"]).version, stamped["id"] < stamped["next"]) == (timedelta(0), 7, True)


def test_a_fake_clock_moves_only_when_told() -> None:
    clock = fakes.Clock(at=datetime(2026, 11, 6, 9, 30, tzinfo=UTC) + timedelta(hours=5))
    clock.advance(timedelta(minutes=1))
    assert clock.now() == datetime(2026, 11, 6, 14, 31, tzinfo=UTC)


# Opening and closing


class _Opening(fakes.Database):
    """A database whose first connection waits until the test releases it, as one does while a login is slow."""

    def __init__(self, release: threading.Event, entered: threading.Event) -> None:
        super().__init__()
        self.release, self.entered = release, entered

    @override
    @contextlib.contextmanager
    def statements(self, *, writes: bool) -> Generator[fakes.Transaction]:
        self.entered.set()
        self.release.wait(5)
        with super().statements(writes=writes) as transaction:
            yield transaction


def test_not_ready_until_every_resource_is_open() -> None:
    release, entered = threading.Event(), threading.Event()
    app = _app(_Opening(release, entered))

    async def main() -> tuple[int, int]:
        lifespan = app.asgi.router.lifespan_context(app.asgi)
        starting = asyncio.ensure_future(lifespan.__aenter__())
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app.asgi), base_url="http://test") as client:
            await asyncio.to_thread(entered.wait, 5)
            opening = (await client.get("/health/ready")).status_code
            release.set()
            await starting
            opened = (await client.get("/health/ready")).status_code
        await lifespan.__aexit__(None, None, None)
        return opening, opened

    assert asyncio.run(main()) == (HTTPStatus.SERVICE_UNAVAILABLE, HTTPStatus.OK)


def test_a_database_that_does_not_answer_in_time_stops_the_app_at_startup() -> None:
    release, entered = threading.Event(), threading.Event()
    app = _app(quick=_Opening(release, entered))

    async def main() -> None:
        try:
            await app.asgi.router.lifespan_context(app.asgi).__aenter__()
        finally:
            release.set()  # the login the app stopped waiting for gives up, as a driver's login timeout does

    with pytest.raises(api.DeadlineExceededError, match="quick did not answer within its deadline"):
        asyncio.run(main())
    assert app.lifecycle.stage is Stage.STARTING


def test_each_given_resource_answers_only_its_own_requests() -> None:
    kept: list[api.Reader] = []

    async def keep(warehouse: Annotated[api.Reader, api.given(WAREHOUSE)]) -> dict[str, str]:
        kept.append(warehouse)
        return {}

    service = api.Service(name="keeps", settings=api.Settings, resources=[WAREHOUSE], routes=[api.get("/keep", keep)])
    with TestClient(api.build(service, settings=api.Settings.given(), fakes={WAREHOUSE: _written()}).asgi) as client:
        client.get("/keep").raise_for_status()

    with pytest.raises(RuntimeError, match="has ended"):
        asyncio.run(kept[0].fetch(Price, SELECT, security=SECURITY))
