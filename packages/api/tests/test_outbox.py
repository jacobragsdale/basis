"""The outbox, on fakes: a call a route that writes sends is made only once its writes commit, with its key as its
`Idempotency-Key`, and its failure then changes nothing the request answers; `basis relay` makes again each call that
is due, with backoff, until its service accepts it or it ages out, which fails the run; and `basis ddl` prints the
outbox's table. The real databases' SQL is the integration tier's (`integration/test_outbox.py`)."""

import contextlib
import json
import os
import re
import sys
import textwrap
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http import HTTPStatus
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from opentelemetry.trace import NoOpTracer
from pydantic import BaseModel, ConfigDict

from basis import api
from basis.api import fakes, outboxes, relay
from basis.api.outboxes import Row
from basis.core import command

type Records = Callable[[], list[dict[str, object]]]  # the `logged` fixture's

OUTBOX = api.outbox("prices_outbox", max_age_s=3600, backoff_s=60, max_backoff_s=600)
WAREHOUSE = api.database("warehouse", deadline_s=1, max_concurrent=4, outbox=OUTBOX)
PLAIN = api.database("plain", deadline_s=1, max_concurrent=4)
LEDGER = api.http("ledger", deadline_s=1)
INSERT = "INSERT INTO prices (security, price) VALUES (%(security)s, %(price)s)"
FAILS = "UPDATE prices SET price = price * 2"
SECURITY = "US912810TM64"
PRICE = {"security": SECURITY, "price": "101.25"}
AT = datetime(2026, 11, 6, 14, 30, tzinfo=UTC)


class Price(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    security: str
    price: Decimal


class Refused(api.Error):
    status = HTTPStatus.CONFLICT
    title = "The price is refused"


async def book(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)], ledger: Annotated[api.Client, api.given(LEDGER)]) -> Price:
    await warehouse.execute(INSERT, security=price.security, price=price.price)
    await ledger.send("POST", "/bookings", price, key=f"price-{price.security}")
    return price


async def book_then_refuse(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)], ledger: Annotated[api.Client, api.given(LEDGER)]) -> Price:
    await book(price, warehouse, ledger)
    msg = f"{price.security} is refused after all"
    raise Refused(msg)


async def book_then_misanswer(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)], ledger: Annotated[api.Client, api.given(LEDGER)]) -> Price:
    await book(price, warehouse, ledger)
    return {"security": price.security, "price": "not a number"}  # ty: ignore[invalid-return-type] -- a response that does not validate, on purpose


async def book_then_carry_on(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)], ledger: Annotated[api.Client, api.given(LEDGER)]) -> Price:
    await book(price, warehouse, ledger)
    with contextlib.suppress(LookupError):  # the fake has no answer for it, as a database's error would be
        await warehouse.execute(FAILS)
    return price


async def book_without_an_outbox(price: Price, plain: Annotated[api.Writer, api.given(PLAIN)], ledger: Annotated[api.Client, api.given(LEDGER)]) -> Price:
    await plain.execute(INSERT, security=price.security, price=price.price)
    await ledger.send("POST", "/bookings", price, key=f"price-{price.security}")
    return price


async def book_directly(price: Price, ledger: Annotated[api.Client, api.given(LEDGER)]) -> Price:
    await ledger.send("POST", "/bookings", price, key=f"price-{price.security}")
    return price


async def book_with_a_bad_key(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)], ledger: Annotated[api.Client, api.given(LEDGER)]) -> Price:
    await warehouse.execute(INSERT, security=price.security, price=price.price)
    await ledger.send("POST", "/bookings", price, key="two words")
    return price


def _post(path: str, handler: Callable[..., object], *errors: type[api.Error]) -> api.Route:
    return api.post(path, handler, deadline_s=10, response=Price, errors=list(errors))  # ty: ignore[invalid-argument-type] -- each handler is a function


SERVICE = api.Service(
    name="prices",
    settings=api.Settings,
    resources=[WAREHOUSE, PLAIN, LEDGER],
    routes=[
        _post("/book", book),
        _post("/refused", book_then_refuse, Refused),
        _post("/misanswered", book_then_misanswer),
        _post("/carried-on", book_then_carry_on),
        _post("/no-outbox", book_without_an_outbox),
        _post("/direct", book_directly),
        _post("/bad-key", book_with_a_bad_key),
    ],
)


class Ledger:
    """The ledger service, as a fake: what it was sent, with the outbox's rows as they stood at each call, and the
    status it answers, which a test changes."""

    def __init__(self, warehouse: fakes.Database, *, status: int = HTTPStatus.CREATED) -> None:
        self.status = status
        self.seen: list[tuple[str | None, object, list[Row]]] = []
        self.app = FastAPI()

        @self.app.post("/bookings")
        async def bookings(request: Request) -> Response:
            self.seen.append((request.headers.get("idempotency-key"), json.loads(await request.body()), warehouse.outbox))
            return Response(status_code=self.status)


def _app(warehouse: fakes.Database, ledger: Ledger) -> api.App:
    return api.build(SERVICE, settings=api.Settings.given(), fakes={WAREHOUSE: warehouse, PLAIN: fakes.Database(answers={INSERT: 1}), LEDGER: fakes.Http(ledger.app)})


def _warehouse(*rows: Row) -> fakes.Database:
    return fakes.Database(answers={INSERT: 1}, outbox=rows)


# Declarations


@pytest.mark.parametrize(
    ("declare", "message"),
    [
        (lambda: api.outbox("prices outbox", max_age_s=1, backoff_s=1, max_backoff_s=1), "an outbox's table is a name, or schema.name"),
        (lambda: api.outbox("a.b.c", max_age_s=1, backoff_s=1, max_backoff_s=1), "an outbox's table is a name, or schema.name"),
        (lambda: api.outbox("prices_outbox", max_age_s=0, backoff_s=1, max_backoff_s=1), "max_age_s is 0: give it the seconds, above 0"),
        (lambda: api.outbox("prices_outbox", max_age_s=1, backoff_s=float("inf"), max_backoff_s=1), "backoff_s is inf"),
        (lambda: api.outbox("prices_outbox", max_age_s=1, backoff_s=10, max_backoff_s=5), "give max_backoff_s at least backoff_s"),
        (lambda: api.database("warehouse", deadline_s=1, max_concurrent=1, outbox="prices_outbox"), r"declare it with api.outbox\(TABLE"),  # ty: ignore[invalid-argument-type] -- wrong on purpose
    ],
    ids=["a-space", "three-parts", "no-age", "endless-backoff", "backoff-past-its-maximum", "a-name-for-an-outbox"],
)
def test_an_outbox_cannot_be_declared_wrong(declare: Callable[[], object], message: str) -> None:
    with pytest.raises(api.ServiceError, match=message):
        declare()


@pytest.mark.parametrize(("attempts", "wait_s"), [(1, 60), (2, 120), (4, 480), (5, 600), (10_000, 600)])
def test_each_failure_doubles_the_wait_up_to_its_maximum(attempts: int, wait_s: float) -> None:
    assert outboxes.backoff(OUTBOX, attempts) == timedelta(seconds=wait_s)


def test_a_call_is_due_again_no_later_than_it_ages_out() -> None:
    row = _row(expires_at=AT + timedelta(seconds=90))
    assert (outboxes.again_at(OUTBOX, row, 1, AT), outboxes.again_at(OUTBOX, row, 2, AT)) == (AT + timedelta(seconds=60), AT + timedelta(seconds=90))


@pytest.mark.parametrize(
    ("path", "key", "message"),
    [
        ("/bookings", "", "is not one to 255 visible ASCII characters"),
        ("/bookings", "two words", "is not one to 255 visible ASCII characters"),
        ("/bookings", "k" * 256, "is not one to 255 visible ASCII characters"),
        ("/bookings", "clé", "is not one to 255 visible ASCII characters"),
        ("bookings", "k", "start it with /"),
        ("/" + "b" * 2000, "k", "of at most 2000 characters"),
    ],
    ids=["empty", "a-space", "too-long", "not-ascii", "no-slash", "path-too-long"],
)
def test_a_call_the_outbox_cannot_hold_is_refused(path: str, key: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        outboxes.checked(path, key)


def test_the_longest_key_is_held() -> None:
    outboxes.checked("/bookings", "k" * 255)


# After the commit


def test_a_call_is_made_after_the_commit_with_its_key_and_its_row_is_done() -> None:
    warehouse = _warehouse()
    ledger = Ledger(warehouse)
    with TestClient(_app(warehouse, ledger).asgi) as client:
        response = client.post("/book", json=PRICE)
    assert (response.status_code, response.json()) == (HTTPStatus.OK, PRICE)
    [(key, body, rows_then)] = ledger.seen
    assert (key, body) == (f"price-{SECURITY}", PRICE)
    assert [(row.status, row.attempts) for row in rows_then] == [("pending", 0)]  # committed before the call was made
    [row] = warehouse.outbox
    assert (row.status, row.attempts, row.next_at, row.route, row.service, row.method, row.path) == ("done", 1, None, "book", "ledger", "POST", "/bookings")
    assert row.ended_at is not None


@pytest.mark.parametrize(
    ("path", "status"),
    [("/refused", HTTPStatus.CONFLICT), ("/misanswered", HTTPStatus.INTERNAL_SERVER_ERROR), ("/carried-on", HTTPStatus.INTERNAL_SERVER_ERROR)],
    ids=["declared-error", "response-does-not-validate", "a-statement-failed"],
)
def test_a_call_whose_writes_roll_back_is_never_made(path: str, status: int) -> None:
    warehouse = _warehouse()
    ledger = Ledger(warehouse)
    with TestClient(_app(warehouse, ledger).asgi) as client:
        assert client.post(path, json=PRICE).status_code == status
    assert (ledger.seen, warehouse.outbox, len(warehouse.rolled_back)) == ([], [], 1)


def test_a_call_the_service_refuses_after_the_commit_changes_nothing_the_request_answers(logged: Records) -> None:
    warehouse = _warehouse()
    ledger = Ledger(warehouse, status=HTTPStatus.SERVICE_UNAVAILABLE)
    with TestClient(_app(warehouse, ledger).asgi) as client:
        response = client.post("/book", json=PRICE)
    assert (response.status_code, response.json()) == (HTTPStatus.OK, PRICE)
    [row] = warehouse.outbox
    assert (row.status, row.attempts, row.last_error) == ("pending", 1, "ledger answered 503 to POST /bookings")
    assert row.next_at is not None
    assert row.next_at - row.created_at > timedelta(seconds=59)  # its backoff, from the attempt
    [warned] = [record for record in logged() if str(record["message"]).startswith("ledger did not accept")]
    assert (warned["level"], warned["idempotency_key"]) == ("warning", f"price-{SECURITY}")


def test_a_route_that_sends_through_a_database_without_an_outbox_is_a_defect(logged: Records) -> None:
    with TestClient(_app(_warehouse(), Ledger(_warehouse())).asgi) as client:
        assert client.post("/no-outbox", json=PRICE).status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    [failed] = [record for record in logged() if record["level"] == "error"]
    assert "book_without_an_outbox sends POST /bookings to ledger, and writes plain, which declares no outbox" in str(failed["error"])


def test_a_call_with_a_key_the_outbox_cannot_hold_is_a_defect_and_rolls_back() -> None:
    warehouse = _warehouse()
    ledger = Ledger(warehouse)
    with TestClient(_app(warehouse, ledger).asgi) as client:
        assert client.post("/bad-key", json=PRICE).status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert (ledger.seen, warehouse.outbox, len(warehouse.rolled_back)) == ([], [], 1)


@pytest.mark.parametrize(("status", "answered"), [(HTTPStatus.CREATED, HTTPStatus.OK), (HTTPStatus.SERVICE_UNAVAILABLE, HTTPStatus.BAD_GATEWAY)], ids=["accepted", "refused"])
def test_a_route_that_writes_no_database_makes_the_call_at_once(status: int, answered: int) -> None:
    warehouse = _warehouse()
    ledger = Ledger(warehouse, status=status)
    with TestClient(_app(warehouse, ledger).asgi) as client:
        assert client.post("/direct", json=PRICE).status_code == answered
    assert ([key for key, _, _ in ledger.seen], warehouse.outbox) == ([f"price-{SECURITY}"], [])


# The relay


def _row(**changed: object) -> Row:
    """A call in the outbox, due at `AT`, which its tests change only where they are about it."""
    row = Row(
        id="0190f1a2-0000-7000-8000-000000000001", service="ledger", method="POST", path="/bookings", body=json.dumps(PRICE), idempotency_key=f"price-{SECURITY}",
        route="book", request_id=None, created_at=AT - timedelta(minutes=5), expires_at=AT + timedelta(hours=1), next_at=AT, attempts=1, status="pending",
        last_error="ledger answered 503 to POST /bookings", ended_at=None,
    )  # fmt: skip
    return Row.model_validate(row.model_dump() | changed)


def _relay(warehouse: fakes.Database, ledger: Ledger, *, at: datetime = AT) -> list[relay.Relayed]:
    planned = relay.plan(SERVICE, api.Settings.given(), {WAREHOUSE: warehouse, LEDGER: fakes.Http(ledger.app)})
    return relay.relay(planned, NoOpTracer(), now=lambda: at)


def test_the_relay_makes_a_due_call_with_its_key_and_records_it_done() -> None:
    warehouse = _warehouse(_row())
    ledger = Ledger(warehouse)
    [relayed] = _relay(warehouse, ledger)
    assert [(key, body) for key, body, _ in ledger.seen] == [(f"price-{SECURITY}", PRICE)]
    assert [(row.status, row.attempts, row.ended_at) for row in warehouse.outbox] == [("done", 2, AT)]
    assert (relayed.accepted, relayed.refused, relayed.expired) == (1, 0, [])


def test_the_relay_makes_a_refused_call_again_after_twice_the_last_wait() -> None:
    warehouse = _warehouse(_row())
    ledger = Ledger(warehouse, status=HTTPStatus.SERVICE_UNAVAILABLE)
    [relayed] = _relay(warehouse, ledger)
    assert [(row.status, row.attempts, row.next_at) for row in warehouse.outbox] == [("pending", 2, AT + timedelta(seconds=120))]
    assert (relayed.accepted, relayed.refused) == (0, 1)


def test_the_relay_leaves_a_call_that_is_not_due() -> None:
    warehouse = _warehouse(_row(next_at=AT + timedelta(seconds=1)))
    ledger = Ledger(warehouse)
    [relayed] = _relay(warehouse, ledger)
    assert (ledger.seen, warehouse.outbox, relayed.accepted) == ([], [_row(next_at=AT + timedelta(seconds=1))], 0)


@pytest.mark.parametrize("status", ["done", "expired"])
def test_the_relay_never_makes_a_call_that_has_ended(status: str) -> None:
    ended = _row(status=status, next_at=None, ended_at=AT)
    ledger = Ledger(warehouse := _warehouse(ended))
    _relay(warehouse, ledger)
    assert (ledger.seen, warehouse.outbox) == ([], [ended])


def test_a_call_that_has_aged_out_is_made_no_more_and_fails_the_run(logged: Records) -> None:
    warehouse = _warehouse(_row(expires_at=AT))
    ledger = Ledger(warehouse)
    found = _relay(warehouse, ledger)
    assert ledger.seen == []
    assert [(row.status, row.next_at, row.ended_at) for row in warehouse.outbox] == [("expired", None, AT)]
    failed = relay.failure(found, "prices")
    assert failed is not None
    reason, fix = failed
    assert reason.startswith(
        f"a call in service prices's outbox aged out before its service accepted it, and is made no more: POST /bookings to ledger with Idempotency-Key price-{SECURITY}"
        " (route book, row 0190f1a2-0000-7000-8000-000000000001), in warehouse's prices_outbox, after 1 attempts, the last refused with: ledger answered 503"
    )
    assert "set its row's status to 'pending'" in fix
    assert [record["idempotency_key"] for record in logged() if record["level"] == "error"] == [f"price-{SECURITY}"]


def test_the_relay_takes_calls_in_batches_until_none_is_due() -> None:
    rows = [_row(id=f"0190f1a2-0000-7000-8000-{index:012d}", idempotency_key=f"price-{index}") for index in range(relay.BATCH + 5)]
    warehouse = _warehouse(*rows)
    ledger = Ledger(warehouse)
    [relayed] = _relay(warehouse, ledger)
    assert (relayed.accepted, len(ledger.seen), {row.status for row in warehouse.outbox}) == (relay.BATCH + 5, relay.BATCH + 5, {"done"})


def test_a_call_to_a_service_the_api_no_longer_declares_fails_its_attempt() -> None:
    warehouse = _warehouse(_row(service="payments"))
    [relayed] = _relay(warehouse, Ledger(warehouse))
    [row] = warehouse.outbox
    assert (relayed.refused, row.status, row.last_error) == (1, "pending", "the service declares no api.http('payments') to make it with")


def test_the_relay_needs_an_outbox_and_the_settings_of_what_it_reaches() -> None:
    bare = api.Service(name="bare", settings=api.Settings, resources=[PLAIN, LEDGER], routes=[_post("/direct", book_directly)])
    with pytest.raises(api.ServiceError, match="service bare keeps no outbox, so basis relay has no calls to make"):
        relay.plan(bare, api.Settings.given(), {})
    with pytest.raises(api.SettingsError, match="set BASIS_DATABASES__WAREHOUSE__KIND and the rest of its block, for the database warehouse; BASIS_HTTP__LEDGER__URL"):
        relay.plan(SERVICE, api.Settings.given(), {})


# The commands


SERVICE_MODULE = """
from basis import api

WAREHOUSE = api.database("warehouse", deadline_s=1, max_concurrent=4, outbox=api.outbox("ops.prices_outbox", max_age_s=3600, backoff_s=60, max_backoff_s=600))
LEDGER = api.http("ledger", deadline_s=1)


def health() -> None: ...


service = api.Service(name="prices", settings=api.Settings, resources=[WAREHOUSE, LEDGER], routes=[api.get("/up", health, deadline_s=1, response=None, errors=[], status=204)])
"""


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logging_restored: None) -> Path:
    """An API repo whose service keeps an outbox, as the module `outboxed_service`, and no BASIS_ variable of the
    developer's; the logging `basis` configures is undone after."""
    del logging_restored
    for name in [name for name in os.environ if name.startswith("BASIS_")]:
        monkeypatch.delenv(name)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "prices-api"\nversion = "0.1.0"\n\n[tool.basis]\nparts = ["api"]\n', encoding="utf-8")
    (tmp_path / "outboxed_service.py").write_text(textwrap.dedent(SERVICE_MODULE), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "outboxed_service", raising=False)
    return tmp_path


def _basis(repo: Path, *argv: str) -> int:
    return command.run(argv, cwd=repo, installed=command.installed())


def test_ddl_prints_the_outbox_table_and_its_index(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _basis(repo, "ddl", "outboxed_service", "--dialect", "oracle") == 0
    printed = capsys.readouterr().out
    assert printed.startswith("CREATE TABLE ops.prices_outbox (\n    id VARCHAR2(36 CHAR) NOT NULL,\n")
    assert "    idempotency_key VARCHAR2(255 CHAR) NOT NULL,\n" in printed
    assert "    body CLOB,  -- no MaxLength in the row: unbounded\n" in printed
    assert "    next_at TIMESTAMP(6),  -- naive, in UTC\n" in printed
    assert printed.endswith("    CONSTRAINT pk_prices_outbox PRIMARY KEY (id)\n);\nCREATE INDEX ix_prices_outbox_due ON ops.prices_outbox (next_at);\n")


@pytest.mark.parametrize("dialect", ["mssql", "oracle"])
def test_the_outbox_table_has_a_column_for_each_of_a_rows_fields_in_order(dialect: str) -> None:
    printed = outboxes.ddl("warehouse", "prices_outbox", "mssql" if dialect == "mssql" else "oracle")
    assert tuple(re.findall(r"^    ([a-z_]+) (?!KEY)", printed, re.MULTILINE)) == tuple(Row.model_fields)


def test_ddl_of_a_service_without_an_outbox_is_one_error_line(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _basis(repo, "ddl", "basis.api.example.service", "--dialect", "mssql") == 1
    assert capsys.readouterr().err.splitlines()[-1].startswith("ERROR: service accruals keeps no outbox, so it has no table to create: declare one where a route sends a call")


def test_basis_lists_relay_and_ddl_in_a_repo_with_an_api(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _basis(repo, "--help") == 0
    out = capsys.readouterr().out
    assert re.search(r"^  relay +make the calls the API's outboxes still hold", out, re.MULTILINE)
    assert re.search(r"^  ddl +print the CREATE TABLE of each outbox", out, re.MULTILINE)


def test_relay_without_its_settings_is_one_error_line_after_its_record(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _basis(repo, "relay", "outboxed_service") == 1
    err = capsys.readouterr().err.splitlines()
    assert json.loads(err[-2])["message"] == "finished relay"
    assert err[-1].startswith("ERROR: service prices's outboxes and services are not all configured, so basis relay cannot run: set BASIS_DATABASES__WAREHOUSE__KIND")
