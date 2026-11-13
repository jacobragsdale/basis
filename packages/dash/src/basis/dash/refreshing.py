"""A page's data, read again: on a timer, and when a person asks.

    page = bd.page("/", "Positions", layout=html.Div([bd.refresh("positions-every", every_s=60), dmc.Button("Reload", id="reload"), ...]))


    @callback(Output("positions", "rowData"), Input("as-of", "value"), Input("positions-every", "n_intervals"), Input("reload", "n_clicks"))
    async def show(as_of: str, _ticks: int | None, clicks: int | None) -> bd.Rows:
        day = date.fromisoformat(as_of)
        if ctx.triggered_id == "reload":
            bd.reload(POSITIONS, as_of=day)
        return bd.rows(await bd.load(POSITIONS, as_of=day))

- **`bd.refresh(id, every_s=)`** is a `dcc.Interval` whose `n_intervals` a page's callbacks take as an input, so they
  run again every `every_s`, at least `MIN_EVERY_S`. Each load still answers from the cache while its value is fresh,
  so a page shows a dataset's new value as soon as its `ttl_s` lets it, and a timer never reads more often than that.
- **`bd.reload(DATASET, **params)`** drops the dataset's value, so the next load reads it, unless it was read less
  than its declared `reload_s` ago, so a button pressed again and again cannot hammer the database. A dataset that
  declares no `reload_s` cannot be reloaded, and a view is reloaded by reloading its inputs.
- **`bd.loaded(DATASET, **params)`** is when the value a load would answer was read, in UTC, or None where it has not
  been, without reading it; the status store tells the browser the same (`basis.dash.ids.STATUS`)."""

import math

from basis.dash.errors import DeclarationError
from dash import dcc

MIN_EVERY_S = 5.0
"""The shortest timer a page may run: a tick runs every callback that takes it."""

__all__ = ["MIN_EVERY_S", "refresh"]


def refresh(id: str, *, every_s: float) -> dcc.Interval:  # noqa: A002 -- Dash's own word for a component's id
    """A timer, `id`, whose `n_intervals` ticks every `every_s` seconds."""
    if not isinstance(id, str) or not id:
        msg = f"a refresh's id is {id!r}: give it one, which the page's callbacks take as Input(ID, 'n_intervals')"
        raise DeclarationError(msg)
    if isinstance(every_s, bool) or not isinstance(every_s, int | float) or not math.isfinite(every_s) or every_s < MIN_EVERY_S:
        msg = f"refresh {id} ticks every {every_s!r} s: give every_s, {MIN_EVERY_S:g} or more, since each tick runs every callback that takes it"
        raise DeclarationError(msg)
    return dcc.Interval(id=id, interval=round(every_s * 1000), n_intervals=0)
