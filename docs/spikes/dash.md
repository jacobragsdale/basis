# Spike 14.1: Dash on basis

Can a Dash app run the way [the Dash plan](../dash-plan.md) builds one: on Dash's FastAPI backend under uvicorn,
React 19, DMC 3, AG Grid and Plotly, behind the firm's proxy, themed from basis-ui's tokens, and driven in the process
by tests? Yes, to all twelve questions, with one fallback for a part of question 4 and one setting for each of
questions 1, 5 and 9 that the plan did not have. Run on 2026-11-13 in a scratch app outside the repo. Each browser
check was one page in headless Chromium, through `heavy`. Nothing was installed into the workspace.

## Versions

Python 3.14.7 on Debian 13 (Linux 6.12, x86_64); dash 4.5.0 (dash-renderer 3.5.3) with React 19.2.4, its opt-in;
dash-mantine-components 3.0.0 (Mantine 9.7); dash-ag-grid 35.3.0; plotly 7.1.0; fastapi 0.162.5, starlette 1.10.0,
uvicorn 0.54.0 with uvloop 0.23.0, watchfiles 1.3.0 and websockets 17.4; anyio 4.16.0; pandas 3.1.1, pyarrow 26.0.1;
orjson 3.13.0, added for question 11. Headless Chromium 158.0.8127.18 through basis-ui's playwright-core 1.65.0. Colours
are from basis-ui's generated `packages/ui/src/styles/theme.css` (`:root` and `.dark`), and the font is its
`inter-latin-wght-normal.woff2`.

## Answers

| # | Question | Answer | How, or the fallback |
|---|---|---|---|
| 1 | Path prefix, behind a proxy that strips it and one that does not | Yes | See [1](#1-path-prefix) |
| 2 | basis's CSS, fonts and JavaScript from its package, fingerprinted, beside an app's `assets/` | Yes | `dash.hooks.stylesheet` and `dash.hooks.script` ([2](#2-basiss-own-css-fonts-and-javascript)) |
| 3 | AG Grid's theme from CSS variables; a formatter beside an app's own | Yes, both | `dashGridOptions={"theme": {"function": "themeQuartz.withParams({...})"}}` ([3](#3-the-grid-follows-the-scheme)) |
| 4 | A figure that follows the scheme with no round trip; a template's cost; sent once | Yes; 374 bytes; no | A CSS variable in a figure is refused, so CSS over Plotly's styles does it. The template cannot be sent once, so it is kept small ([4](#4-figures-follow-the-scheme)) |
| 5 | Where React 19 is chosen; `dcc` and the dev tools on it | Yes | `dash._dash_renderer._set_react_version("19.2.4")` at basis.dash's import. The dev tools' version check reaches another origin, so basis turns it off ([5](#5-react-19)) |
| 6 | Hot reload under uvicorn | Yes on Linux. WSL and Windows unverified | uvicorn's `--reload` and Dash's own asset watcher ([6](#6-hot-reload)) |
| 7 | `ctx` and `set_props` after `await` and in a load's thread; what bounds threads | Yes, except in a bare executor | `anyio.to_thread.run_sync(fn, limiter=CapacityLimiter(n))`, one limiter per database ([7](#7-context-and-threads)) |
| 8 | Dash internals that in-process driving touches | Eight, listed | [8](#8-the-internals) |
| 9 | WebSocket and streaming callbacks on this backend | Yes, both | Streaming over HTTP needs `shared_storage` and, across pods, a signing secret ([9](#9-websocket-and-streaming-callbacks)) |
| 10 | Megabytes added; wheels for Windows and Linux on 3.14 | 128 MB; yes | Every added wheel is pure Python or built for cp314 on both platforms. uvloop has no Windows wheel, and uvicorn's marker skips it there ([10](#10-size-and-wheels)) |
| 11 | A 10,000 × 20 grid | 3.90 MB JSON, 1.33 MB gzip, about 100 ms on the server, 116 ms click to rows in the browser | [11](#11-a-10000-by-20-grid) |
| 12 | DMC 3 or 2.8.1 | DMC 3 | No open defect that only DMC 2 avoids ([12](#12-dmc-3-or-dmc-2)) |

### 1. Path prefix

Both proxy shapes work in the browser: assets, callbacks, Pages, `dcc.Link` navigation forward and back, a deep link
to `/apps/pos/other`, the packaged CSS, font and script, a download, and no failed request. The proxy was 25 lines of
Starlette and httpx in front of uvicorn, on `/apps/pos/`.

- **Proxy strips the prefix**: `Dash(requests_pathname_prefix="/apps/pos/", routes_pathname_prefix="/")`. The server
  answers at `/`, and the page asks for everything under `/apps/pos/`.
- **Proxy keeps it**: `Dash(url_base_pathname="/apps/pos/")`. Both prefixes are then `/apps/pos/`.
- Pages and links use `dash.get_relative_path("/other")`. `dcc.Location.pathname` is the browser's path with the
  prefix (`/apps/pos/other`), and Pages strips the prefix before it matches a page.
- **Found:** routes basis adds to the FastAPI server, such as `/health/live`, stay at the server's root. Behind a proxy
  that keeps the prefix, `/apps/pos/health/live` is a 404. basis must mount its own routes under the same
  `routes_pathname_prefix` (14.4, 14.5).

### 2. basis's own CSS, fonts and JavaScript

```python
dash.hooks.stylesheet([
    {"relative_package_path": "static/basis.css", "namespace": "basis.dash"},
    {"relative_package_path": "static/fonts/inter.woff2", "namespace": "basis.dash", "dynamic": True},
])
dash.hooks.script([{"relative_package_path": "static/basis.js", "namespace": "basis.dash"}])
```

- Dash serves each file at `_dash-component-suites/NAMESPACE/static/basis.v0_2_0m1794577305.css`, fingerprinted with
  the package's `__version__` and the file's mtime, with `Cache-Control: public, max-age=31536000`. The namespace
  must be an importable module with a `__version__`; a dotted one, as `basis.dash` will be, worked the same in a
  two-level scratch package.
- The app's own `assets/` still loads beside them: its CSS applied (a probe element's outline was the app's red), and
  its `dashAgGridFunctions.js` ran.
- **A file that CSS names, such as a font, must be registered as `dynamic: True`**, or Dash refuses it (500, not 404).
  CSS reaches it by a relative URL, `url("fonts/inter.woff2")`. A file registered that way is served with an ETag
  only. CSS that names it in Dash's fingerprint form, `url("fonts/inter.v0_2_0m1a2b.woff2")`, gets the year-long cache
  too, since Dash strips any `vVERSIONmHEX` part before it reads the file. basis's build can write that name.
- Order in the page: the app's `assets/` scripts load first, then the hooks' scripts.

### 3. The grid follows the scheme

```python
dag.AgGrid(..., dashGridOptions={"theme": {"function": (
    "themeQuartz.withParams({backgroundColor: 'var(--basis-card)', foregroundColor: 'var(--basis-foreground)',"
    " borderColor: 'var(--basis-border)', headerBackgroundColor: 'var(--basis-muted)',"
    " accentColor: 'var(--basis-primary)', fontFamily: 'Inter Tabular, sans-serif'})")}})
```

- dash-ag-grid 35 calls a `theme` given as a function, and the namespace it evaluates in has `themeQuartz`,
  `themeAlpine`, `themeBalham` and `themeMaterial`. AG Grid writes each parameter into its own CSS variable, so
  `var(--basis-card)` is resolved by the browser.
- Switching the scheme changed the grid's background from `oklch(1 0 255)` to `oklch(0.198 0.0084 255)` and its text
  from `oklch(0.215 0.0126 255)` to `oklch(0.955 0.0126 255)`. The grid's root element was the same node before and
  after, so nothing re-rendered.
- **Formatters:** basis's script does `var f = (window.dashAgGridFunctions = window.dashAgGridFunctions || {});
  f.basisMoney = ...`, and the app's asset uses the pattern dash-ag-grid documents. Both functions were there
  (`appUpper,basisMoney`), and the grid showed `912828X1` and `1,000,000` through them. basis's script loads after the
  app's, so it must add to the object, never replace it, and it must prefix its names (`basis…`), since a name it shares
  with the app wins.
- `dash_ag_grid.themes` (`QUARTZ` and the others) are jsdelivr URLs. basis never uses them.
- **The switch** sets `data-mantine-color-scheme` on `<html>` in a clientside callback. It changes only the DOM: no
  server request, and no prop of the provider changes. The tokens are CSS variables under
  `:root[data-mantine-color-scheme="light"]` and `…="dark"`, and Mantine's variables point at them from
  `html:root[data-mantine-color-scheme] { --mantine-color-body: var(--basis-background); … }`. The selector needs that
  much specificity to beat the variables Mantine injects. The body, the text and the light primary followed.
  **Found:** Mantine's filled button in dark kept Mantine's own blue (`rgb(24, 100, 171)`), so the primary colour
  needs a Mantine palette built from the tokens, as well as the variables (16.1).

### 4. Figures follow the scheme

- **A CSS variable inside a figure is refused.** `paper_bgcolor: "var(--basis-card)"` (sent as a plain dict, past
  plotly.py's validation) drew white in both schemes, because Plotly.js parses colours itself.
- **What works:** a template with transparent backgrounds (`paper_bgcolor` and `plot_bgcolor` `rgba(0,0,0,0)`), and
  rules in basis's CSS over the styles Plotly writes inline:
  `.js-plotly-plot .main-svg text { fill: var(--basis-foreground) !important; }` and
  `.js-plotly-plot .gridlayer path, .js-plotly-plot .zerolinelayer path { stroke: var(--basis-edge) !important; }`.
  Axis text followed the switch (`oklch(0.215 …)` to `oklch(0.955 …)`) with no request and no new figure. Hover
  labels, the legend's background and the modebar need rules of their own, which this spike did not write (16.1).
  Series colours stay the same in both schemes, so the palette must read on both backgrounds (the `dataviz` skill's
  validator, 16.1).
- **Cost:** on a three-bar figure, Plotly's default template adds 6,458 bytes, DMC's `mantine_light` 6,876, and a
  basis template that holds only what differs (transparent backgrounds, font, colorway, margins, axis lines) 374.
- **Sent once: no.** Plotly.js has no default template for a page, and `dcc.Graph` has no prop for one, so each
  figure carries its template. **Fallback:** keep basis's template to what differs, under 0.5 KB, and set it as
  `plotly.io.templates.default` so a page's own figures get it too.

### 5. React 19

- **Where:** `dash._dash_renderer._set_react_version("19.2.4")`, called when basis.dash is imported. Dash reads the
  choice each time it renders the index page, so the call holds for any app in the process, a test's `TestClient`
  among them. It also holds when made after `Dash()` is built, and over `REACT_VERSION=18.3.1` in the environment,
  which Dash reads when it is imported. The index then names `deps/react@19…2.4.min.js`. Dash's default is 18.3.1, and
  its CHANGELOG for 4.5.0 calls the opt-in experimental. The function is private, so it goes in
  `basis.dash.internals`.
- **dcc on React 19**, in the browser with no console error or warning: `Location` (pathname under both prefixes),
  `Interval` (three ticks, then stops at `max_intervals`), `Store` (data into a callback), `Download` (the browser
  received `spike.csv` from `dcc.send_string`), `Loading` (wrapped a 0.3 s async callback), and `Graph`.
- **Dev tools** (`enable_dev_tools(debug=True)`): the dev bundles load (`react@19…2.4.js`), and the debug menu renders.
  **Found:** they fetch `https://dash-version.plotly.com:8080/current_version` on load, a request to another origin,
  which the firm's network would refuse with a console error. `dev_tools_disable_version_check=True` stops it, and the
  console was then clean.

### 6. Hot reload

- **Linux, yes**, measured in the browser with uvicorn's `--reload` and `app.enable_dev_tools(debug=True,
  dev_tools_hot_reload=True, dev_tools_disable_version_check=True)`. An edit to `assets/app.css` showed in 2.9 s with
  no page reload (a marker on `window` survived). An edit to a page's Python showed in 3.3 s: uvicorn restarted the
  process, the renderer saw the new `_reload-hash` and fetched the layout again.
- There are two watchers. uvicorn's (watchfiles) restarts the process for Python. Dash's own (`dash/_watch.py`, a
  thread that polls mtimes with `os.walk` every 0.5 s) covers `assets/` and every component package's folder.
  `app.run(debug=True)` on this backend starts `python -m uvicorn MODULE:VAR.server --reload` in a subprocess and
  passes the dev tools through `_DASH_FASTAPI_CONFIG`. `basis dev` should run uvicorn itself and call
  `enable_dev_tools` on the app it builds instead.
- **WSL, unverified:** watchfiles switches itself to polling when it detects WSL, which catches edits made from
  Windows to a repo on `/mnt/c`. Dash's watcher polls anyway.
- **Windows, unverified:** watchfiles has a cp314 `win_amd64` wheel, uvicorn's reloader spawns its worker with
  `multiprocessing`, which works there, and Dash's watcher is plain `os.walk`. Step 17.7's Windows leg proves it.

### 7. Context and threads

An async callback that read `ctx.triggered_id` and called `set_props` at each point, on uvicorn:

| Where | Thread | `ctx.triggered_id` | `set_props` |
|---|---|---|---|
| Before the first `await` | `MainThread` | `go` | ok |
| After `await asyncio.sleep` | `MainThread` | `go` | ok |
| In `asyncio.to_thread` | `uvloop_0` | `go` | ok |
| In `loop.run_in_executor(pool, …)` | `ThreadPoolExecutor-0_0` | `LookupError` | `LookupError` |
| In `anyio.to_thread.run_sync(…, limiter=…)` | `AnyIO worker thread` | `go` | ok |

- `set_props` calls from the worker threads reached the response's `sideUpdate`. Dash keeps the callback's context
  in a `ContextVar`, which `asyncio.to_thread` and anyio copy into the thread and a bare executor does not.
- **What bounds the threads:** one `anyio.CapacityLimiter(max_concurrent)` per database, passed to
  `anyio.to_thread.run_sync`. Three 0.5 s loads at once under `CapacityLimiter(1)` finished at 0.5, 1.0 and 1.5 s,
  while `/health/live` answered in 3 ms.
- **The plan's finding holds:** a callback that is not `async` ran on `MainThread`, and a `time.sleep(1)` in it held
  a concurrent `/health/live` for 0.9 s.

### 8. The internals

A 50-line driver built page `/` from its layout and fired every callback whose inputs the page has, from the
layout's first values, through `TestClient`. It touched these, which go in `basis.dash.internals`:

1. `dash._dash_renderer._set_react_version` (question 5).
2. The wire format of `POST /_dash-update-component`: `output` (`"id.prop"`, or `"..a.b...c.d.."` for several),
   `outputs`, `inputs` and `state` (each `{"id", "property", "value"}`), and `changedPropIds`. Its answer is
   `{"multi": true, "response": {ID: {PROP: VALUE}}, "sideUpdate": {...}}`, or 204 when nothing updates.
3. `GET /_dash-dependencies`: each callback's `output` string, `inputs`, `state`, `clientside_function` and
   `prevent_initial_call`.
4. `Component._traverse()`, to walk a layout, and `Component.to_plotly_json()["props"]`, to read a component's first
   values.
5. `dash._utils.stringify_id`, for a pattern-matching id, which the wire format writes as sorted JSON.
6. `Dash._setup_server()`, which Dash runs on the first request. It is where setup errors are raised, such as
   question 9's `StreamCallbackError`, so `basis check` and `bd.testing` must call it rather than wait for a
   request.
7. `dash.page_registry`, which is public, and each page's `layout`, a callable that takes the query string.
8. `fastapi.testclient.TestClient` (Starlette's), which warns that it wants `httpx2` rather than `httpx`, as basis-api
   already arranges.

Every callback ran in 1 to 3 ms in the process, and the async one in its 300 ms sleep.

### 9. WebSocket and streaming callbacks

Both work on this backend, in the browser, with every frame shown in order:

- **Streaming over HTTP**: an `async def` callback that `yield`s. Dash infers the stream; there is no keyword. It
  needs `Dash(shared_storage=dash.LocalSharedStorage())`. Without it, `_setup_server` raises `StreamCallbackError`,
  but only at the first request (a 500), so a check must build the server. The browser posted
  `/_dash-update-component?endId=…&downlinkId=…` through a stream worker.
- **WebSocket**: `@callback(..., websocket=True)` on one callback, or `Dash(websocket_callbacks=True)` for all. One
  socket at `/_dash-ws-callback`, opened by a web worker (`dash-ws-worker.js`). A plain answer and a stream of five
  frames both arrived over it, and a stream over the WebSocket does not need shared storage.
- **What phase 18 pays:**
  - `shared_storage`: `LocalSharedStorage` for one pod, or `RedisSharedStorage` or `DiskcacheSharedStorage` across
    pods.
  - A signing secret every pod shares (`server.secret_key`, or `DASH_SECRET_KEY`), since streams are signed for each
    page load.
  - `websocket_allowed_origins` behind the firm's gateway.
  - An ingress that passes WebSocket upgrades and long responses (a **(work)** question). Dash sends a keepalive line
    every 15 s (`stream_keepalive_interval`) for proxies' idle timeouts.
- **Found:** `DASH_SHARED_STORAGE` and `DASH_SECRET_KEY` in the environment change what Dash does, as
  `DASH_MCP_ENABLED` would. They are two of the 24 `DASH_*` variables Dash reads (`dash/_configs.py`), among them
  `DASH_URL_BASE_PATHNAME`, `DASH_COMPRESS`, `DASH_DEBUG` and `DASH_SERVE_DEV_BUNDLES`. An argument passed to `Dash()`
  wins over its variable. But the FastAPI backend also calls `enable_dev_tools` from the JSON in
  `_DASH_FASTAPI_CONFIG` on its first request (`_initialize_dev_tools`), which no argument overrides, so a stray
  variable would turn the dev tools on in production. basis passes every setting explicitly, `shared_storage=None`
  among them until phase 18 declares it. `basis serve` refuses to start while any `DASH_*` variable or
  `_DASH_FASTAPI_CONFIG` is set.

### 10. Size and wheels

Measured as two fresh environments on Python 3.14: basis-core with basis-api, then the same with
`dash[fastapi]==4.5.0`, `dash-ag-grid==35.3.0`, `dash-mantine-components==3.0.0` and `plotly==7.1.0`. site-packages
went from 360 to 488 MB, **128 MB more**. The 24 added distributions total 139 MB by their `RECORD`s: plotly 40.4,
dash 39.2, dash-ag-grid 24.6, uvloop 16.2, dash-mantine-components 7.6, setuptools 3.0, narwhals 2.1, httptools 1.7,
watchfiles 1.2, and the rest under 1 MB each. 33 MB of it is JavaScript source maps (`*.map`), which an image could
leave out. An image grows by the same amount, as its environment is the same. No image was built.

- Every added wheel is pure Python, or built for cp314 on both `win_amd64` and `manylinux` x86_64 (httptools,
  markupsafe, watchfiles). The exception is uvloop, which has no Windows wheel, and uvicorn's `standard` extra
  installs it only where `sys_platform != 'win32'`. Checked against PyPI's file lists for each version; the Windows
  install itself is unverified until 17.7.
- `dash[fastapi]` brings `uvicorn[standard]` (uvloop, httptools, watchfiles, websockets). Dash itself still requires
  Flask, Werkzeug, setuptools and `requests` on this backend.

### 11. A 10,000 by 20 grid

Twenty columns: four text, a date as ISO text, eight floats at full precision and seven at four decimals, from a
seeded generator. Served by uvicorn on localhost; plotly's encoder (Dash's `to_json`) uses orjson when it is
installed.

| Rows | JSON | gzip, level 1 / 6 / 9 | `to_dict("records")` | Dash's encoding, json / orjson | Server answer, plain / Dash's `compress=True` | Browser: click to rows shown (request, then after) |
|---|---|---|---|---|---|---|
| 1,000 | 0.39 MB | – / 0.14 / 0.13 MB | 5 ms | 5 / 1 ms | 16 / 46 ms | 49 ms (12, 32) |
| 10,000 | 3.90 MB | 1.61 / 1.36 / 1.33 MB | 43 ms | 47 / 9 ms | 101 / 444 ms | 116 ms (63, 51) |
| 100,000 | 38.98 MB | 16.04 / 13.63 / 13.33 MB | 455 ms | 464 / 81 ms | 1,031 / 4,500 ms | 986 ms (626, 358) |

- The browser column is from a click to AG Grid's `getDisplayedRowCount()` reaching the row count with a row drawn,
  measured in the page with orjson installed. A second 10,000-row load took 141 ms.
- **Dash's `compress=True` is too slow to use.** It is Starlette's `GZipMiddleware` at level 9: 340 ms to compress
  10,000 rows, against 25 ms at level 1, for a result 21% larger. Leave compression to the ingress, or use
  basis's own middleware at level 1.
- **orjson cuts encoding five-fold**, so basis-dash depends on it. Then building the rows is the larger cost. A
  column-wise build (`zip` over each column's `tolist()`) took 199 ms against 455 ms at 100,000 rows; `bd.rows`
  (15.8) picks the fastest that keeps NaN as null.
- Budgets this page proposes for 17.1: a callback's answer over 4 MB (about 10,000 rows of 20 columns) is one warning
  line. 100,000 rows needs the server-side row model (18).

### 12. DMC 3 or DMC 2

**DMC 3.0.0 on React 19.** The scratch app rendered the Mantine shell, AG Grid, Plotly and every `dcc` component above
with no console error or warning. A lookup of the trackers on 2026-11-13 (by keyword, through GitHub's issue search)
found no open defect that DMC 2 or React 18 would avoid:

- **Dash** [#3231](https://github.com/plotly/dash/issues/3231), React 19 support: still open as a tracker. The
  support landed in [#3880](https://github.com/plotly/dash/pull/3880), and CI now runs the main, html and dcc tests
  on both React versions.
- **Dash** [#4121](https://github.com/plotly/dash/issues/4121), open: since 4.2.0 a callback that returns large
  `children` dispatches one store update per descendant, up to twice as slow as 4.4.1. This is the same on either
  React. Fixed-shape layouts whose callbacks return data, as basis's are, avoid it.
- **Dash** [#4119](https://github.com/plotly/dash/issues/4119): `dash_canvas`, which is archived, breaks on React 19.
  Any component outside the stack must be checked on React 19 before an app takes it.
- **DMC:** none of the nine open issues concerns AppShell, MantineProvider, the colour scheme, Select, the date pickers
  or notifications. [#743](https://github.com/snehilvj/dash-mantine-components/issues/743), open, is a MantineProvider
  interaction-latency regression from the 2.x era.
  [#734](https://github.com/snehilvj/dash-mantine-components/issues/734) is DatePicker keyboard navigation.
- **DMC 2.x** stops at 2.8.1 (2026-10-22), on Mantine 8, with no 2.x branch, so a defect there would get no fix.
- DMC 3's breaking changes are listed in its [migration guide](https://www.dash-mantine-components.com/migration),
  such as `color` on `Text` becoming `c`, `Grid.gutter` becoming `gap`, and `defaultRadius` becoming `md`. They bind
  basis only as it writes its shell (16.2).

## What this changes

The steps of [the plan](../dash-plan.md) this commit edits, each in its own words there:

- **14.4, 14.5:** basis's routes (`/health/live`, `/health/ready`) sit under the app's `routes_pathname_prefix`. One
  setting, the public prefix, and whether the proxy strips it, map to Dash's two prefixes (question 1).
- **14.5:** what `bd.App` passes Dash: `_set_react_version("19.2.4")` at import; `enable_mcp=False`,
  `shared_storage=None` and `compress=False`, each explicit so no variable changes them; `serve_locally` left on;
  `dev_tools_disable_version_check=True` with the dev tools in `basis dev`. `basis dev` runs uvicorn with `reload` and
  calls `enable_dev_tools` itself. `basis serve` refuses `DASH_*` variables and `_DASH_FASTAPI_CONFIG`. The server is
  set up (`_setup_server`) when the app is built. orjson is a dependency.
- **15.2:** a load runs in `anyio.to_thread.run_sync` under its database's `CapacityLimiter`, never a bare executor,
  so `ctx` and `set_props` hold in it.
- **The stack:** orjson joins it.
- **15.8, 17.1:** this page's numbers are the first budgets. Compression is level 1, or the ingress's.
- **16.1:** the mechanics of the theme: tokens keyed on `data-mantine-color-scheme`, Mantine's variables pointed at
  them, a Mantine primary palette from the tokens, AG Grid's `themeQuartz.withParams` with `var()`, and Plotly
  through a transparent template and CSS. Fonts are named in fingerprint form.
- **16.3:** basis's grid functions are added to `window.dashAgGridFunctions`, never replacing it, under `basis`
  names.
- **17.1:** a URL of another origin includes `dash_ag_grid.themes`' CDN links. The dev tools' version check is off
  (14.5).
- **18, live updates:** what each transport needs, from question 9.
- **Open decisions:** DMC 3 is settled.
