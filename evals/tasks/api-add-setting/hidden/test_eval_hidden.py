"""The dirty-price route's `max_face` setting: a billion by default, a 422 problem naming `face` above it, and a
setting that can lower it."""

from http import HTTPStatus

from quotes.resources import Settings
from quotes.tests.eval_app import ISIN, client


def price(face: float, **settings: object) -> tuple[int, dict[str, object]]:
    with client(**settings) as started:
        response = started.get(f"/bonds/{ISIN}/dirty-price", params={"face": face, "settle": "2026-11-06"})
    return response.status_code, response.json()


def test_the_limit_is_a_setting_of_a_billion() -> None:
    assert Settings.model_fields["max_face"].default == 1_000_000_000


def test_a_billion_is_priced_and_more_is_refused_naming_face() -> None:
    assert price(1_000_000_000)[0] == HTTPStatus.OK
    status, problem = price(1_000_000_001)
    assert status == HTTPStatus.UNPROCESSABLE_CONTENT
    errors = problem.get("errors")
    assert isinstance(errors, list)
    assert any(isinstance(error, dict) and error.get("parameter") == "face" for error in errors)


def test_a_configured_limit_holds() -> None:
    assert price(2_000, max_face=1_000)[0] == HTTPStatus.UNPROCESSABLE_CONTENT
    assert price(500, max_face=1_000)[0] == HTTPStatus.OK
