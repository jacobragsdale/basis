# How-to guides

Each guide is one task in a Dash app's repo. Paths are the repo's package, as `desk/data.py`; after each, run
`uv run basis check`, and commit what it passes with the code.

## Add a page

1. `uv run basis new page NAME` writes `pages/NAME.py` and `tests/test_NAME.py`, and adds `NAME.page` to the app's
   `pages=[...]`.
2. Lay it out with `bd.page("/NAME", "Title", filters=[...], body=[...])`, each id prefixed with the page's name, since
   every page shares the shell.
3. Write its callbacks in the same module, each an `async def` that reads with `await bd.load(...)`.
4. Fire them in its test with `bd.testing.client(app)`.

The `dash-add-page` skill walks an agent through the same steps.

## Read a new table

1. Write its SQL in `sql/NAME.sql`, binding each param in the database's dialect: `%(as_of)s` for SQL Server, `:as_of`
   for Oracle.
2. Declare its row and the query in `data.py`:
   `bd.query("NAME", WAREHOUSE, "sql/NAME.sql", Row, binds={"as_of": date}, ttl_s=900, stale_s=86_400)`.
3. Add its fixture, `fixtures/NAME.csv`, a synthetic row per line with the row's columns.
4. Check it with `uv run basis load NAME --param as_of=2026-11-12`.

A database the app has not read before is one more `bd.database(NAME, dialect="mssql", deadline_s=30,
max_concurrent=4)`, listed in the app's `resources=[...]`, faked in `dev.py` with `fixtures.database(DATABASE)`, and
configured in each environment as `BASIS_DATABASES__NAME__*`, which `.env.example` lists.

The `dash-add-dataset` skill walks an agent through the same steps.

## Filter, summarise or join datasets

Declare a view, `bd.view("NAME", domain.FUNCTION, Row, INPUT=DATASET)`, whose function is in `domain.py`: it takes each
input's frame by the parameter's name, and each of its own params, typed, and returns a frame of its row. A load of the
view gives every param of its inputs and its own; the cache keeps each answer, so three callbacks that show one summary
compute it once.

## Keep a page fresh

Add `bd.refresh("NAME-refresh", every_s=60)` to the page's body and take its `n_intervals` as a callback's input: each
tick loads again, and the cache answers until the dataset's `ttl_s` has passed. A button that reads again calls
`bd.reload(DATASET, **params)`, which drops the value once the dataset's `reload_s` has passed since it was read.

## Export a grid

Declare it with `bd.grid(ID, Row, exports=True)`, and give its buttons a callback:

```python
@callback(Output(GRID.download, "data"), Input(GRID.export, "n_clicks"), State("as-of", "value"), prevent_initial_call=True)
async def export(_: list[int | None], as_of: str) -> dict[str, object]:
    positions = await bd.load(POSITIONS, as_of=bd.day(as_of))
    return GRID.exported(positions, asked={"As of": as_of}, loaded_at=bd.loaded(POSITIONS, as_of=bd.day(as_of)))
```

An Excel export writes each column's format as Excel's own, and an About sheet with what was asked and when it was
loaded; a CSV export guards each cell against formula injection.

## Serve on a real environment's data

`uv run basis dev --env dev` serves the app on this machine on dev's databases, read from your own
`~/.config/basis/dev.env`, read-only as ever. `uv run basis render PAGE --env dev` and
`uv run basis load DATASET --env dev` read the same way.

## Check a deploy

Set `BASIS_SMOKE__URL` in the environment's settings to the app's address there, as a browser opens it, then run
`uv run basis smoke --env qa`. It checks that the app is ready, that each page answers, and that each dataset that
declares `smoke=` loads with it. Add `--commit SHA` to check the deploy serves that commit.

## Serve behind the firm's proxy

Set `BASIS_SERVE__PREFIX` to the path the proxy serves the app under, as `/apps/desk/`, and
`BASIS_SERVE__PREFIX_STRIPPED=true` where the proxy strips it before it forwards a request. Links between pages and
basis's files follow the prefix; the health routes answer at the server's root too, for the cluster's probes.
