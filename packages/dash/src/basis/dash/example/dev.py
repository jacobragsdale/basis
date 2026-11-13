"""The fakes `basis dev` and the tests run the app on: the warehouse, answered from `fixtures/`, synthetic holdings
that `packages/dash/tests/example_fixtures.py` makes."""

from pathlib import Path

from basis.dash import fixtures

from basis.dash.example.data import WAREHOUSE

FIXTURES = Path(__file__).parent / "fixtures"


def fakes() -> dict[object, object]:
    return {WAREHOUSE: fixtures.database(WAREHOUSE, FIXTURES)}
