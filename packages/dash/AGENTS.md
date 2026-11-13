# Agent guide

This package is basis-dash, which Dash app repos depend on. An app declares its name, its title, its settings, its
resources and its pages as a `bd.App` (`import basis.dash as bd`), and basis builds the Dash app around them, on Dash's
FastAPI backend, the same in every repo; an app never makes `dash.Dash` itself. The plumbing it runs on is basis-core's,
in `packages/core`, which imports nothing from here: the rows (`basis.core.rows`), the server, health, readiness and the
request boundary (`basis.core.serving`), settings and environments, logs and traces.
[The design](../../docs/design.md#dash-apps-basisdash) holds the decisions, [the Dash plan](../../docs/dash-plan.md) the
steps, and [the spike](../../docs/spikes/dash.md) the facts about Dash 4.5, DMC 3, AG Grid 35 and Plotly 7 they rest on.
Those versions are newer than an agent's training: check a property, hook or argument against the installed package's
source before relying on it.

## Where things are

Paths are under `src/basis/dash/`. The foundations are step 14.5's; after it two lanes build at once, phase 15 (data)
and phase 16 (display), each in modules of its own, so they never edit one file.

| Path | Holds | Whose |
|---|---|---|
| `__init__.py` | The names `bd` gives: the foundations', then the lanes' from `_data.py` and `_display.py`, each lane's `__all__` added to the package's; `bd.build`, `bd.serve` and `bd.testing` load on first use, so declaring an app loads no server. Importing it chooses React 19 and registers basis's files. | Foundations |
| `_data.py`, `_display.py` | Each lane's names: it imports each from the module that defines it and lists it in `__all__`. Only its lane edits it. | Data, display |
| `app.py` | `App`, the declaration, checked when it is made, and the module that declared it. | Foundations |
| `pages.py` | `Page` and `bd.page(path, title, layout=...)`, and the module that declared it, whose callbacks are the page's; 16.2 adds its filters and body. | Display, from 16.2 |
| `settings.py` | `Settings`, basis-core's, with `serve`: core's `serving.Serve` and the path prefix, `prefix` and `prefix_stripped`; `smoke`, where `basis smoke` reaches the deployed app; `Settings.given`, for tests; `unreadable`. | Foundations |
| `ids.py` | The fixed ids of the chrome both lanes build against: `NOTIFICATIONS`, DMC's notification container, and `STATUS`, a store of what is known of each dataset's last load. Its docstring says what each prop holds. | Foundations |
| `errors.py` | `DeclarationError`, and `on_error`, the one handler of every callback's error, which logs it once, keeps each output as it was, and shows its sentence in the notifications. | Data, from 15.5 |
| `assets.py`, `static/` | The one place basis registers its CSS, fonts and scripts with Dash's hooks, and the files themselves; 16.1 and 16.3 add theirs. | Display |
| `shell.py` | `chrome`, what every page shows inside: Mantine's provider, the two fixed ids, Dash Pages' container; 16.2 builds the shell here. | Display |
| `building.py` | `build`: the Dash app from its declaration, its settings, every setting Dash would read from a variable given, its prefixes, its pages and callbacks, core's health routes, request boundary and telemetry, and its lifespan. | Foundations; data adds its resources' opening in 15.2 |
| `internals.py` | Every private name of Dash's basis touches, held by `tests/test_internals.py`: React's choice, the callbacks of each app's own modules, Pages' registry, setting the server up, the wire format. | Foundations; a lane that needs another adds it here, with its test |
| `server.py` | `serve` on core's runner, `dev` on uvicorn's reloader with Dash's dev tools, and the refusal of `DASH_*` variables. | Foundations |
| `cli.py` | `part`: `basis serve [APP] --env ENV`, `basis dev [APP] [--env ENV]`, `basis render PAGE [--set ID.PROP=VALUE]... [--env ENV]` and `basis load DATASET [--param NAME=VALUE]... [--env ENV]`, and the part's checks. | Foundations; 17.x adds verbs |
| `driving.py` | An app driven as a browser drives it, in the process: a page's components inside the chrome, its first values, the callbacks its load calls in the browser's order, each call's answer, failures and hold on the loop; `checks` and `render` share it. | The repo's, from 17.2 |
| `render.py` | `basis render` and `basis load`: what a page shows, and what a dataset holds, as JSON. Its docstring is the reference. | The repo's, from 17.2 |
| `smoke.py` | `basis smoke --env ENV`, the part's `smoke`: the app at `BASIS_SMOKE__URL` ready, at the commit, every page answering, and each dataset's `smoke=` example loaded through the environment's settings. Its docstring is the reference. | The repo's, from 17.4 |
| `checks.py` | `basis check`'s checks of a Dash repo, the part's `checks`: the layers, the callbacks' modules and `async`, and the app driven on its fakes through `bd.testing`. Its docstring is the reference. | The repo's, from 17.1 |
| `loading.py` | `load`, an app by entry point (`basis.dash_apps`) or module; `bd.load` is a dataset's (`loads.py`). | Foundations |
| `testing.py` | `client`: an app built in the process, driven through FastAPI's `TestClient`, with `index`, `layout`, `dependencies`, `page` and `fire`, on the fakes the app declares or a test's. | Foundations; data from 15.4 |
| Data's own, from 15.1 | `declarations.py` (`bd.Row`, `bd.database`, `bd.folder`, `bd.lake`, `bd.http`, `bd.query`, `bd.file`, `bd.fetch`, `bd.view`, and `check`, which building runs first), `format.py` (`bd.format`, each column's display format), `cache.py`, `loads.py` (`bd.load`, which app code imports, so it opens nothing), `runtime.py` (what a built app opens for its datasets, and how each is read, a view computed among them), `fixtures.py` (a fake database from `fixtures/QUERY.csv`, failing on demand, and `of`, the fakes an app declares), `refreshing.py` (`bd.refresh`), `browser.py` (`bd.rows`, named so the module never shadows the function), `sizes.py` (each callback's answer measured, and its budget). | Data |
| Display's own, from 16.1 | `theme.py`, `icons.py`, `pages.py`, `shell.py`, `texts.py` (`bd.text`, and the browser formatter's spec), `grids.py` (`bd.grid`), `tiles.py` (`bd.stat`, `bd.badge`), `filters.py` (`bd.as_of`, `bd.date_range`, `bd.select`, `bd.search`), `charts.py`, `states.py` (loading, empty, failed and stale, and the `on_error` that marks a failed output), `exports.py` (an Excel or CSV export of a grid's rows, with its provenance). | Display |

A lane adds a name to `bd` in two lines of its own file: the import, and the name in its `__all__`.

## Rules to keep

- An app's declarations, and every module an app's code imports, load no server, driver or client; `lint-imports`
  enforces it for this package's.
- basis gives Dash every setting itself, and `basis serve` and `basis dev` refuse every `DASH_*` variable and
  `_DASH_FASTAPI_CONFIG`; a new setting of Dash's that a variable can set is given in `building.py`.
- Nothing is fetched from another origin: no CDN, no font service, no version check. The dev tools are `basis dev`'s
  alone, with their version check off.
- Every private name of Dash's is in `internals.py`, with a test; nothing else imports one.
- Dash keeps one app per process; building an app makes it the process's, and gives it the callbacks of its own
  modules alone, its declaration's and its pages'.
- Tests drive the app in the process with `basis.dash.testing`, with no browser and no socket, but `tests/test_server.py`'s.
- Dash's pytest plugin, `dash`, stays off (`-p no:dash`): it drives a browser, and its hooks fail pytest 9's warnings.

## Checks

From the workspace's root:

```sh
uv run pre-commit run --all-files   # ruff, ty, the lockfile and the import contracts
uv run pytest packages/dash         # this package's tests
uv run pytest -m integration packages/dash   # a query's load on real SQL Server and Oracle, as packages/job/tests/integration/README.md sets them up
```
