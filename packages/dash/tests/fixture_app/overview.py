"""The app's one page, and its callback."""

from datetime import date

from dash import Input, Output, callback, dcc, html

import basis.dash as bd
from fixture_app.data import POSITIONS

page = bd.page("/", "Positions", layout=html.Div([dcc.Input(id="fixture-day", value="2026-11-13"), html.Div(id="fixture-par")]))


@callback(Output("fixture-par", "children"), Input("fixture-day", "value"))
async def total(day: str) -> str:
    frame = await bd.load(POSITIONS, as_of=date.fromisoformat(day))
    return f"{len(frame)} positions, {frame['par'].sum():,.0f} par"
