"""The fakes the app runs on in `basis dev` and its tests, from the repo's `fixtures/`."""

from basis.dash import fixtures
from fixture_app.data import WAREHOUSE


def fakes() -> dict[object, object]:
    return {WAREHOUSE: fixtures.database(WAREHOUSE)}
