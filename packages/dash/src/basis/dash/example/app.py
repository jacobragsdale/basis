"""The app's declaration: basis builds the Dash app from it."""

import basis.dash as bd

from basis.dash.example.data import WAREHOUSE
from basis.dash.example.pages import curve, overview, pnl, positions

app = bd.App(
    name="holdings", title="Fixed Income Holdings", settings=bd.Settings, resources=[WAREHOUSE], pages=[overview.page, positions.page, curve.page, pnl.page], fakes="basis.dash.example.dev:fakes"
)
