"""The positions page on the app's fakes, through `bd.testing`, with no browser: what its callback answers, and that its
numbers reconcile with each other."""

import basis.dash as bd
import pytest

from basis.dash.example.app import app
from basis.dash.example.pages import positions

AS_OF = {"as-of.value": "2026-11-12", "book.value": None, "search.value": ""}
OUTPUTS = ("grid.rowData", "grid.dashGridOptions", "market-value.children", "dv01.children", "yield.children", "duration.children", "sectors.figure", "scatter.figure")
SHOW = "...".join(f"positions-{output}" for output in OUTPUTS).join(("..", ".."))
"""The page's one callback that shows it, by its outputs, as Dash names a callback of several."""


def test_the_positions_page_builds_inside_the_shell() -> None:
    with bd.testing.client(app) as client:
        assert "Every holding, priced at the close" in str(client.page(positions.page.path))


def test_the_grid_holds_every_holding_and_its_total_is_the_tiles() -> None:
    with bd.testing.client(app) as client:
        answered = client.fire(SHOW, AS_OF)
    rows = answered["positions-grid"]["rowData"]
    assert len(rows) == 320  # the fixture's holdings
    total = sum(row["market_value"] for row in rows)
    assert str(answered["positions-market-value"]["children"]).count(bd.text(total, bd.format.money("USD", 0))) == 1


def test_a_book_keeps_its_own_holdings() -> None:
    with bd.testing.client(app) as client:
        answered = client.fire(SHOW, {**AS_OF, "book.value": "Municipals"})
    rows = answered["positions-grid"]["rowData"]
    assert {row["book"] for row in rows} == {"Municipals"}
    assert sum(row["weight"] for row in rows) == pytest.approx(1.0)
