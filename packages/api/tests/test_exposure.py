"""What an app tells a caller who is no one yet: the development key on no socket but this machine's, no `/docs` page,
whose script comes from a CDN, unless the settings ask for one, and a refused token's 401 that names neither the
audience nor the sign-in system, which the log says instead."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Annotated

import pytest
from fastapi.testclient import TestClient

from basis import api
from basis.api import tokens
from basis.api.identity import Identity

DEVELOPMENT = {"issuer": "https://sign-in.example", "audience": "positions", "algorithms": ["ES256"], "leeway_s": 0, "keys": {"kind": "development"}}


def me(user: Annotated[api.User | None, api.given(api.User)]) -> dict[str, str]:
    return {"subject": user.subject if user is not None else "anonymous"}


SERVICE = api.Service(name="positions", settings=api.Settings, routes=[api.get("/me", me, deadline_s=1, response=dict[str, str], errors=[])], user=api.User)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.1.2.3", "positions.internal.example"], ids=["every-interface", "every-ipv6-interface", "an-address", "a-name"])  # noqa: S104 -- refused
def test_the_development_key_is_refused_on_any_socket_but_this_machines(host: str) -> None:
    settings = api.Settings.given(identity=DEVELOPMENT, serve={"host": host})
    message = f"BASIS_IDENTITY__KEYS__KIND is development, whose key anyone can sign tokens with, and the API would serve on {host}, which other machines reach:"
    with pytest.raises(api.SettingsError, match=message):
        api.serve(SERVICE, settings=settings)


def test_the_docs_page_is_served_only_where_the_settings_ask_for_it() -> None:
    with TestClient(api.build(SERVICE, settings=api.Settings.given(identity=DEVELOPMENT)).asgi) as client:
        absent = [client.get("/docs").status_code, client.get("/redoc").status_code, client.get("/docs/oauth2-redirect").status_code]
    with TestClient(api.build(SERVICE, settings=api.Settings.given(identity=DEVELOPMENT, serve={"docs": True})).asgi) as client:
        page = client.get("/docs")
    assert absent == [HTTPStatus.NOT_FOUND] * 3
    assert page.status_code == HTTPStatus.OK
    scripts = [part.split('"')[1] for part in page.text.split("src=")[1:]]
    assert scripts
    assert all("swagger-ui-dist@5." in script and script.count(".") >= 4 for script in scripts), scripts  # an exact version, never the latest


def _token(**changed: str) -> str:
    identity = Identity.model_validate(DEVELOPMENT | changed)
    now = datetime.now(UTC)
    return tokens.sign(identity, "jsmith", issued=now, expires=now + timedelta(hours=1))


@pytest.mark.parametrize(
    ("changed", "detail", "why"),
    [
        ({"audience": "pricing"}, "the token is not for this API", "this API's audience is positions"),
        ({"issuer": "https://elsewhere.example"}, "the token is not from this API's issuer", "this API's issuer is https://sign-in.example"),
    ],
    ids=["another-audience", "another-issuer"],
)
def test_a_refused_token_names_what_the_api_expects_in_the_log_alone(changed: dict[str, str], detail: str, why: str, logged: Callable[[], list[dict[str, object]]]) -> None:
    with TestClient(api.build(SERVICE, settings=api.Settings.given(identity=DEVELOPMENT)).asgi) as client:
        response = client.get("/me", headers={"authorization": f"Bearer {_token(**changed)}"})
    assert (response.status_code, response.json()["detail"]) == (HTTPStatus.UNAUTHORIZED, detail)
    [record] = [record for record in logged() if str(record["message"]).startswith("refused the bearer token")]
    assert record["why"] == why
