---
name: dash-add-page
description: Add a page to a basis-dash app end to end, from its datasets to its layout, callbacks and tests. Use when the user wants a new page, screen, view or tab in the Dash app.
---

# Add a page

1. Read `uv run basis docs dash/reference` once: the layers, the components and what a callback answers.
2. Make the page: `uv run basis new page NAME`. It writes `pages/NAME.py`, with a layout and a callback that loads,
   `tests/test_NAME.py`, and adds `NAME.page` to the app's `pages=[...]` in `app.py`.
3. Data first, in `data.py`: a row for what the page shows, each number a float with its format, as
   `Annotated[float, bd.format.pct(2)]`; and a dataset for it, a `bd.view` of the datasets there where the page
   reshapes or filters them, its function pure in `domain.py`. A new query needs its SQL file in `sql/`, binding each
   param, and its fixture in `fixtures/NAME.csv`.
4. Layout, in `pages/NAME.py`: `bd.page("/NAME", "Title", filters=[...], body=[...])`, its filters from `bd.as_of`,
   `bd.select` and `bd.search`, its body `bd.stat` tiles, `bd.card`s holding a `bd.grid(ID, Row)` or a `bd.graph(ID)`.
   Prefix each id with the page's name, since every page shares the shell.
5. Callbacks, in the same module: `@callback(Output(...), Input(...))` on an `async def`, each dataset read with
   `await bd.load(DATASET, **params)`, answering `bd.rows(frame)`, `GRID.totals(frame)`, `TILE.show(value)` and a
   `bd.charts` figure. Never a `dcc.Store` of rows: load again where another callback needs them.
6. Tests: the view's function in `tests/test_domain.py` on plain frames; the page in `tests/test_NAME.py` with
   `bd.testing.client(app)` and `client.fire(OUTPUT, {"ID.PROP": VALUE})`.
7. Look at it: `uv run basis render /NAME` prints each output as the page shows it, and `uv run basis dev` serves it.
8. Run `uv run basis check`, and fix each `ERROR` line as it says.
