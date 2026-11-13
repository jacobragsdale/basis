"""The app's declaration: basis builds the Dash app from it."""

import basis.dash as bd

from basis.dash.example.data import WAREHOUSE
from basis.dash.example.pages import positions

app = bd.App(name="holdings", title="Fixed Income Holdings", settings=bd.Settings, resources=[WAREHOUSE], pages=[positions.page], fakes="basis.dash.example.dev:fakes")
