# Plan: Dash apps, and one part's removal

Written on 2026-11-13, against commit `a63f5fe`. basis gets a new kind of part, `dash`: a Dash app that reads the
firm's data straight from Python, with no API behind it. The portfolio part leaves the repo. [The design](design.md) still holds
the decisions and [PLAN.md](../PLAN.md) phases 6 to 12, which are done; this page adds phases 13 to 18, and its step
ids continue theirs, so a branch is named as `AGENTS.md` says, such as `15.2-cache`.

The design below is the orchestrator's, made with the maintainer's leave to decide ("take the liberty to introduce
your own ideas"). Each choice he may want to reverse is in [Open decisions](#open-decisions), with the pick this
plan builds on. Step 14.2 writes the design into `docs/design.md`, where it then lives.

## Status

Keep this table true in each step's own commit.

| Step | State | Commit |
|---|---|---|
| 13.1 The portfolio part goes | Done: the code and its wiring, then the docs; history in [the design](design.md#the-packages) | `99aaf2e`, the commit "Step 13.1, part 2" |
| 14.1 The spike | Done: twelve yeses, in [the spike](spikes/dash.md); the steps it changed say so | the commit "Step 14.1: the Dash spike" |
| 14.2 The design, written down | Not started | |
| 14.3 Rows in core | Done: `basis.core.rows`, a `float`'s `Display`; `basis.job.rows` re-exports | the commit "Step 14.3" |
| 14.4 Serving in core | Not started | |
| 14.5 The package and an empty app | Not started | |
| 15.1 to 15.8 Data | Not started | |
| 16.1 to 16.9 Display | Not started | |
| 17.1 to 17.8 The repo around it | Not started | |

## What the best Dash app is

A Dash app goes wrong in the same few ways every time, and each is a thing basis can own or check, so an agent
cannot get it wrong quietly:

| What goes wrong | What basis does about it |
|---|---|
| One slow query freezes every user's page | Every load leaves the server's event loop, under its database's deadline and its own limit on concurrent queries; a load called on the loop is refused, with the fix (15.2) |
| The same query runs once per callback and once per user | A load is declared once and cached by its params; callbacks that ask at the same moment share one query (15.2) |
| Frames travel to the browser and back in a `dcc.Store` | Callbacks share data by asking the cache again, which costs nothing; `basis check` refuses a dataset in a store (17.1) |
| A database blip blanks the dashboard | A load that fails serves the last good value, marked stale, for as long as the dataset declares (15.5) |
| Transforms are tangled into callbacks, untested | A transform is a pure function in `domain.py`, declared as a view: typed, cached, run off the loop, tested with frames (15.3) |
| A number reads three ways in the grid, the chart and the export | A column declares its display format once, on its row; the grid, axes, hover text, stat tiles, Excel and Slack all read it, by one rounding rule (16.3) |
| Every app has its own look, and no dark mode | basis builds the shell, the theme and each component's loading, empty, error and stale states, from basis-ui's tokens (16.1, 16.8) |
| A wrong component id fails silently in the browser | `basis check` builds every page and checks every callback's inputs and outputs against it (17.1) |
| Nobody can run it without the database | `basis dev` serves the app on its synthetic fixtures; tests drive pages in-process, with no browser and no socket (15.4) |
| An agent cannot see what a page shows | `basis render PAGE` runs a page's callbacks in the process and prints what each output holds, as JSON (17.2) |
| The page loads fonts and scripts from the internet | Everything is served from the app; a page that names another origin fails the check (17.1) |

Plain Dash stays plain. Agents know `from dash import Input, Output, callback`, `dmc` and `dag` well, and a page here
is written in those idioms. basis adds the parts every team reinvents: the loads, the cache, the theme, the grid and
chart defaults, the states, the server and the checks.

## The stack

Versions as of 2026-11-13, installed together on Python 3.14.7 for this plan.

| Piece | Version | Why |
|---|---|---|
| Dash | 4.5.0, on its FastAPI backend, served by uvicorn | The server basis-api already runs, on Windows desktops and in the image alike; gunicorn has no Windows. The backend also carries WebSocket and streaming callbacks, for live prices later (18) |
| React | 19.2.4, Dash 4.5's opt-in | Mantine 9 needs it |
| dash-mantine-components | 3.0.0 (Mantine 9.7) | The shell, layout, inputs, date pickers, notifications and skeletons: the widest component set in Dash, with light and dark built in |
| dash-ag-grid | 35.3.0 (AG Grid 35.3.1), Community features only | The grid. `dash_table.DataTable` is deprecated since Dash 3.3 |
| Plotly | 7.1.0 | Charts, under one basis template |
| orjson | 3.13.0 | Plotly's encoder, which Dash's answers go through, uses it when installed: a tenth of the time for 10,000 rows ([14.1](spikes/dash.md#11-a-10000-by-20-grid)) |

Nothing else: no dash-bootstrap-components, whose last release was in 2025; no dash-extensions; no dash-iconify,
which fetches each icon from a public CDN at run time. Icons are a vendored subset, as basis-ui's are.

Checked while writing this plan, in a scratch app:

- `Dash(server=FastAPI())` works with routes and middleware of basis's own beside it, with Dash Pages, an async
  callback, and a global `on_error` that answers through `set_props`.
- NaN leaves a callback as JSON null.
- FastAPI's `TestClient` drives `/_dash-update-component` in the process, so a page's callbacks can be run for
  real with no browser and no socket.
- In headless Chromium on React 19.2.4, the Mantine shell, an AG Grid and a Plotly graph render together with no
  console error or warning and no request to another origin. On React 18.3.1, Dash's default, DMC 3 refuses to load.
- **A callback that is not `async` runs on the event loop's own thread** on this backend
  (`dash/backends/_fastapi.py`, `_dispatch`). A blocking query inside one stops the whole server, which is why
  every load is awaited (15.2).
- Dash's own MCP server is off unless asked for; basis passes `enable_mcp=False` so no variable can turn it on.

Step 14.1's answers, each with its evidence and the exact call, are in [the spike](spikes/dash.md); the steps
below that it changed say so.

## The shape of an app

A repo declares the part, `[tool.basis] parts = ["dash"]`, with `"job"` beside it where the repo also holds the
jobs that build the tables it shows. A repo holds an API or a Dash app, never both: each serves, and a repo has one
served thing, so `basis serve` and `basis dev` mean one thing in it. Declaring both is one `ERROR` line.

```text
positions/              the app's package
  app.py                the declaration: basis builds the Dash app from it
  data.py               rows, resources and every dataset the app reads
  domain.py             pure transforms: frames in, a frame out
  pages/overview.py     one module per page: its layout and its callbacks
  sql/positions.sql
  dev.py                the fakes `basis dev` runs on, read from fixtures/
fixtures/positions.csv  synthetic data, in git
tests/
```

The layers are modules basis knows by name, each importing only those below it, held by an import check as
basis-api's are: `pages` import `data` and `domain`; `data` declares and imports `domain`; `domain` imports no Dash,
no basis loader and nothing that does I/O, so a job could run its functions as they stand.

App code imports the part as `import basis.dash as bd`, as the ecosystem writes `dmc`, `dag` and `px`, and basis's
ruff settings hold the alias, so `bd` never collides with `dash` itself.

```python
# positions/data.py
class Position(bd.Row):
    account: str
    cusip: str
    sector: str
    par: Annotated[float, bd.format.money("USD", 0)]
    price: Annotated[float, bd.format.price(3)]
    yield_to_worst: Annotated[float, bd.format.pct(2)]
    oas: Annotated[float, bd.format.bps(0)]
    maturity: date


class Sector(bd.Row):
    sector: str
    market_value: Annotated[float, bd.format.money("USD", 0)]
    weight: Annotated[float, bd.format.pct(1)]


WAREHOUSE = bd.database("warehouse", deadline_s=30, max_concurrent=4)

POSITIONS = bd.query("positions", WAREHOUSE, "sql/positions.sql", Position, binds={"as_of": date}, ttl_s=900, stale_s=86_400)
SECTORS = bd.view("sectors", domain.by_sector, Sector, positions=POSITIONS)
```

```python
# positions/pages/overview.py
page = bd.page(
    "/",
    "Overview",
    filters=[bd.as_of("as-of", calendar="sifma-us")],
    body=[
        bd.card("By sector", bd.graph("by-sector")),
        bd.card("Positions", bd.grid("positions", Position, key="cusip", totals={"par": "sum"})),
    ],
)


@callback(Output("positions", "rowData"), Output("by-sector", "figure"), Input("as-of", "value"))
async def show(as_of: str) -> tuple[bd.Rows, bd.Figure]:
    day = bd.day(as_of)
    positions, sectors = await bd.load(POSITIONS, as_of=day), await bd.load(SECTORS, as_of=day)
    return bd.rows(positions), bd.charts.bar(sectors, Sector, x="sector", y="weight")
```

```python
# positions/app.py
app = bd.App(name="positions", title="Positions", settings=Settings, resources=[WAREHOUSE], pages=[overview.page], fakes="positions.dev:fakes")
```

The names above are the plan's proposal; the steps settle each as it is built, and the docs follow the code.

### Data

- **A dataset is declared, by name.** `bd.query` reads a SQL file from a declared database, binding only the params
  it declares, each with its type. `bd.file` reads a CSV, Parquet or Excel file from a declared folder, `bd.fetch`
  the JSON rows another service answers, and `bd.lake` Parquet from a lake path (15.7). Each names its row, and
  every frame is conformed to it at the boundary, by the rules a job's frames already follow: every number a
  float64 with NaN for missing, ids as text, dates typed, no `object` column.
- **Query by the few things that change what is read, and filter in a view.** A query binds an as-of date; the
  book, sector and maturity filters are pandas in a view, over the cached frame. Few distinct queries means the
  cache nearly always answers. No list binds, so no `IN (...)` built from input.
- **`await bd.load(DATASET, **params)` is the one way a page gets data.** It runs the query in the database's own
  bounded pool of threads, under its deadline, and answers from the cache when it can. It keeps the Arrow table,
  which nothing can change, and hands each caller a fresh frame.
- **A view is a transform, declared.** `bd.view(name, function, Row, **inputs)` names a pure function of datasets
  and params. basis loads its inputs, runs it off the event loop, conforms its result to its row, and caches it by
  its inputs' versions and its params, so three callbacks that show one summary compute it once.
- **Freshness is declared, never assumed.** Each dataset states `ttl_s`, and `stale_s`, how long a value past its
  TTL may still be shown when a reload fails. The page shows when its data was loaded, and says when it is stale.
- **One process per pod.** The cache is the process's own, bounded by a setting in megabytes and evicted least
  recently used first. An app scales by pods, each with its cache. A cache shared between pods waits until an app
  needs one (18).
- **An app reads.** A dataset's database gives a reader and no writer, so a Dash app cannot write, in any
  environment. Writes from a page are a later, explicit step (18).

### Display

- **One theme, from basis-ui's tokens.** One table of tokens in Python gives the CSS variables, the Mantine theme,
  the AG Grid theme and the Plotly templates, in light and dark, with Inter and its tabular figures and JetBrains
  Mono vendored. A pinned copy of basis-ui's generated `theme.css` is a fixture, and a test fails when the two
  drift, as the number formatter is held to `format.json`. A Dash app and a React app then read as one product.
- **The shell is basis's.** `bd.App` builds the header (the app's title, a badge naming the environment, so nobody
  mistakes QA for prod, the light and dark switch), the navigation from the declared pages, and the place errors
  show. `bd.page` builds a page's title, filter bar, freshness line and body.
- **The grid reads the row.** `bd.grid(id, Row)` builds the AG Grid from the row: headers, alignment, each
  column's format, a filter of the right kind, stable row ids, a pinned totals row (sums and weighted means,
  computed on the server), a quick filter, text that can be selected and copied, and CSV and Excel exports. A
  page overrides a column where it must, and never restates what the row declares.
- **Charts are Plotly, under one template.** `bd.charts` holds pure builders, a frame in and a figure out, for the
  charts a fixed income desk draws: a time series, bars and stacked bars, a scatter with a fitted line, a yield
  curve on a tenor axis, a waterfall, a heatmap and a histogram. Each takes its axis and hover formats from the
  row, skips weekends and holidays on a daily axis by the declared calendar, labels series at the line's end
  where there are few, and draws an empty state, not empty axes. A page may still build any Plotly figure; the
  template applies to it too.
- **States are one look.** Loading (shown only after a short delay, so a cached answer never flickers), empty,
  failed and stale look the same on every grid, graph and stat tile, and no page writes them.
- **Filters keep their state in the URL**, so a link reproduces a view. `bd.as_of` offers business days of the
  declared calendar and starts on the previous one; `bd.select` takes its options from a dataset's column.
- **Exports carry provenance.** An Excel export applies each column's format as Excel's own, and its second sheet
  says what was asked, as of when, and when it was loaded.

## Phase 13: the portfolio part goes

One step, one agent, on the main checkout. It ran beside 14.1.

**13.1 Remove the portfolio part.** It lives on as [trellis](https://github.com/jacobragsdale/trellis), a repo of its
own, so its copy here went whole: its package, its CI workflow and job, the script that checked nothing else needed
it, its import-contract hook, and every mention in the root `pyproject.toml` and the docs, with a relock. basis is
then APIs, batch jobs and, once phase 14 builds them, Dash apps.

- The rule it brought stays, said without it: core names no part, parts import core and never each other, and a
  mechanism only one part uses must be one a job could use as it stands.
- `docs/surface/0.1.0/` is a corpus v0.1.0's own code wrote, and stays byte for byte.
- One paragraph of history in `docs/design.md`, under [the packages](design.md#the-packages): where the part went,
  and the commits that fixed defects only in this copy, so that trellis can take them. This step as first written is
  in this page at `4a69721`.

Done when a case-insensitive `git grep` of the part's name finds only that history and the 0.1.0 corpus, the hooks
and unit tests pass, the lockfile has lost cvxpy, highspy, scipy and coptpy, and CI is green on `main`.

## Phase 14: foundations

One agent, in order. 14.1 may start beside phase 13, in a worktree.

**14.1 The spike.** Answers in `docs/spikes/dash.md`: the versions, a yes or no for each question, and the fallback
for each no. Scratch code stays out of the repo.

1. Does the app work under a path prefix, behind a proxy that strips it and one that does not: assets, callbacks,
   Pages and links?
2. Can basis ship its CSS, fonts and JavaScript from its own package (`dash.hooks.stylesheet` and `script`, or a
   mounted folder), fingerprinted, while an app's own `assets/` still works?
3. Can AG Grid 35's theming API be driven from Python with CSS variables as its values, so light and dark switch
   with no re-render? Can basis register a formatter in `dashAgGridFunctions` without replacing an app's own?
4. Can a figure's template follow the colour scheme in the browser, with no server round trip? What does a
   template cost in each figure's JSON, and can it be sent once?
5. Where is React 19 chosen so that it holds for every run, a test's among them? Do the `dcc` components basis
   uses (`Graph`, `Download`, `Location`, `Interval`, `Loading`, `Store`) and the dev tools work on it?
6. Does hot reload work under uvicorn on Linux, in WSL and on Windows?
7. Inside an async callback, do `dash.ctx` and `set_props` hold after an `await`, and inside the thread a load
   runs in? What bounds the threads: one limiter per database?
8. Which of Dash's internals does driving a page in-process touch? They go in one module, `basis.dash.internals`,
   held by a test, as `basis.api.internals` holds FastAPI's.
9. Do WebSocket and streaming callbacks work here, on this backend? (For phase 18; the firm's ingress is a
   **(work)** question.)
10. What does the stack add to an environment and an image, in megabytes? Is every wheel pure Python or built for
    Windows and Linux on 3.14?
11. A grid of 10,000 rows by 20 columns: its JSON bytes, its gzip bytes, the server's milliseconds and the
    browser's, measured once in headless Chromium.
12. DMC 3 on React 19, or DMC 2.8.1 on React 18? Go with 3 unless a question above finds a defect that only 2
    avoids, and say which.

**14.2 The design, written down.** A section "Dash apps: basis.dash" in `docs/design.md`, from this page and the
spike's answers; "A UI inside basis" in "Decided against" becomes a TypeScript UI inside basis, since a Dash app is
Python and the repo keeps one toolchain; the packages table, the verbs table, `README.md` and `AGENTS.md` follow.

**14.3 Rows in core.** A frame's row, `Row`, with `conform`, moves from `basis.job.rows` to `basis.core.rows`, and
a column's display format, basis-core's `Display`, becomes something a row's field can declare. basis-job exports
the names it always has, and its surface test passes unchanged. A Dash app's rows are then a job's rows, and a
third copy never starts.

**14.4 Serving in core.** What both servers need lives once, in `basis.core.serving`, taken from basis-api: the `serve`
settings, uvicorn's runner with its three-step shutdown, the stages of readiness, `/health/live` and `/health/ready`,
and the request boundary's ids, log fields and `traceparent`. The health routes take a path prefix, since a Dash app
behind a proxy that keeps its prefix is asked for them under it (14.1, question 1). uvicorn and Starlette come as an
extra, `basis-core[serve]`, which basis-api and basis-dash take. basis-api's own tests pass unchanged, which is the
proof. Token verification stays in basis-api until identity (18).

**14.5 The package and an empty app.** `packages/dash`: the distribution `basis-dash`, the module `basis.dash`, the
part `dash`. `bd.App`, its settings, `basis serve` and `basis dev` on core's serving. From 14.1:

- React 19 is chosen by `dash._dash_renderer._set_react_version("19.2.4")` when `basis.dash` is imported, through
  `basis.dash.internals`, with a test that the index names `react@19`.
- `bd.App` passes Dash every setting, so none of its 24 `DASH_*` variables changes one: `enable_mcp=False`,
  `shared_storage=None`, `compress=False`, `serve_locally=True`. `basis serve` refuses any `DASH_*` variable, and
  `_DASH_FASTAPI_CONFIG`, from which the FastAPI backend turns the dev tools on with no argument to stop it.
- One setting, the public path prefix, with whether the proxy strips it. Stripped, it is Dash's
  `requests_pathname_prefix` with `routes_pathname_prefix="/"`; kept, it is `url_base_pathname`, and basis's
  health routes move under it too.
- The dev tools are in `basis dev` alone, with `dev_tools_disable_version_check=True`, since the check calls
  plotly.com. `basis dev` runs uvicorn with `reload` itself and calls `enable_dev_tools` on the app it builds,
  rather than through `app.run`.
- Building the app calls `_setup_server()`, so setup errors such as a streaming callback with no shared storage
  fail the build, and not the first request.
- basis-dash depends on orjson.

The root's import contracts gain the part: parts import core and never each other. A repo that declares `api` and `dash`
is refused, in one line. The workspace's checks, CI and the Windows leg run its tests.

## Phase 15: data

Lane A. It runs beside phase 16, in a worktree of its own; the two write different modules.

**15.1 Declarations.** `bd.database`, `bd.query`, `bd.file`, `bd.view`, each named once, everything stated and
none of it guessed. Building the app opens every SQL file and checks its binds against the declared params, for
each dialect, before any environment is read, as step 9.5 did for jobs.

**15.2 The cache, and `bd.load`.** Off the loop, in the database's bounded pool; one query for callers that ask
together; a TTL; a ceiling in megabytes with least-recently-used eviction; Arrow kept and a fresh frame handed
out; the database's `max_rows`; a span and a log record for each load, with its dataset, params, rows,
milliseconds and whether the cache answered. A load runs in `anyio.to_thread.run_sync` under its database's
`anyio.CapacityLimiter(max_concurrent)`, never a bare executor, in which `dash.ctx` and `set_props` are lost (14.1,
question 7). A load called on the event loop's thread raises, and the message
ends with the fix: make the callback `async` and await it. Start with the failing tests: a 2-second fake query
must not delay another callback, and fifty callers at once make one query.

**15.3 Views.** Pure functions, run in a worker thread, cached by their inputs' versions and their params,
conformed to their row. Purity is the import check's to hold.

**15.4 Fixtures and fakes.** `basis dev` runs the app on `dev.py`'s fakes, read from `fixtures/`, a CSV per query,
and never mixes a fake with a real database. `bd.testing` gives a test the app in-process: build a page, fire a
callback by its inputs, read its outputs; a fake database that fails on demand, as basis-core's does. `basis dev
--env dev` serves on the real dev databases, read-only as ever.

**15.5 Failure.** One `on_error` for the app. A service error becomes a sentence a person can act on ("the
warehouse did not answer within 30 s"), shown as a notification with the request's id; the output keeps what it
showed. A dataset inside `stale_s` answers its last good value, marked stale. Every failure is logged once, with
its trace id.

**15.6 Refresh.** `bd.refresh(every_s=)` reloads a page's data on a timer, and a reload control drops a dataset's
entry, no more often than a declared floor, so a button cannot hammer a database. `bd.loaded` tells a page when
each dataset was read.

**15.7 More sources.** `bd.fetch`, with the logins step 12.2 built; `bd.lake`; an Excel sheet in a folder.

**15.8 Rows to the browser.** `bd.rows(frame)`: JSON-safe rows, fast, with NaN as null and dates as ISO text. Each
callback's response size is logged, and 17.1 holds it to a budget set from 14.1's measurements: 10,000 rows of 20
columns are 3.9 MB of JSON and about 100 ms on the server, and building the rows is the larger cost with orjson
(43 ms by `to_dict("records")`; a column-wise build was faster at 100,000 rows). Dash's `compress=True` is
gzip at level 9, four times the server's time; compression is the ingress's, or basis's own at level 1.

## Phase 16: display

Lane B.

**16.1 Tokens and theme.** The token table; the CSS variables, the Mantine theme, the AG Grid theme and the
Plotly templates made from it; the fonts; the drift test against basis-ui's `theme.css`. Load the `dataviz` skill
before choosing any chart colour. The mechanics, from 14.1 (questions 2 to 4):

- The tokens are CSS variables under `:root[data-mantine-color-scheme="light"]` and `…="dark"`. Mantine's own
  variables point at them from `html:root[data-mantine-color-scheme]`, and the primary colour also needs a Mantine
  palette made from the tokens. The switch sets the attribute on `<html>` in a clientside callback.
- The grid's theme is `themeQuartz.withParams({...})` with `var(--basis-…)` values, given as
  `dashGridOptions={"theme": {"function": ...}}`.
- A figure cannot hold a CSS variable. basis's template has transparent backgrounds and holds only what differs
  (under 0.5 KB a figure, against Plotly's 6.5 KB), and basis's CSS colours Plotly's text, grid lines, hover labels
  and legend over its inline styles, with `!important`. The colorway reads on both backgrounds.
- The CSS, the script and the fonts ship through `dash.hooks.stylesheet` and `script`. Each font is registered
  `dynamic: True` and named in the CSS in Dash's fingerprint form (`inter.vVERSIONmHEX.woff2`), so it is cached
  as the CSS is.

**16.2 The shell and the page.** `bd.App`'s shell, `bd.page`, `bd.card`, the layout grid, the 404 page, titles.

**16.3 One number, one text.** `bd.text(value, format)` on the server, from `basis.core.display`. For the grid, a
JavaScript formatter of the same kinds and the same rounding (15 significant digits, half away from zero, then the
decimals shown), held to `format.json`'s cases by a test that runs it under `node` in CI. A chart's hover text is
written on the server. The formatter is added to `window.dashAgGridFunctions`, never replacing it, under a `basis`
name: basis's script loads after an app's `assets/`, and a name both define is the later one's (14.1, question 3).

**16.4 The grid.** As [Display](#display) says, with a column's overrides: header, width, pin, hide, signed
colours for a profit or loss, a bar or a heat scale in the cell.

**16.5 Charts.** `bd.graph` and the builders in `bd.charts`, each a pure function tested on frames.

**16.6 Stat tiles and small parts.** `bd.stat`, with its value's format, a signed change and a sparkline; badges;
the freshness line; the icon subset.

**16.7 Filters and the URL.** `bd.as_of`, `bd.date_range` with presets (month, quarter and year to date),
`bd.select`, a text search; the filter bar; state in the query string.

**16.8 States.** Loading, empty, failed and stale, applied by the grid, graph and stat wrappers.

**16.9 Exports.** Excel through basis-core's workbook writer, with formats and the provenance sheet; CSV with the
guard against formula injection.

One screenshot per step, where the eye is the only judge, looked at and not kept as a baseline.

## Phase 17: the repo around it

One agent, once both lanes are on `main`.

**17.1 The checks.** Each problem is one `ERROR` line with its fix: the layers; every callback's inputs, outputs
and states name a component and a property the page has; every page builds and every callback runs on the
fixtures, from the layout's first values, and answers what its outputs take (a valid figure, rows of the grid's
columns); a callback that holds the event loop longer than a budget; a URL of another origin anywhere in a page,
`dash_ag_grid.themes`' CDN links among them;
a dataset in a `dcc.Store`; `dash_table.DataTable`. The coverage floor applies to `domain.py`.

**17.2 The agent's eyes.** `basis render PAGE [--set ID.PROP=VALUE]`: the page's callbacks run in the process, on
the fakes or an environment, and each output is summarised as JSON: a grid's row count, columns and first rows as
shown, a figure's traces, points and axes, a text. `basis load DATASET [--param ...]` prints a dataset's first
rows and its timings.

**17.3 New repos and their files.** `uvx basis-dash new app NAME`; `basis new page NAME`; what `basis sync`
writes: the Dockerfile's entry, `.env.example`, the README, the part's block of `AGENTS.md`, the skills (add a
page, add a dataset, why did it fail), and the docs `basis docs` prints.

**17.4 Smoke.** `basis smoke --env ENV` for a Dash app: health and readiness, every page answers, and every
dataset that declares a smoke example loads.

**17.5 The example.** The packaged example is a fixed income holdings app on synthetic fixtures, rich enough to
show the whole kit: an overview (stat tiles, allocation, top holdings), positions (the full grid, filters,
totals, export), a curve, and a profit and loss waterfall. It is what `new app` starts from, cut down, what the
tests exercise, and what the maintainer is shown, served on the tailnet.

**17.6 Measure.** On the development host: payload and time for grids of 1,000 to 100,000 rows; fifty sessions
with a slow query behind one of them, and what the others wait; the cache's memory. The numbers go in
`docs/design.md`, and set 17.1's budgets.

**17.7 CI.** The Windows leg; a generated-repo job that makes an app, checks it, commits through its hook,
builds its image, serves it and smokes it.

**17.8 Release.** `v0.2.0`, with basis-dash published and its surface recorded. The tag waits for the
maintainer's word.

## Phase 18: later

Not built in this push. Each needs a decision, the firm's environment, or proof that an app needs it.

- **Identity.** Token verification moves from basis-api to core; a callback asks for the user; a query may bind
  the user, and its cache key carries it. How the firm's sign-in reaches a server-rendered page, a header from
  the gateway or a cookie, is a **(work)** question.
- **Live updates.** WebSocket or streaming callbacks for values that tick. 14.1 found both work on this backend.
  A stream over HTTP needs Dash's `shared_storage` (one pod's `LocalSharedStorage`, or Redis across pods) and a
  signing secret every pod shares. A WebSocket callback (`websocket=True`) needs neither, but needs
  `websocket_allowed_origins`. Whether the firm's ingress passes WebSocket upgrades and long responses is a
  **(work)** question.
- **Rows on the server.** AG Grid's infinite row model over a cached dataset, with the sort and filter done in
  pandas, for tables too large to send whole.
- **Writes from a page**, declared per table, in a transaction.
- **`basis shot PAGE`**, one picture from the desktop's own Chrome or Edge, where an agent must see the page.
- **parallax as a Dash component**, built in basis-ui and published as a wheel: Arrow straight to its grid, a
  million rows, pivots, and Excel export in the browser.
- **Regress for pages**: recorded datasets replayed through every page, listing the outputs a change moved.
- **A cache shared between pods**, and background callbacks for a query that takes minutes.

## Open decisions

Each has the pick this plan builds on. None blocks a step before the one named.

| Decision | Pick | Step |
|---|---|---|
| DMC 3 on React 19, which Dash 4.5 calls experimental, or DMC 2.8.1 on React 18, Mantine 8's last line | DMC 3: it rendered clean here, and starting on 2 means a migration in every app within months. 14.1 found no open defect that DMC 2 avoids, and DMC 2.x has had no release since 2.8.1 | Settled by 14.1 |
| Dash on FastAPI and uvicorn with awaited loads, or on Flask with a thread per request | FastAPI: one server for APIs and apps, on Windows too, and the road to live updates. The cost is that loads must be awaited, which basis enforces | 14.5 |
| AG Grid Community, or Enterprise if the firm holds a licence (row grouping, pivots, range copy, Excel export in the browser) | Community; basis does totals and Excel on the server. An Enterprise key would be a setting, later | 16.4 |
| May a repo hold an API and a Dash app? | No: one served thing per repo | 14.5 |
| Do `Row` and serving move into core, which touches basis-job and basis-api? | Yes: a second copy of each would drift, and their tests prove the move | 14.3, 14.4 |
| Does the look follow basis-ui's theme? | Yes, held by a drift test | 16.1 |
| Does `v0.2.0` publish basis-dash? | Yes, on the maintainer's word | 17.8 |
| The design's "Decided against" row "Shared datasets across packages", whose one reason was the removed part | Removed with it; a Dash app's datasets are 15.1's to decide | 13.1 |
| A part that gives no share of the tool settings (`AgentFiles.tools` of `None`), which only the removed part used | Kept: it is general, any part may give none, and basis then leaves the repo's own settings alone | 13.1 |
| PLAN.md's findings and figures about the removed part | Kept as history, naming it "the third part"; its full text is PLAN.md at `4a69721` | 13.1 |

## Rules for this work

- The maintainer's working mode stands: commit to `main` and push after every step, no pull requests, no agent
  trials, screenshots only where the eye is the only judge.
- Every step starts with its failing test where it fixes or guards behaviour.
- A decision a step takes goes into `docs/design.md` in the step's own commit, and into the agent's report.
- The repo is public: "the firm", placeholder names, synthetic data.
- The host's memory rules are `~/.cache/basis-agents/RULES.md`.
