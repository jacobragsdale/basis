# Reference

What basis-dash gives a Dash app's repo, by name, as `import basis.dash as bd` names it. The rules behind each are in
the repo's `AGENTS.md`, and the reasons in basis's design page, under "Dash apps: basis.dash".

## The layers

Each layer is a module basis knows by its name, a package's modules taking its name too, so `pages/positions.py` is a
page. `basis check` holds them, each failure one `ERROR` line with its fix.

| Module | Holds | Imports, of the layers |
|---|---|---|
| `app.py` | The `bd.App` | any below it |
| `pages/` | One module per page: its layout and its callbacks, which are the app's only there or in `app.py` | `data`, `domain` |
| `data.py` | The rows, the databases and every dataset the pages load | `domain` |
| `domain.py` | Pure transforms, frames in and a frame out, which a view runs | none |

`domain.py`, and the module of every view's function, imports no Dash, no Plotly, no web server, nothing of
`basis.dash` and nothing that does I/O. `dev.py`, a test's module and a job's are no layer.

## The app

| Name | What it is |
|---|---|
| `bd.App(name=, title=, settings=, resources=, pages=, fakes=)` | The app: its name in lower case and dashes, its title, its settings class, every resource it reads, its pages in the navigation's order, and `"module:function"`, the function that returns a fake of each resource |
| `bd.Settings` | basis-core's settings, with `serve` (`port`, `prefix`, `prefix_stripped`, `drain_s`, `shutdown_s`), `smoke` (`url`, `ca_bundle`) and `cache_mb`; an app may subclass it |
| `bd.testing.client(app)` | The app built on its fakes and started in the process: `.page(PATH)`, `.layout()`, `.dependencies()`, `.fire(OUTPUT, {"ID.PROP": VALUE})`, and `.http`, FastAPI's test client |
| `bd.build(app)`, `bd.serve(app)` | The Dash app built, and served with uvicorn; `basis serve` and `basis dev` call them |

## Data

| Name | What it is |
|---|---|
| `bd.Row` | A frame's columns, typed; a float's display format as `Annotated[float, bd.format.money("USD", 0)]`; a column that may be missing as `... \| None` |
| `bd.format.number(decimals)`, `money(currency, decimals)`, `pct(decimals)`, `bps(decimals)`, `price(decimals)`, `price32(step)` | A column's display format, which the grid, the charts, the tiles, `bd.text` and the exports all write by |
| `bd.database(name, dialect=, deadline_s=, max_concurrent=)` | A SQL database, `"mssql"` or `"oracle"`, its settings `BASIS_DATABASES__NAME__*`, read through its read login alone |
| `bd.folder(name)`, `bd.lake(name)`, `bd.http(name, deadline_s=)` | A shared folder, a lake's container and another service, each by its settings |
| `bd.query(name, database, sql, Row, binds=, ttl_s=, stale_s=, reload_s=, smoke=)` | A SQL file's rows, binding each param of `binds` and nothing else |
| `bd.file(name, source, path, Row, sheet=, binds=, ttl_s=, stale_s=, reload_s=, smoke=)` | A CSV, Parquet or workbook's rows, from a folder or a lake, its path naming its params as `marks/{as_of}.csv` |
| `bd.fetch(name, service, path, Row, binds=, rows_at=, ttl_s=, stale_s=, reload_s=, smoke=)` | Another service's JSON rows, each param in the query |
| `bd.view(name, function, Row, INPUT=DATASET)` | A pure function of datasets and its own params, run in a worker thread, cached by its inputs' versions |
| `await bd.load(DATASET, **params)` | A dataset's frame, from the cache while it is fresh, read off the event loop otherwise, in an `async` callback |
| `bd.reload(DATASET, **params)` | Drops the value, so the next load reads it, unless it was read less than `reload_s` ago |
| `bd.loaded(DATASET, **params)` | When the value a load would answer was read, or None |
| `bd.rows(frame)` | A frame's rows as a grid's `rowData` takes them: NaN as null, dates as ISO text |

`ttl_s` is how long a value is fresh; `stale_s`, how long past it a value may still be shown when a reload fails;
neither has a default. `smoke=` is the params `basis smoke` loads the dataset with, or a function of no arguments that
returns them.

## Pages and components

| Name | What it is |
|---|---|
| `bd.page(path, title, filters=, body=, description=, datasets=)` | A page: its title and freshness line, its filter bar, and its body on twelve columns; `layout=` instead of `body=` lays it out itself |
| `bd.card(title, *content, span=, description=, actions=, flush=)` | A card of a page's body, `span` columns wide |
| `bd.as_of(id, label, calendar=)`, `bd.date_range(id, label, presets=, default=, calendar=)`, `bd.select(id, label, options=, multiple=, default=)`, `bd.search(id, label, placeholder=)` | The filters, each kept in the URL's query string under its id; `bd.day(value)`, `bd.days(value)` read the first two, and `bd.options(frame, column)` gives a select's options |
| `bd.grid(id, Row, key=, totals=, columns=, height=, search=, noun=, dataset=, exports=)` | AG Grid from the row: headers, formats, filters, a quick filter, a count, totals, and CSV and Excel exports |
| `bd.column(header=, width=, pin=, hide=, signed=, bar=, heat=)`, `bd.weighted(by)` | A grid column's overrides, and a total weighted by another column |
| `GRID.totals(frame)`, `GRID.exported(frame, asked=, loaded_at=, name=, title=)`, `GRID.export`, `GRID.download` | The totals row, as a callback's answer to the grid's `dashGridOptions`; an export's file, and the ids its callback takes and answers |
| `bd.graph(id, figure=, height=, dataset=)` | A graph with basis's config, whose `figure` a callback answers |
| `bd.charts.line`, `bar`, `scatter`, `curve`, `waterfall`, `heatmap`, `histogram` | Figures from a frame and its row, their ticks and hover text in the row's formats |
| `bd.stat(id, label, display, change=, period=, good=, span=, dataset=)`, `TILE.show(value, change=, history=)` | A stat tile, and its `children` |
| `bd.badge(text, tone, icon_name=)`, `bd.icon(name, label=)`, `bd.text(value, display)` | A badge, an icon of basis's set, and a value as people read it |
| `bd.refresh(id, every_s=)` | A timer, at least 5 s, whose `n_intervals` a callback takes |

## Commands

| Command | What it does |
|---|---|
| `basis dev [APP] [--env ENV]` | Serves the app on `127.0.0.1:8000`, on its fakes or an environment's resources, with Dash's dev tools, reloading as code changes |
| `basis serve [APP] --env ENV` | Serves the app until SIGTERM, which drains it first: the image's command |
| `basis render PAGE [--set ID.PROP=VALUE]... [--env ENV] [--app APP]` | Runs a page's callbacks in this process and prints each output as JSON |
| `basis load DATASET [--param NAME=VALUE]... [--env ENV] [--app APP]` | Loads a dataset twice and prints its first rows and timings as JSON |
| `basis new page NAME` | Writes a page and its test, and adds it to the app's pages |
| `basis check` | Lints, type checks and tests the repo, and runs the app's checks on its fakes |
| `basis smoke --env ENV [--commit SHA]` | Checks the deployed app at `BASIS_SMOKE__URL` |
| `basis-dash new app NAME [--dir DIR] [--index URL] [--framework-path PATH]` | Makes a Dash app's repo, outside one, as `uvx basis-dash new app NAME` |

## Settings

| Variable | What it sets |
|---|---|
| `BASIS_ENV` | The environment the settings are of: `dev`, `qa` or `prod` |
| `BASIS_DATABASES__NAME__*` | Each database the app reads, by its declared name |
| `BASIS_SERVE__PORT`, `BASIS_SERVE__PREFIX`, `BASIS_SERVE__PREFIX_STRIPPED` | Where the app serves, and the path the firm's proxy serves it under |
| `BASIS_CACHE_MB` | The most megabytes of datasets the cache keeps, 512 by default |
| `BASIS_SMOKE__URL`, `BASIS_SMOKE__CA_BUNDLE` | Where `basis smoke` reaches the deployed app |
| `BASIS_TRACES__EXPORTER`, `BASIS_TRACES__ENDPOINT` | Where spans go |

No `DASH_*` variable changes what Dash does: `basis serve` and `basis dev` refuse to start while one is set.
