"""What the settings hold that is secret never reaches a log or an error: a run store's credentials, a password in a
connection string that does not parse, a service's or its proxy's login, and the body of a reply that failed. And a
log record whose fields JSON cannot hold, as a missing time or bytes, is still one line of JSON."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import pandas as pd
import pytest
from opentelemetry.trace import NoOpTracer

from basis.core import fakes, resources
from basis.core.databases.mssql import connect_to
from basis.core.errors import BasisError, ServiceError
from basis.core.logs import JsonFormatter, describe
from basis.core.opened import HttpClient, client
from basis.core.secrets import redacted
from basis.core.settings import HttpService
from basis.core.storage import Root


@pytest.mark.parametrize(
    ("given", "shown"),
    [
        ("abfs://runs@lakeprod.dfs.core.windows.net/etl", "abfs://runs@lakeprod.dfs.core.windows.net/etl"),
        ("s3://AKIAEXAMPLE:wJalrXUtnFEMI@bucket/runs", "s3://AKIAEXAMPLE:***@bucket/runs"),
        ("https://user:pa55word@pricing.firm/v1?sig=Zm9vYmFy&sv=2024&expiry=1", "https://user:***@pricing.firm/v1?sig=***&sv=2024&expiry=1"),
        ("Server=sql;UID=etl;PWD=Pa55;Database=x", "Server=sql;UID=etl;PWD=***;Database=x"),
        ("Server=sql;Password={a;b};Encrypt=yes", "Server=sql;Password=***;Encrypt=yes"),
    ],
    ids=["no-secret", "url-login", "url-login-and-sas", "connection-string", "braced-password"],
)
def test_a_secret_in_a_url_or_a_connection_string_is_shown_as_stars(given: str, shown: str) -> None:
    assert redacted(given) == shown


def test_a_run_store_is_named_without_its_credentials() -> None:
    root = Root.at("s3://AKIAEXAMPLE:wJalrXUtnFEMI@bucket/runs?endpoint_override=127.0.0.1:9&scheme=http")
    assert "wJalrXUtnFEMI" not in root.location
    assert "wJalrXUtnFEMI" not in (root / "runs" / "job").location


def test_a_connection_string_that_does_not_parse_is_refused_without_its_text() -> None:
    with pytest.raises(BasisError) as raised:
        connect_to("warehouse", "Server=127.0.0.1,9;UID=sa;PWD=Pa55;tail-of-it;Database=x", 1, autocommit=True)
    described = describe(raised.value)
    shown = json.dumps([described["error"], described["notes"], described["root_cause"]])  # what a log says of it, beside the traceback's source lines
    assert "tail-of-it" not in shown
    assert "Pa55" not in shown
    assert str(raised.value).startswith("the connection string of database warehouse does not parse")


def test_a_service_s_and_its_proxy_s_logins_are_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    service = HttpService.model_validate({"url": "https://user:pa55word@pricing.firm/v1", "proxy": "http://proxyuser:proxypa55@proxy.firm:8080"})
    with caplog.at_level(logging.DEBUG, logger="basis.core.opened"):
        client("pricing", service)
    logged = caplog.text + json.dumps([record.__dict__ for record in caplog.records], default=str)
    assert "pa55word" not in logged
    assert "proxypa55" not in logged


def test_the_body_of_a_reply_that_failed_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    async def app(scope: MutableMapping[str, Any], receive: Callable[[], Awaitable[MutableMapping[str, Any]]], send: Callable[[MutableMapping[str, Any]], Awaitable[None]]) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": 404, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"no such price; token=eyJsecret"})

    fake = fakes.Http(app)
    calls = HttpClient(resources.http("pricing", deadline_s=5), fake.client("pricing"), NoOpTracer(), waits=fake.waits)
    with caplog.at_level(logging.DEBUG), pytest.raises(ServiceError):
        asyncio.run(calls.get("/prices/S1", dict))
    assert "eyJsecret" not in caplog.text + json.dumps([record.__dict__ for record in caplog.records], default=str)


@pytest.mark.parametrize("value", [pd.NaT, b"\xff\xfe raw", {"nested": pd.NaT, "bytes": b"\x00"}], ids=["nat", "bytes", "nested"])
def test_a_record_whose_fields_json_cannot_hold_is_still_one_line_of_json(value: object) -> None:
    record = logging.LogRecord("basis.test", logging.INFO, __file__, 1, "a value", None, None)
    record.value = value
    line = JsonFormatter().format(record)
    assert "\n" not in line
    assert json.loads(line)["message"] == "a value"
