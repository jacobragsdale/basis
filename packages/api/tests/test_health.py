from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient

from basis import api


def quote(security: str) -> dict[str, str]:
    return {"security": security}


SERVICE = api.Service(name="quotes", settings=api.Settings, routes=[api.get("/quotes/{security}", quote, deadline_s=1, response=dict[str, str], errors=[])])


def _readiness(client: TestClient) -> tuple[int, object]:
    response = client.get("/health/ready")
    body: object = response.json()
    assert isinstance(body, dict)
    return response.status_code, body.get("detail") if response.status_code != HTTPStatus.OK else body.get("status")


def test_ready_only_between_startup_and_shutdown() -> None:
    app = api.build(SERVICE, settings=api.Settings.given())
    client = TestClient(app.asgi)
    assert _readiness(client) == (HTTPStatus.SERVICE_UNAVAILABLE, "the service is starting")
    with client:
        assert _readiness(client) == (HTTPStatus.OK, "ready")
        app.lifecycle.drain()
        assert _readiness(client) == (HTTPStatus.SERVICE_UNAVAILABLE, "the service is draining")
        assert client.get("/quotes/US912810TM64").status_code == HTTPStatus.OK  # still serving while the cluster stops routing to it
    assert _readiness(client) == (HTTPStatus.SERVICE_UNAVAILABLE, "the service is stopped")


def test_alive_whatever_the_readiness() -> None:
    app = api.build(SERVICE, settings=api.Settings.given())
    client = TestClient(app.asgi)
    assert client.get("/health/live").json() == {"status": "live", "commit": None}
    with client:
        app.lifecycle.drain()
        assert client.get("/health/live").json() == {"status": "live", "commit": None}


def test_health_says_which_commit_the_app_runs() -> None:
    with TestClient(api.build(SERVICE, settings=api.Settings.given(git_sha="3f1c2a9e")).asgi) as client:
        assert [client.get(f"/health/{which}").json() for which in ("live", "ready")] == [{"status": "live", "commit": "3f1c2a9e"}, {"status": "ready", "commit": "3f1c2a9e"}]


def test_not_ready_is_a_problem() -> None:
    response = TestClient(api.build(SERVICE, settings=api.Settings.given()).asgi).get("/health/ready")
    assert response.headers["content-type"] == "application/problem+json"
    assert (response.json()["type"], response.json()["title"]) == ("not-ready", "The service is not ready")


def test_an_app_starts_once() -> None:
    app = api.build(SERVICE, settings=api.Settings.given())
    with TestClient(app.asgi):
        pass
    with pytest.raises(api.DeclarationError, match="has started once already"), TestClient(app.asgi):
        pass


def test_the_health_endpoints_are_no_part_of_the_api_contract() -> None:
    document = TestClient(api.build(SERVICE, settings=api.Settings.given()).asgi).get("/openapi.json").json()
    assert sorted(document["paths"]) == ["/quotes/{security}"]
