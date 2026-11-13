"""The app's declaration."""

import basis.dash as bd
from fixture_app import overview
from fixture_app.data import WAREHOUSE

app = bd.App(name="fixtures", title="Fixtures", settings=bd.Settings, resources=[WAREHOUSE], pages=[overview.page], fakes="fixture_app.dev:fakes")
