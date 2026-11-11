"""The dirty-price route's largest face amount, a setting."""

from http import HTTPStatus

from basis.api import fakes
from fastapi.testclient import TestClient

from quotes.repository import BOND
from quotes.tests.test_handlers import ISIN, Ledger, _bonds, _pricing, built


def test_a_face_over_the_setting_is_refused_naming_face() -> None:
    warehouse = fakes.Database(answers={BOND: fakes.Respond(_bonds)})
    with TestClient(built(warehouse, fakes.Http(_pricing()), Ledger(), fakes.Cache(), max_face=1000).asgi) as client:
        refused = client.get(f"/bonds/{ISIN}/dirty-price", params={"face": 2000, "settle": "2026-11-06"})
        priced = client.get(f"/bonds/{ISIN}/dirty-price", params={"face": 500, "settle": "2026-11-06"})
    assert refused.status_code == HTTPStatus.UNPROCESSABLE_CONTENT
    assert refused.json()["errors"] == [{"detail": "is more than 1,000", "parameter": "face"}]
    assert priced.status_code == HTTPStatus.OK
