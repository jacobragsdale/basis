## basis-dash

This repo's Dash app runs on basis-dash {version}. The app is one `bd.App` (`import basis.dash as bd`), which declares
its name, its title, its settings, its resources, its pages and its fakes; basis builds the Dash app around it, on
Dash's FastAPI backend, with the shell, the theme in light and dark, request ids and JSON logs, traces, health and
readiness, and a graceful shutdown. The app never makes `dash.Dash` itself, and the repo lists it under
`[project.entry-points."basis.dash_apps"]` in its `pyproject.toml`. A page is written in plain Dash's idioms:
`from dash import Input, Output, State, callback`, `dmc` and `dag` as they are, with basis's components beside them.

The app's code sits in layers, each a module basis knows by its name: `app.py`, the declaration; `pages/`, one module
per page, holding its layout and its callbacks; `data.py`, the rows, the databases and every dataset the pages load;
and `domain.py`, the pure transforms its views run. `dev.py` gives the fakes. `basis check` holds these rules, each
failure one `ERROR` line with its fix: imports go down that list only; `domain.py`, and the module of every view's
function, imports no Dash, no Plotly, nothing of `basis.dash` and nothing that does I/O, so a job could run it too; a
callback lives in a page's module or the app's, since basis gives the app no other; and every page builds, every
callback's ids and properties are its page's, and every callback a page's load calls runs on the fakes from the
layout's values, within 4 MB, without holding the event loop past 250 ms.

Rules to keep:

- Declare each dataset once, by name, in `data.py`: `bd.query(NAME, DATABASE, "sql/NAME.sql", Row, binds={"as_of":
  date}, ttl_s=, stale_s=)` reads a SQL file of the package's, which binds each param and nothing else, in its
  database's dialect (`%(as_of)s` for SQL Server, `:as_of` for Oracle); `bd.file` a folder's or a lake's file;
  `bd.fetch` another service's JSON rows; and `bd.view(NAME, domain.FUNCTION, Row, INPUT=DATASET)` a pure function
  of other datasets and its own params. Each says how fresh it must be, `ttl_s` and `stale_s`, with no default; a
  `reload_s` lets a person ask for it again with `bd.reload`.
- Query by the few params that change what is read, an as-of day above all, and filter in a view: a book, a sector
  or a search is a view's param, so the cache answers every filter from one query.
- A page gets data only by `await bd.load(DATASET, **params)`, in an `async def` callback; a load in a callback that
  is not `async` is refused, since it would hold every user's page. Never keep rows in a `dcc.Store`: ask `bd.load`
  again in each callback that needs them, which the cache answers in well under a millisecond.
- Every number is a float, with its display format on its row's column, as
  `par: Annotated[float, bd.format.money("USD", 0)]`; `bd.grid`, `bd.charts`, `bd.stat`, `bd.text` and the exports
  all write it by that format. A column that may be missing is `Annotated[float, ...] | None`.
- Build a page with `bd.page(PATH, TITLE, filters=[...], body=[...])`: filters from `bd.as_of`, `bd.date_range`,
  `bd.select` and `bd.search`, whose state is the URL's; a body of `bd.card`s, `bd.stat` tiles, `bd.grid(ID, Row)`
  and `bd.graph(ID)` with a figure from `bd.charts`. Plain `dmc`, `dag` and `dcc` components work beside them; never
  `dash_table.DataTable`, a URL of another origin, or a stylesheet from a CDN, which `basis check` refuses.
- A callback answers `bd.rows(frame)` for a grid's `rowData`, `GRID.totals(frame)` for its `dashGridOptions`,
  `TILE.show(value)` for a tile's `children`, and a `bd.charts` figure for a graph's `figure`. A failure needs no
  handler: basis logs it once, keeps what the page showed, and tells the person in one sentence with the request's id.
- An app reads, and never writes: its databases give a reader alone. Its settings are `bd.Settings`, or a subclass, as
  `BASIS_*` variables; nothing reads `os.environ`, and no `DASH_*` variable changes what Dash does.
- Test the domain on frames in `tests/test_domain.py`, and each page on the fakes with `bd.testing.client(app)`,
  firing its callbacks, in `tests/test_pages.py`. A fixture is a CSV per query in `fixtures/`, read through the
  query's row, so a fixture that does not match fails as a real answer would.

Commands:

```sh
uv run basis dev                            # the app on its fakes, at http://127.0.0.1:8000, reloading as code changes
uv run basis dev --env dev                  # the same on dev's databases, read-only
uv run basis render /                       # what a page shows, each output as JSON; --set ID.PROP=VALUE stands in for the layout's
uv run basis load DATASET --param as_of=2026-11-12   # a dataset's first rows, and how the cache answered
uv run basis new page NAME                  # a page in pages/, its test, and its place in app.py's pages
uv run basis check                          # the files basis owns, ruff, ty, the gates, pytest and the app's checks
uv run basis smoke --env qa                 # the deployed app: ready, every page answering, each dataset's smoke example loading
uv run basis docs dash/reference            # every name and setting, for this version
```
